#!/usr/bin/env python3
"""Compute Monitor - SSH-based server monitoring dashboard."""

import os
import sqlite3
import subprocess
import threading
import time
import re
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from pathlib import Path
from flask import Flask, render_template, jsonify, request, redirect, url_for, flash, abort
from flask_login import (
    LoginManager, UserMixin, login_user, logout_user,
    login_required, current_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

ADMIN_USERS = {"gonzc11", "shik2"}
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.path))
        if current_user.username not in ADMIN_USERS:
            abort(403)
        return fn(*args, **kwargs)
    return wrapper

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "users.db"
SECRET_KEY_PATH = BASE_DIR / ".secret_key"


def _load_secret_key():
    env_key = os.environ.get("COMPUTE_MONITOR_SECRET")
    if env_key:
        return env_key
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text().strip()
    key = secrets.token_hex(32)
    SECRET_KEY_PATH.write_text(key)
    SECRET_KEY_PATH.chmod(0o600)
    return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    SESSION_COOKIE_SECURE=os.environ.get("COMPUTE_MONITOR_INSECURE_COOKIE") != "1",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,
)

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.session_protection = "strong"


class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.password_hash = row["password_hash"]


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "last_seen" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN last_seen INTEGER")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS invites (
                token TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                note TEXT,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used_at INTEGER,
                used_by_user_id INTEGER
            )"""
        )
        # Migration: add username column if the table predates it
        cols = {r[1] for r in conn.execute("PRAGMA table_info(invites)").fetchall()}
        if "username" not in cols:
            conn.execute("ALTER TABLE invites ADD COLUMN username TEXT")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                server_name TEXT NOT NULL,
                gpus INTEGER NOT NULL DEFAULT 0,
                cpus INTEGER NOT NULL DEFAULT 0,
                starts_at INTEGER NOT NULL,
                ends_at INTEGER,
                note TEXT,
                cancelled_at INTEGER,
                created_at INTEGER NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reservations_active "
            "ON reservations(server_name, cancelled_at, ends_at)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS usage_tally (
                username TEXT PRIMARY KEY,
                kwh REAL NOT NULL DEFAULT 0,
                kg_co2 REAL NOT NULL DEFAULT 0,
                last_updated INTEGER
            )"""
        )


def get_user_by_username(username):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
    return User(row) if row else None


def get_user_by_id(user_id):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
    return User(row) if row else None


@login_manager.user_loader
def load_user(user_id):
    return get_user_by_id(user_id)


init_db()


PRESENCE_WINDOW = 300  # seconds; a user is "online" if seen in the last 5 min


@app.before_request
def _bump_last_seen():
    """Update last_seen for authenticated users, skipping static/health noise."""
    if not current_user.is_authenticated:
        return
    if request.path.startswith("/static/") or request.path == "/healthz":
        return
    try:
        with db() as conn:
            conn.execute(
                "UPDATE users SET last_seen = ? WHERE id = ?",
                (int(time.time()), int(current_user.id)),
            )
    except Exception:
        pass  # presence is best-effort, never break a request

SERVERS = [
    {"name": "ignatius",  "host": "ignatius",  "desc": "RTX 5090",      "img": "ignatius.jpg"},
    {"name": "chesterton","host": "chesterton","desc": "RTX 5090",      "img": "chesterton.jpg"},
    {"name": "aquinas",   "host": "aquinas",   "desc": "RTX 5090",      "img": "aquinas.jpg"},
    {"name": "origen",    "host": "origen",    "desc": "RTX 5090",      "img": "origen.jpg"},
    {"name": "augustine", "host": "augustine", "desc": "RTX 5090",      "img": "augustine.jpg"},
    {"name": "bf65",      "host": "bf65",      "desc": "8x H100 80GB",  "img": "bf65.jpeg"},
    {"name": "bf64",      "host": "bf64",      "desc": "4x H200 144GB", "img": "bf64.jpeg"},
]

# Cache: {server_name: {"data": ..., "timestamp": ...}}
_cache = {}
CACHE_TTL = 30  # seconds

