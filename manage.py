#!/usr/bin/env python3
"""User management CLI for Compute Monitor.

Usage:
    python3 manage.py create <username>         # prompts for password
    python3 manage.py passwd <username>         # change password
    python3 manage.py delete <username>
    python3 manage.py list
"""

import getpass
import sqlite3
import sys
import time
from pathlib import Path
from werkzeug.security import generate_password_hash

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
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
