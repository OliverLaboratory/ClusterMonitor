#!/usr/bin/env python3
"""Compute Monitor - SSH-based server monitoring dashboard."""

import subprocess
import time
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, jsonify

app = Flask(__name__)

SERVERS = [
    {"name": "ignatius",  "host": "ignatius",  "desc": "RTX 5090",      "img": "ignatius.jpg"},
    {"name": "chesterton","host": "chesterton","desc": "RTX 5090",      "img": "chesterton.jpg"},
    {"name": "aquinas",   "host": "aquinas",   "desc": "RTX 5090",      "img": "aquinas.jpg"},
    {"name": "origen",    "host": "origen",    "desc": "RTX 5090",      "img": "origen.jpg"},
    {"name": "augustine", "host": "augustine", "desc": "RTX 5090",      "img": "augustine.jpg"},
    {"name": "bf65",      "host": "bf65",      "desc": "8x H100 80GB",  "img": None},
    {"name": "bf64",      "host": "bf64",      "desc": "4x H200 144GB", "img": None},
]

# Cache: {server_name: {"data": ..., "timestamp": ...}}
_cache = {}
CACHE_TTL = 30  # seconds

REMOTE_SCRIPT = r"""
# GPU info
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu --format=csv,noheader 2>/dev/null || echo "NO_GPU"
echo "===SECTION==="

# GPU processes with user/command
nvidia-smi --query-compute-apps=pid,gpu_uuid,used_gpu_memory --format=csv,noheader 2>/dev/null | while IFS= read -r line; do
    pid=$(echo "$line" | cut -d',' -f1 | tr -d ' ')
    if [ -n "$pid" ] && [ "$pid" != "" ]; then
        user=$(ps -o user= -p "$pid" 2>/dev/null | tr -d ' ')
        cmd=$(ps -o args= -p "$pid" 2>/dev/null | head -c 120)
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
ps aux --sort=-%cpu | head -16
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
            input=REMOTE_SCRIPT, capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 and not result.stdout.strip():
            return {"error": f"SSH failed: {result.stderr.strip()[:200]}"}
        return parse_output(result.stdout, server)
    except subprocess.TimeoutExpired:
        return {"error": "SSH timeout (15s)"}
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
                gpus.append({
                    "index": int(parts[0]),
                    "name": parts[1],
                    "mem_used_mb": mem_used,
                    "mem_total_mb": mem_total,
                    "mem_pct": round(mem_used / mem_total * 100, 1) if mem_total else 0,
                    "util_pct": int(parts[4].replace("%", "").strip()),
                    "temp_c": int(parts[5]),
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
                "command": parts[4][:100] if len(parts) > 4 else "",
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
                "command": cols[10][:100],
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
                else:
                    data["name"] = srv["name"]
                    data["desc"] = srv["desc"]
                    data["online"] = False
                results[srv["name"]] = data

    # Return in the original order
    return [results[s["name"]] for s in SERVERS if s["name"] in results]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/servers")
def api_servers():
    return jsonify(collect_all())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5111, debug=False)
