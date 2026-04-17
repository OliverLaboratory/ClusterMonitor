# ClusterMonitor

Lightweight Flask dashboard for monitoring the Oliver Lab compute servers. Runs on `augustine` and polls each host over SSH for GPU / CPU / RAM / disk / process info.

![monitored hosts: ignatius · chesterton · aquinas · origen · augustine · bf65 · bf64]()

## Usage (for lab members)

The dashboard is running continuously on `augustine:5111`. You don't run anything yourself — you just forward the port to your laptop.

From **your laptop**, open a terminal and run:

```bash
ssh -L 5111:localhost:5111 <your-vu-id>@augustine.csb.vanderbilt.edu
```

Leave that SSH session open, then in a browser go to:

```
http://localhost:5111
```

That's it. The page auto-refreshes every 30 seconds.

If port `5111` is already in use on your laptop, map it to anything else:

```bash
ssh -L 8080:localhost:5111 <your-vu-id>@augustine.csb.vanderbilt.edu
# then open http://localhost:8080
```

## What you'll see

- **Overview** — mini cards for every server with CPU / RAM / GPU bars. Click one to jump to its full details.
- **Users** — aggregated per-user view: who's using what, where, and how much.
- **Details** — per-server view with all GPUs, top processes, and logged-in users.
- **Warning dots** — a pulsing red dot appears on a server only when it's in genuine crash risk (RAM ≥ 95%, root disk ≥ 97%, GPU ≥ 90 °C). Click the dot for a specific suggestion on how to rescue the node.
- **Easter egg** — double-click a theologian's portrait.

## For the admin

The dashboard lives in `/home/gonzc11/compute_monitor/` on `augustine`. It requires passwordless SSH from `augustine` to every host in the `SERVERS` list in `app.py`.

Start / restart:

```bash
cd /home/gonzc11/compute_monitor
pkill -f compute_monitor/app.py
setsid nohup python3 app.py > app.log 2>&1 < /dev/null &
```

Check it's up:

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:5111/
```

Adding a server — edit the `SERVERS` list in `app.py`, drop a portrait in `static/img/`, restart. Adding a user portrait — drop the image in `static/img/` and add an entry to `USER_MAP` in `templates/index.html`.

Templates are cached, so restart after any HTML edit and hard-refresh the browser.