REMOTE_SCRIPT = r"""
# GPU info (power.draw in watts is appended for CO2 tally)
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw --format=csv,noheader,nounits 2>/dev/null || echo "NO_GPU"
echo "===SECTION==="

# GPU processes with user/command
nvidia-smi --query-compute-apps=pid,gpu_uuid,used_gpu_memory --format=csv,noheader 2>/dev/null | while IFS= read -r line; do
    pid=$(echo "$line" | cut -d',' -f1 | tr -d ' ')
    if [ -n "$pid" ] && [ "$pid" != "" ]; then
        user=$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ')
        cmd=$(ps -o args= -p "$pid" 2>/dev/null | head -c 300)
        echo "$line, $user, $cmd"
    fi
done
echo "===SECTION==="

# Memory
free -m
echo "===SECTION==="

# CPU count
nproc
echo "===SECTION==="

# Uptime / load
uptime
echo "===SECTION==="

# Top processes by CPU (skip header)
ps aux --sort=-%cpu | head -31
echo "===SECTION==="

# Logged-in users
who 2>/dev/null
echo "===SECTION==="

# Disk
df -h / | tail -1
"""


def ssh_collect(server):
    """Run the collection script on a remote server via SSH."""
    host = server["host"]
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
             "-o", "StrictHostKeyChecking=no", host, "bash -s"],
            input=REMOTE_SCRIPT, capture_output=True, text=True, timeout=25,
        )
        if result.returncode != 0 and not result.stdout.strip():
            return {"error": f"SSH failed: {result.stderr.strip()[:200]}"}
        return parse_output(result.stdout, server)
    except subprocess.TimeoutExpired:
        return {"error": "SSH timeout (25s)"}
    except Exception as e:
        return {"error": str(e)[:200]}


def parse_output(raw, server):
    """Parse the sectioned output from the remote script."""
    sections = raw.split("===SECTION===")
    if len(sections) < 8:
        return {"error": f"Unexpected output format ({len(sections)} sections)"}

    data = {
        "name": server["name"],
        "desc": server["desc"],
        "img": server.get("img"),
        "online": True,
    }

    # --- GPUs ---
    gpus = []
    gpu_lines = sections[0].strip().splitlines()
    if gpu_lines and gpu_lines[0].strip() != "NO_GPU":
        for line in gpu_lines:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                mem_used = int(parts[2].replace("MiB", "").strip())
                mem_total = int(parts[3].replace("MiB", "").strip())
                # power.draw may be "[N/A]" or missing on some systems
                power_w = 0.0
                if len(parts) >= 7:
                    try:
                        power_w = float(parts[6].replace("W", "").strip())
                    except ValueError:
                        power_w = 0.0
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "mem_used_mb": mem_used,
                    "mem_total_mb": mem_total,
                    "mem_pct": round(mem_used / mem_total * 100, 1) if mem_total else 0,
                    "util_pct": int(parts[4].replace("%", "").strip()),
                    "temp_c": int(parts[5]),
                    "power_w": power_w,
                })
    data["gpus"] = gpus

    # --- GPU processes ---
    gpu_procs = []
    for line in sections[1].strip().splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split(",", 4)]
        if len(parts) >= 5:
            gpu_procs.append({
                "pid": parts[0],
                "gpu_mem_mb": parts[2].replace("MiB", "").strip(),
                "user": parts[3],
                "command": parts[4][:300] if len(parts) > 4 else "",
            })
    data["gpu_processes"] = gpu_procs

    # --- RAM ---
    mem_lines = sections[2].strip().splitlines()
    data["ram"] = {"total_mb": 0, "used_mb": 0, "available_mb": 0, "pct": 0}
    for line in mem_lines:
        if line.startswith("Mem:"):
            cols = line.split()
            total = int(cols[1])
            used = int(cols[2])
            avail = int(cols[6]) if len(cols) > 6 else total - used
            data["ram"] = {
                "total_mb": total,
                "used_mb": used,
                "available_mb": avail,
                "pct": round(used / total * 100, 1) if total else 0,
            }
        elif line.startswith("Swap:"):
            cols = line.split()
            data["swap"] = {
                "total_mb": int(cols[1]),
                "used_mb": int(cols[2]),
            }

    # --- CPU count ---
    try:
        data["cpu_count"] = int(sections[3].strip())
    except ValueError:
        data["cpu_count"] = 0

    # --- Uptime / load ---
    uptime_raw = sections[4].strip()
    data["uptime_raw"] = uptime_raw
    load_match = re.search(r"load average:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)", uptime_raw)
    if load_match:
        data["load"] = {
            "1m": float(load_match.group(1)),
            "5m": float(load_match.group(2)),
            "15m": float(load_match.group(3)),
        }
    else:
        data["load"] = {"1m": 0, "5m": 0, "15m": 0}

    # Uptime duration
    up_match = re.search(r"up\s+(.+?),\s+\d+\s+user|up\s+(.+?),\s+load", uptime_raw)
    if up_match:
        data["uptime"] = (up_match.group(1) or up_match.group(2)).strip().rstrip(",")
    else:
        data["uptime"] = "unknown"

    # --- Top processes ---
    procs = []
    proc_lines = sections[5].strip().splitlines()
    for line in proc_lines[1:]:  # skip header
        cols = line.split(None, 10)
        if len(cols) >= 11:
            procs.append({
                "user": cols[0],
                "pid": cols[1],
                "cpu_pct": cols[2],
                "mem_pct": cols[3],
                "rss_kb": cols[5],
                "command": cols[10][:300],
            })
    data["top_processes"] = procs

    # --- Who ---
    users = []
    for line in sections[6].strip().splitlines():
        if line.strip():
            parts = line.split()
            users.append({
                "user": parts[0] if parts else "",
                "terminal": parts[1] if len(parts) > 1 else "",
                "login_time": " ".join(parts[2:4]) if len(parts) > 3 else "",
            })
    data["logged_in_users"] = users

    # --- Disk ---
    disk_line = sections[7].strip()
    if disk_line:
        cols = disk_line.split()
        if len(cols) >= 5:
            data["disk"] = {
                "size": cols[1],
                "used": cols[2],
                "avail": cols[3],
                "pct": cols[4],
            }
        else:
            data["disk"] = {}
    else:
        data["disk"] = {}

    return data


