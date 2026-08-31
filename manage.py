#!/usr/bin/env python3
"""User management CLI for Compute Monitor.

Usage:
    python3 manage.py create <username>                        # prompts for password (admin bypass)
    python3 manage.py passwd <username>                        # change password
    python3 manage.py delete <username>
    python3 manage.py list

    python3 manage.py invite <username> [note] [--days N]      # generate an invite link
    python3 manage.py invites                                  # list all invites (status)
    python3 manage.py revoke <token-prefix>                    # revoke an unused invite
"""

import getpass
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from werkzeug.security import generate_password_hash

PUBLIC_BASE_URL = "https://compute.oliverlaboratory.com"
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,30}$")

DB_PATH = Path(__file__).resolve().parent / "users.db"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
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
        cols = {r[1] for r in conn.execute("PRAGMA table_info(invites)").fetchall()}
        if "username" not in cols:
            conn.execute("ALTER TABLE invites ADD COLUMN username TEXT")


def prompt_password():
    p1 = getpass.getpass("Password: ")
    p2 = getpass.getpass("Confirm:  ")
    if p1 != p2:
        sys.exit("passwords do not match")
    if len(p1) < 8:
        sys.exit("password must be at least 8 characters")
    return p1


def cmd_create(username):
    username = username.strip().lower()
    ensure_schema()
    pw = prompt_password()
    h = generate_password_hash(pw)
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                (username, h, int(time.time())),
            )
        except sqlite3.IntegrityError:
            sys.exit(f"user {username!r} already exists (use passwd to reset)")
    print(f"created user {username!r}")


def cmd_passwd(username):
    username = username.strip().lower()
    ensure_schema()
    pw = prompt_password()
    h = generate_password_hash(pw)
    with db() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash=? WHERE username=?", (h, username)
        )
        if cur.rowcount == 0:
            sys.exit(f"no such user {username!r}")
    print(f"password updated for {username!r}")


def cmd_delete(username):
    username = username.strip().lower()
    with db() as conn:
        cur = conn.execute("DELETE FROM users WHERE username=?", (username,))
        if cur.rowcount == 0:
            sys.exit(f"no such user {username!r}")
    print(f"deleted user {username!r}")


def cmd_list():
    ensure_schema()
    with db() as conn:
        rows = conn.execute(
            "SELECT username, created_at FROM users ORDER BY username"
        ).fetchall()
    if not rows:
        print("(no users)")
        return
    for r in rows:
        ts = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
        print(f"{r['username']:20s}  created {ts}")


def cmd_invite(username, note, days):
    username = username.strip().lower()
    if not USERNAME_RE.match(username):
        sys.exit(
            "username must be 2–31 chars: lowercase letters, digits, - or _, "
            "starting with a letter or digit (e.g. 'gonzc11')"
        )
    ensure_schema()
    with db() as conn:
        existing_user = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (username,)
        ).fetchone()
        if existing_user:
            sys.exit(f"user {username!r} already exists — use `passwd` to reset password")
        active = conn.execute(
            "SELECT token FROM invites WHERE username = ? "
            "AND used_at IS NULL AND expires_at > ?",
            (username, int(time.time())),
        ).fetchone()
        if active:
            sys.exit(
                f"an active invite already exists for {username!r} "
                f"(prefix {active['token'][:10]}...). Revoke it first if you want a new one."
            )
        token = secrets.token_urlsafe(24)
        now = int(time.time())
        expires = now + days * 86400
        conn.execute(
            "INSERT INTO invites (token, username, note, created_at, expires_at) "
            "VALUES (?,?,?,?,?)",
            (token, username, note, now, expires),
        )
    url = f"{PUBLIC_BASE_URL}/signup?token={token}"
    exp_str = time.strftime("%Y-%m-%d %H:%M %Z", time.localtime(expires))
    print(url)
    print(f"# user:    {username}")
    print(f"# note:    {note or '(none)'}")
    print(f"# expires: {exp_str}  ({days} days)")
    print(f"# prefix:  {token[:10]}...  (use with `revoke`)")


def cmd_invites():
    ensure_schema()
    with db() as conn:
        rows = conn.execute(
            "SELECT token, username, note, created_at, expires_at, used_at "
            "FROM invites ORDER BY created_at DESC"
        ).fetchall()
    if not rows:
        print("(no invites)")
        return
    now = int(time.time())
    print(f"{'STATUS':8s}  {'USERNAME':14s}  {'PREFIX':14s}  {'DATE':12s}  NOTE")
    for r in rows:
        prefix = r["token"][:10] + "..."
        user = r["username"] or "(unknown)"
        if r["used_at"]:
            status = "used"
            when = time.strftime("%Y-%m-%d", time.localtime(r["used_at"]))
        elif r["expires_at"] < now:
            status = "expired"
            when = time.strftime("%Y-%m-%d", time.localtime(r["expires_at"]))
        else:
            status = "active"
            when = time.strftime("%Y-%m-%d", time.localtime(r["expires_at"]))
        print(f"{status:8s}  {user:14s}  {prefix:14s}  {when:12s}  {r['note'] or ''}")


def cmd_revoke(prefix):
    ensure_schema()
    with db() as conn:
        rows = conn.execute(
            "SELECT token, used_at FROM invites WHERE token LIKE ?",
            (prefix + "%",),
        ).fetchall()
    if not rows:
        sys.exit(f"no invite matches prefix {prefix!r}")
    if len(rows) > 1:
        sys.exit(f"ambiguous prefix {prefix!r} — matches {len(rows)} invites")
    row = rows[0]
    if row["used_at"]:
        sys.exit("invite already used — can't revoke")
    with db() as conn:
        # Mark expired by setting expires_at to the past.
        conn.execute("UPDATE invites SET expires_at = 0 WHERE token = ?", (row["token"],))
    print(f"revoked {row['token'][:10]}...")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    args = sys.argv[2:]
    if cmd == "create" and len(args) == 1:
        cmd_create(args[0])
    elif cmd == "passwd" and len(args) == 1:
        cmd_passwd(args[0])
    elif cmd == "delete" and len(args) == 1:
        cmd_delete(args[0])
    elif cmd == "list":
        cmd_list()
    elif cmd == "invite":
        if not args:
            sys.exit("usage: invite <username> [note...] [--days N]")
        days = 7
        positional = []
        i = 0
        while i < len(args):
            if args[i] == "--days" and i + 1 < len(args):
                try:
                    days = int(args[i + 1])
                except ValueError:
                    sys.exit("--days requires an integer")
                i += 2
            else:
                positional.append(args[i])
                i += 1
        if not positional:
            sys.exit("usage: invite <username> [note...] [--days N]")
        if days < 1 or days > 365:
            sys.exit("--days must be between 1 and 365")
        username = positional[0]
        note = " ".join(positional[1:]) or None
        cmd_invite(username, note, days)
    elif cmd == "invites":
        cmd_invites()
    elif cmd == "revoke" and len(args) == 1:
        cmd_revoke(args[0])
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
