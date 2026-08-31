#!/usr/bin/env bash
# Reverse SSH tunnel to the DO droplet:
#   - Flask dashboard:  droplet 127.0.0.1:$DASH_PORT       -> this host 127.0.0.1:5111
#   - SSH back-channel: droplet 127.0.0.1:$SSH_TUNNEL_PORT -> this host 127.0.0.1:22
#
# Ports are chosen per-workstation by hostname. ~/compute_monitor is on a
# shared NFS mount, so we cannot rely on per-host config files or systemd
# Environment= overrides — a single shared script must self-dispatch.

set -u
KEY="$HOME/.ssh/id_ed25519_clustermonitor_do"
REMOTE="root@137.184.145.140"

case "$(hostname -s)" in
    augustine)  DASH_PORT=5111; SSH_TUNNEL_PORT=2222 ;;
    aquinas)    DASH_PORT=5112; SSH_TUNNEL_PORT=2223 ;;
    ignatius)   DASH_PORT=5113; SSH_TUNNEL_PORT=2224 ;;
    origen)     DASH_PORT=5114; SSH_TUNNEL_PORT=2225 ;;
    chesterton) DASH_PORT=5115; SSH_TUNNEL_PORT=2226 ;;
    bf64)       DASH_PORT=5116; SSH_TUNNEL_PORT=2227 ;;
    bf65)       DASH_PORT=5117; SSH_TUNNEL_PORT=2228 ;;
    *)
        echo "[$(date -Iseconds)] [$(hostname -s)] unknown host, refusing to start tunnel" >&2
        exit 64
        ;;
esac

HOST=$(hostname -s)
echo "[$(date -Iseconds)] [$HOST] starting tunnel: dash=$DASH_PORT ssh=$SSH_TUNNEL_PORT" >&2

while true; do
    ssh -N -T \
        -o "ExitOnForwardFailure=yes" \
        -o "ServerAliveInterval=30" \
        -o "ServerAliveCountMax=3" \
        -o "StrictHostKeyChecking=accept-new" \
        -i "$KEY" \
        -R 127.0.0.1:${DASH_PORT}:127.0.0.1:5111 \
        -R 127.0.0.1:${SSH_TUNNEL_PORT}:127.0.0.1:22 \
        "$REMOTE"
    echo "[$(date -Iseconds)] [$HOST] tunnel dropped (dash=$DASH_PORT ssh=$SSH_TUNNEL_PORT), reconnecting in 10s" >&2
    sleep 10
done