# --- CO2 accounting ---
# TVA (Vanderbilt's grid operator) is ~0.35 kg CO2 / kWh (nuclear + gas heavy).
CO2_KG_PER_KWH = 0.35
_last_sample_time = {}  # {server_name: float timestamp}
SAMPLE_MIN_SEC = 20     # ignore two polls closer together than this
SAMPLE_MAX_SEC = 300    # gap this big means we lost observation, don't attribute


def _accumulate_usage(server_name, data):
    """Attribute GPU energy consumption to users based on their GPU memory share."""
    now = time.time()
    prev = _last_sample_time.get(server_name)
    _last_sample_time[server_name] = now
    if prev is None:
        return
    delta = now - prev
    if delta < SAMPLE_MIN_SEC or delta > SAMPLE_MAX_SEC:
        return

    total_power_w = sum(g.get("power_w", 0) or 0 for g in data.get("gpus", []))
    if total_power_w <= 0:
        return
    procs = data.get("gpu_processes", [])
    total_proc_mem = 0.0
    for p in procs:
        try:
            total_proc_mem += float(p.get("gpu_mem_mb", 0) or 0)
        except (TypeError, ValueError):
            pass
    if total_proc_mem <= 0:
        return

    energy_kwh = total_power_w * delta / 3600.0 / 1000.0
    per_user_kwh = {}
    for p in procs:
        user = (p.get("user") or "").strip()
        if not user:
            continue
        try:
            mem = float(p.get("gpu_mem_mb", 0) or 0)
        except (TypeError, ValueError):
            continue
        share = mem / total_proc_mem
        per_user_kwh[user] = per_user_kwh.get(user, 0.0) + energy_kwh * share

    if not per_user_kwh:
        return
    ts = int(now)
    with db() as conn:
        for user, kwh in per_user_kwh.items():
            kg = kwh * CO2_KG_PER_KWH
            conn.execute(
                "INSERT INTO usage_tally (username, kwh, kg_co2, last_updated) "
                "VALUES (?,?,?,?) "
                "ON CONFLICT(username) DO UPDATE SET "
                "kwh = kwh + excluded.kwh, "
                "kg_co2 = kg_co2 + excluded.kg_co2, "
                "last_updated = excluded.last_updated",
                (user, kwh, kg, ts),
            )


