# Deployment: compute.oliverlaboratory.com

```
browser ──HTTPS──▶ 137.184.145.140 (DigitalOcean droplet)
                   └─ Caddy (TLS, reverse proxy) ──▶ 127.0.0.1:5111
                                                       ▲
                                         reverse SSH tunnel (autossh)
                                                       │
               ┌── Flask app on a VU host (aquinas/augustine) ──┐
               │    binds 127.0.0.1:5111, SSHes to VU servers   │
               └─────────────────────────────────────────────────┘
```

The Flask app stays inside the Vanderbilt network because it needs SSH access
to the VU compute hosts. A reverse SSH tunnel publishes it through the DO
droplet, which handles TLS, DNS, and public HTTPS.

## One-time DNS

Create an **A record** at your DNS provider:

```
compute.oliverlaboratory.com.  A  137.184.145.140
```

Wait for propagation (`dig +short compute.oliverlaboratory.com`).

## Part 1 — DigitalOcean droplet (137.184.145.140)

SSH in as root and run:

```bash
# 1. Install Caddy (auto-HTTPS via Let's Encrypt)
apt update
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
apt update
apt install -y caddy

# 2. Install the Caddyfile
#    (Clone the repo so we always serve the committed config.)
mkdir -p /opt
cd /opt
git clone git@github.com:OliverLaboratory/ClusterMonitor.git   # requires deploy key on droplet; OR use HTTPS
# If you don't want to put a deploy key on the droplet, just scp the file:
#   scp deploy/Caddyfile root@137.184.145.140:/etc/caddy/Caddyfile
cp /opt/ClusterMonitor/deploy/Caddyfile /etc/caddy/Caddyfile

# 3. Allow sshd to accept reverse tunnels bound to 127.0.0.1 (it does by default)
#    Make sure these are NOT disabled in /etc/ssh/sshd_config:
#       AllowTcpForwarding yes
#       GatewayPorts no           # (default, and what we want)
#       ClientAliveInterval 60
#       ClientAliveCountMax 3

# 4. Open firewall for 80/443 if you use ufw
ufw allow 80/tcp || true
ufw allow 443/tcp || true

# 5. Start Caddy
systemctl enable --now caddy
systemctl status caddy --no-pager
```

Caddy will obtain a Let's Encrypt cert for `compute.oliverlaboratory.com`
automatically on first request, as long as the DNS A record points here.

Verify:

```bash
curl -I https://compute.oliverlaboratory.com/healthz
# expect: HTTP/2 502 (until the tunnel is up) or 200 (once Flask is reachable)
```

## Part 2 — VU host (aquinas / augustine — wherever the app runs)

```bash
cd /home/gonzc11/compute_monitor

# 1. Python deps (user-local install; adjust path if using a venv)
pip3 install --user -r requirements.txt

# 2. Install autossh if needed
which autossh || sudo dnf install -y autossh   # or apt, depending on OS

# 3. Accept DO host key once (so autossh doesn't block on first connect)
ssh -i ~/.ssh/id_ed25519_clustermonitor_do \
    -o "StrictHostKeyChecking=accept-new" \
    root@137.184.145.140 "echo tunnel-ok"

# 4. Create your first user account
python3 manage.py create carlos
# (prompts for password twice)

# 5. Install the systemd units (needs sudo)
sudo cp deploy/compute-monitor.service        /etc/systemd/system/
sudo cp deploy/compute-monitor-tunnel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now compute-monitor.service
sudo systemctl enable --now compute-monitor-tunnel.service

# 6. Check
systemctl status compute-monitor.service --no-pager
systemctl status compute-monitor-tunnel.service --no-pager
curl -sI http://127.0.0.1:5111/healthz
```

## Verify end-to-end

```bash
curl -I https://compute.oliverlaboratory.com/healthz     # 200 ok
curl -I https://compute.oliverlaboratory.com/            # 302 -> /login
```

Open the browser: <https://compute.oliverlaboratory.com>.
Log in with the account you created in step 4.

## Operating notes

- **Add a user:** `python3 manage.py create <username>` on the VU host.
- **Reset password:** `python3 manage.py passwd <username>`.
- **List users:** `python3 manage.py list`.
- **Delete user:** `python3 manage.py delete <username>`.
- **Restart Flask:** `sudo systemctl restart compute-monitor.service`.
- **Restart tunnel:** `sudo systemctl restart compute-monitor-tunnel.service`.
- **Logs:**
  - Flask: `tail -f /home/gonzc11/compute_monitor/app.log`
  - Tunnel: `journalctl -u compute-monitor-tunnel.service -f`
  - Caddy (on DO): `journalctl -u caddy -f` and `/var/log/caddy/compute.log`
- **Update:** `git pull && sudo systemctl restart compute-monitor.service`.
- **Rotate the session secret:** delete `.secret_key` and restart (logs everyone
  out). Or set `COMPUTE_MONITOR_SECRET` in the systemd unit.

## Why it's set up this way

- **Flask runs on the VU host**, not DO, because it needs SSH access to the
  VU compute servers which are only reachable via Vanderbilt VPN.
- **Reverse tunnel** (augustine → DO, not DO → augustine) because DO can't
  initiate VPN-restricted connections.
- **`-R 127.0.0.1:5111:127.0.0.1:5111`** binds the forwarded port to loopback
  on the DO droplet, so the Flask app is never exposed directly on the
  internet — only Caddy talks to it, and Caddy talks to the internet.
- **Session cookies are `Secure`** (TLS-only) because requests reach Flask
  with `X-Forwarded-Proto: https` from Caddy. The `SESSION_COOKIE_SECURE`
  default means browsers refuse to send the cookie over plain HTTP —
  appropriate for a public site.