def collect_all():
    """Collect data from all servers in parallel, using cache."""
    now = time.time()
    results = {}
    stale = []

    for srv in SERVERS:
        cached = _cache.get(srv["name"])
        if cached and (now - cached["timestamp"]) < CACHE_TTL:
            results[srv["name"]] = cached["data"]
        else:
            stale.append(srv)

    if stale:
        with ThreadPoolExecutor(max_workers=len(stale)) as pool:
            futures = {pool.submit(ssh_collect, s): s for s in stale}
            for future in as_completed(futures):
                srv = futures[future]
                try:
                    data = future.result()
                except Exception as e:
                    data = {"error": str(e)}
                if "error" not in data:
                    _cache[srv["name"]] = {"data": data, "timestamp": time.time()}
                    try:
                        _accumulate_usage(srv["name"], data)
                    except Exception:
                        pass  # never let accounting break the dashboard
                else:
                    data["name"] = srv["name"]
                    data["desc"] = srv["desc"]
                    data["online"] = False
                results[srv["name"]] = data

    # Return in the original order
    return [results[s["name"]] for s in SERVERS if s["name"] in results]


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = (request.form.get("username") or "").strip().lower()
        password = request.form.get("password") or ""
        user = get_user_by_username(username)
        if user and check_password_hash(user.password_hash, password):
            login_user(user, remember=True)
            nxt = request.args.get("next")
            if nxt and nxt.startswith("/"):
                return redirect(nxt)
            return redirect(url_for("index"))
        flash("Invalid username or password.")
        time.sleep(0.5)
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


def _load_invite(token):
    """Return invite row if valid+unused+unexpired, else None."""
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM invites WHERE token = ?", (token,)
        ).fetchone()
    if not row:
        return None
    if row["used_at"] is not None:
        return None
    if row["expires_at"] < int(time.time()):
        return None
    return row


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    token = (request.values.get("token") or "").strip()
    invite = _load_invite(token)
    if invite is None:
        return render_template("signup.html", invalid=True), 410

    username = invite["username"]

    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm = request.form.get("confirm") or ""

        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return render_template("signup.html", token=token, username=username), 400
        if password != confirm:
            flash("Passwords do not match.")
            return render_template("signup.html", token=token, username=username), 400

        with db() as conn:
            existing = conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            ).fetchone()
            if existing:
                flash("An account with this username already exists. Contact the admin.")
                return render_template("signup.html", token=token, username=username), 409

            # Re-check invite inside the transaction to close race window.
            inv = conn.execute(
                "SELECT * FROM invites WHERE token = ?", (token,)
            ).fetchone()
            if not inv or inv["used_at"] is not None or inv["expires_at"] < int(time.time()):
                return render_template("signup.html", invalid=True), 410

            cur = conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                (username, generate_password_hash(password), int(time.time())),
            )
            new_user_id = cur.lastrowid
            conn.execute(
                "UPDATE invites SET used_at = ?, used_by_user_id = ? WHERE token = ?",
                (int(time.time()), new_user_id, token),
            )

        user = get_user_by_id(new_user_id)
        login_user(user, remember=True)
        return redirect(url_for("index"))

    return render_template("signup.html", token=token, username=username)


@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        username=current_user.username,
        is_admin=current_user.username in ADMIN_USERS,
        servers=[{"name": s["name"], "desc": s["desc"]} for s in SERVERS],
    )


@app.route("/api/servers")
@login_required
def api_servers():
    return jsonify(collect_all())


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/api/leaderboard")
@login_required
def api_leaderboard():
    with db() as conn:
        rows = conn.execute(
            "SELECT username, kwh, kg_co2, last_updated "
            "FROM usage_tally ORDER BY kg_co2 DESC"
        ).fetchall()
    return jsonify([
        {
            "username": r["username"],
            "kwh": round(r["kwh"], 3),
            "kg_co2": round(r["kg_co2"], 3),
            "last_updated": r["last_updated"],
        }
        for r in rows
    ])


@app.route("/api/presence")
@login_required
def api_presence():
    cutoff = int(time.time()) - PRESENCE_WINDOW
    with db() as conn:
        rows = conn.execute(
            "SELECT username, last_seen FROM users "
            "WHERE last_seen IS NOT NULL AND last_seen >= ? "
            "ORDER BY last_seen DESC",
            (cutoff,),
        ).fetchall()
    return jsonify([
        {"username": r["username"], "last_seen": r["last_seen"]}
        for r in rows
    ])


# ---------- Reservations ----------

SERVER_NAMES = {s["name"] for s in SERVERS}


def _reservation_row(row):
    return {
        "id": row["id"],
        "username": row["username"],
        "server": row["server_name"],
        "gpus": row["gpus"],
        "cpus": row["cpus"],
        "starts_at": row["starts_at"],
        "ends_at": row["ends_at"],
        "note": row["note"] or "",
        "created_at": row["created_at"],
        "mine": str(row["user_id"]) == current_user.get_id(),
    }


@app.route("/api/reservations", methods=["GET"])
@login_required
def api_reservations_list():
    """Return active + upcoming reservations (not cancelled, not expired)."""
    now = int(time.time())
    with db() as conn:
        rows = conn.execute(
            "SELECT r.*, u.username FROM reservations r "
            "JOIN users u ON u.id = r.user_id "
            "WHERE r.cancelled_at IS NULL "
            "AND (r.ends_at IS NULL OR r.ends_at > ?) "
            "ORDER BY r.server_name, r.starts_at",
            (now,),
        ).fetchall()
    return jsonify([_reservation_row(r) for r in rows])


@app.route("/api/reservations", methods=["POST"])
@login_required
def api_reservations_create():
    data = request.get_json(silent=True) or request.form
    server = (data.get("server") or "").strip()
    if server not in SERVER_NAMES:
        return jsonify({"error": f"unknown server {server!r}"}), 400

    try:
        gpus = int(data.get("gpus") or 0)
        cpus = int(data.get("cpus") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "gpus and cpus must be integers"}), 400
    if gpus < 0 or gpus > 32 or cpus < 0 or cpus > 1024:
        return jsonify({"error": "gpus/cpus out of range"}), 400
    if gpus == 0 and cpus == 0:
        return jsonify({"error": "reserve at least 1 GPU or 1 CPU"}), 400

    now = int(time.time())

    starts_at_raw = data.get("starts_at")
    if starts_at_raw in (None, "", "now"):
        starts_at = now
    else:
        try:
            starts_at = int(starts_at_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "starts_at must be a unix timestamp"}), 400
        if starts_at < now - 300:
            return jsonify({"error": "starts_at is in the past"}), 400
        if starts_at > now + 86400 * 30:
            return jsonify({"error": "starts_at is more than 30 days out"}), 400

    hours_raw = data.get("hours")
    ends_at = None
    if hours_raw not in (None, "", "0"):
        try:
            hours = float(hours_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "hours must be a number"}), 400
        if hours <= 0 or hours > 24 * 30:
            return jsonify({"error": "hours must be between 0 and 720"}), 400
        ends_at = int(starts_at + hours * 3600)

    note = (data.get("note") or "").strip()[:200] or None

    with db() as conn:
        cur = conn.execute(
            "INSERT INTO reservations "
            "(user_id, server_name, gpus, cpus, starts_at, ends_at, note, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (int(current_user.id), server, gpus, cpus, starts_at, ends_at, note, now),
        )
        rid = cur.lastrowid
        row = conn.execute(
            "SELECT r.*, u.username FROM reservations r "
            "JOIN users u ON u.id = r.user_id WHERE r.id = ?",
            (rid,),
        ).fetchone()
    return jsonify(_reservation_row(row)), 201


@app.route("/api/reservations/<int:rid>/edit", methods=["POST"])
@login_required
def api_reservations_edit(rid):
    data = request.get_json(silent=True) or request.form
    now = int(time.time())

    with db() as conn:
        row = conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (rid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        if str(row["user_id"]) != current_user.get_id():
            return jsonify({"error": "not your reservation"}), 403
        if row["cancelled_at"] is not None:
            return jsonify({"error": "reservation is cancelled"}), 409

        updates = {}

        if "gpus" in data:
            try:
                g = int(data["gpus"] or 0)
            except (TypeError, ValueError):
                return jsonify({"error": "gpus must be an integer"}), 400
            if g < 0 or g > 32:
                return jsonify({"error": "gpus out of range"}), 400
            updates["gpus"] = g

        if "cpus" in data:
            try:
                c = int(data["cpus"] or 0)
            except (TypeError, ValueError):
                return jsonify({"error": "cpus must be an integer"}), 400
            if c < 0 or c > 1024:
                return jsonify({"error": "cpus out of range"}), 400
            updates["cpus"] = c

        final_gpus = updates.get("gpus", row["gpus"])
        final_cpus = updates.get("cpus", row["cpus"])
        if final_gpus == 0 and final_cpus == 0:
            return jsonify({"error": "must keep at least 1 GPU or 1 CPU"}), 400

        if "starts_at" in data:
            if row["starts_at"] <= now:
                return jsonify({"error": "cannot change start time after reservation has started"}), 409
            raw = data["starts_at"]
            if raw in (None, "", "now"):
                updates["starts_at"] = now
            else:
                try:
                    new_start = int(raw)
                except (TypeError, ValueError):
                    return jsonify({"error": "starts_at must be a unix timestamp"}), 400
                if new_start < now - 300:
                    return jsonify({"error": "starts_at is in the past"}), 400
                if new_start > now + 86400 * 30:
                    return jsonify({"error": "starts_at is more than 30 days out"}), 400
                updates["starts_at"] = new_start

        # Duration: if "hours" key is present, we reinterpret ends_at.
        if "hours" in data:
            hours_raw = data["hours"]
            effective_start = updates.get("starts_at", row["starts_at"])
            if hours_raw in (None, ""):
                updates["ends_at"] = None
            else:
                try:
                    hours = float(hours_raw)
                except (TypeError, ValueError):
                    return jsonify({"error": "hours must be a number"}), 400
                if hours <= 0 or hours > 24 * 30:
                    return jsonify({"error": "hours must be between 0 and 720"}), 400
                updates["ends_at"] = int(effective_start + hours * 3600)

        if "note" in data:
            note = (data.get("note") or "").strip()[:200] or None
            updates["note"] = note

        if not updates:
            return jsonify({"error": "no fields to update"}), 400

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        params = list(updates.values()) + [rid]
        conn.execute(f"UPDATE reservations SET {set_clause} WHERE id = ?", params)

        row = conn.execute(
            "SELECT r.*, u.username FROM reservations r "
            "JOIN users u ON u.id = r.user_id WHERE r.id = ?",
            (rid,),
        ).fetchone()
    return jsonify(_reservation_row(row))


@app.route("/api/reservations/<int:rid>/cancel", methods=["POST"])
@login_required
def api_reservations_cancel(rid):
    now = int(time.time())
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM reservations WHERE id = ?", (rid,)
        ).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        if str(row["user_id"]) != current_user.get_id():
            return jsonify({"error": "not your reservation"}), 403
        if row["cancelled_at"] is not None:
            return jsonify({"error": "already cancelled"}), 409
        conn.execute(
            "UPDATE reservations SET cancelled_at = ? WHERE id = ?", (now, rid)
        )
    return jsonify({"ok": True})


# --- Background sampler ---
# Runs collect_all() on an interval even when no dashboard is open, so the
# CO2 tally reflects reality, not just "times Carlos had the page open".
BG_INTERVAL_SEC = 30
_bg_thread_started = False


def _background_sampler():
    # Sleep a moment on boot so the main thread can finish init cleanly.
    time.sleep(3)
    while True:
        try:
            collect_all()
        except Exception as e:
            # Best-effort: never kill the worker over a transient error.
            print(f"[bg-sampler] {type(e).__name__}: {e}", flush=True)
        time.sleep(BG_INTERVAL_SEC)


def _start_background_sampler_once():
    global _bg_thread_started
    if _bg_thread_started:
        return
    if os.environ.get("COMPUTE_MONITOR_BG_WORKER") == "0":
        return
    _bg_thread_started = True
    t = threading.Thread(target=_background_sampler, name="bg-sampler", daemon=True)
    t.start()


@app.route("/admin")
@admin_required
def admin_dashboard():
    now = int(time.time())
    with db() as conn:
        users = conn.execute(
            "SELECT id, username, created_at, last_seen FROM users ORDER BY username"
        ).fetchall()
        invites = conn.execute(
            "SELECT token, username, note, created_at, expires_at, used_at "
            "FROM invites ORDER BY created_at DESC"
        ).fetchall()
    users_out = [dict(u) for u in users]
    invites_out = []
    for inv in invites:
        d = dict(inv)
        if d["used_at"]:
            d["status"] = "used"
        elif d["expires_at"] < now:
            d["status"] = "expired"
        else:
            d["status"] = "active"
        d["signup_url"] = url_for("signup", token=d["token"], _external=True)
        invites_out.append(d)
    return render_template(
        "admin.html",
        username=current_user.username,
        admin_users=sorted(ADMIN_USERS),
        users=users_out,
        invites=invites_out,
        now=now,
    )


@app.route("/admin/invites", methods=["POST"])
@admin_required
def admin_create_invite():
    username = (request.form.get("username") or "").strip().lower()
    note = (request.form.get("note") or "").strip() or None
    try:
        days = int(request.form.get("days") or "7")
    except ValueError:
        days = 7
    days = max(1, min(365, days))
    if not USERNAME_RE.match(username):
        flash("Username must be 2-31 chars: lowercase letters, digits, - or _.", "error")
        return redirect(url_for("admin_dashboard"))
    now = int(time.time())
    with db() as conn:
        existing_user = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing_user:
            flash(f"User {username!r} already exists.", "error")
            return redirect(url_for("admin_dashboard"))
        active = conn.execute(
            "SELECT token FROM invites WHERE username = ? "
            "AND used_at IS NULL AND expires_at > ?",
            (username, now),
        ).fetchone()
        if active:
            flash(
                f"Active invite already exists for {username!r}. Revoke it first.",
                "error",
            )
            return redirect(url_for("admin_dashboard"))
        token = secrets.token_urlsafe(24)
        conn.execute(
            "INSERT INTO invites (token, username, note, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (token, username, note, now, now + days * 86400),
        )
    flash(
        f"Invite created for {username!r}: "
        f"{url_for('signup', token=token, _external=True)}",
        "ok",
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/invites/<token>/revoke", methods=["POST"])
@admin_required
def admin_revoke_invite(token):
    with db() as conn:
        row = conn.execute(
            "SELECT used_at FROM invites WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            flash("Invite not found.", "error")
            return redirect(url_for("admin_dashboard"))
        if row["used_at"]:
            flash("Invite already used — can't revoke.", "error")
            return redirect(url_for("admin_dashboard"))
        conn.execute("UPDATE invites SET expires_at = 0 WHERE token = ?", (token,))
    flash("Invite revoked.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<username>/delete", methods=["POST"])
@admin_required
def admin_delete_user(username):
    username = username.strip().lower()
    if username in ADMIN_USERS:
        flash(f"Cannot delete admin {username!r}.", "error")
        return redirect(url_for("admin_dashboard"))
    with db() as conn:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        if cur.rowcount == 0:
            flash(f"No such user {username!r}.", "error")
        else:
            flash(f"Deleted user {username!r}.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/users/<username>/password", methods=["POST"])
@admin_required
def admin_reset_password(username):
    username = username.strip().lower()
    password = request.form.get("password") or ""
    if len(password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("admin_dashboard"))
    h = generate_password_hash(password)
    with db() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?", (h, username)
        )
        if cur.rowcount == 0:
            flash(f"No such user {username!r}.", "error")
        else:
            flash(f"Password updated for {username!r}.", "ok")
    return redirect(url_for("admin_dashboard"))


_start_background_sampler_once()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5111, debug=False)
