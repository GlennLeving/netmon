# netmon

![Python](https://img.shields.io/badge/python-3.9%2B-blue) ![License](https://img.shields.io/badge/license-MIT-green)

Watches your network by pinging a set of test servers at a fixed interval and
logging when something goes down. It tells **your local network being down**
apart from **your ISP or the internet being down** — so when the connection
drops you know whether to look at your own cable or to call the provider.

No dependencies — Python 3 standard library only, plus the system `ping`.

## Install

Needs Python 3.9+ and the system `ping`. No pip packages.

```
git clone https://github.com/GlennLeving/netmon.git
cd netmon
python3 netmon.py --set-password     # set the password that protects the settings
./start.sh
```

The first start creates `config.json` from the defaults — see
`config.example.json`. Everything else can be changed in the web UI.

## Start

```
./start.sh                      # http://127.0.0.1:8420
./start.sh --host 0.0.0.0       # reachable from other machines
./start.sh --port 9000
```

## How an outage is classified

Each test server is marked either **LAN** (your router, say `192.168.1.1`) or
**internet** (`1.1.1.1`, `8.8.8.8`, `8.8.4.4`).

| Situation | Logged as |
|---|---|
| A single server stops answering | `target` outage for that server |
| No LAN host answers | `LAN` — local network, cable or router is down |
| LAN is up, but no internet host answers | `ISP` — your provider is down |
| No LAN host defined, no internet host answers | `INTERNET` (cause unknown) |

A server has to fail `fail_threshold` times in a row before it counts as down.
That keeps a single dropped packet from raising a false alarm.

## Password

The status page is open to anyone who can reach it, but **changing the settings
requires a login**. Protected calls: saving settings, clearing the outage log,
changing the password.

* Stored only as a PBKDF2 hash (240,000 rounds, random salt) in `auth.json`
  (chmod 600) — never in clear text, and never in `config.json`, which is
  world-readable.
* A login yields a random session cookie (HttpOnly, SameSite=Strict) valid for
  12 hours.
* 5 failed attempts from one IP block that IP for 5 minutes.

Change it in the web UI under Settings, or from the terminal:

```
python3 netmon.py --set-password
```

Forgot it? Delete `auth.json` and run `--set-password` again.

> Note: traffic is plain HTTP, so the password crosses the network unencrypted.
> That is fine on a home network, but do not reuse a password from elsewhere.

## Settings (in the web UI or `config.json`)

| Field | Meaning |
|---|---|
| `interval_seconds` | Seconds between measurement rounds |
| `timeout_seconds` | How long to wait for a ping reply |
| `ping_count` | Pings per measurement (the best round-trip time is used) |
| `fail_threshold` | Failures in a row before the status becomes down |
| `recover_threshold` | Successes in a row before it is up again |
| `retention_days` | How long raw measurements are kept |
| `targets[]` | `{name, host, kind: internet\|lan, enabled}` |

Changes made in the web UI are written to `config.json` and take effect
immediately.

## Data

* `netmon.db` (sqlite) — `samples` (every single ping) and `outages` (outage
  events).
* The log can be exported as CSV from the UI or with `GET /api/export.csv`.

Query directly:

```
sqlite3 netmon.db "SELECT scope, name, datetime(started,'unixepoch','localtime') AS start, ROUND(COALESCE(ended, strftime('%s','now')) - started) AS seconds FROM outages ORDER BY started DESC LIMIT 20;"
```

## API

| Endpoint | Description |
|---|---|
| `GET /api/status` | Live status for every server plus the LAN/ISP verdict |
| `GET /api/config` / `POST /api/config` | Read and write settings |
| `GET /api/outages?scope=all\|link\|target&limit=N` | Outage log |
| `GET /api/history?host=1.1.1.1&hours=24` | Raw measurements |
| `GET /api/export.csv` | The log as CSV |
| `POST /api/check-now` | Run a measurement round straight away |
| `POST /api/clear-log` | Delete finished events (requires a login) |
| `POST /api/login` / `POST /api/logout` | `{"password": "..."}` — sets a session cookie |
| `GET /api/session` | `{authed, lockout}` |
| `POST /api/set-password` | `{"current": "...", "new": "..."}` (requires a login) |

`POST /api/config` requires a login as well.

## Run as a background service (systemd)

The service is installed as a **user service** in
`~/.config/systemd/user/netmon.service` and starts automatically at boot.

```
systemctl --user status netmon      # is it running?
systemctl --user restart netmon     # restart, e.g. after changing the password
systemctl --user stop netmon
systemctl --user disable --now netmon   # turn auto-start off
journalctl --user -u netmon -f      # follow the log live
```

Details:

* `Restart=always` — starts again 5 seconds after a crash.
* `loginctl enable-linger glenn` is on, so it runs even when you are not logged in.
* The log goes to the journal (`journalctl --user -u netmon`), not to `netmon.log`.
* The service runs with `ProtectSystem=strict` and may only write inside `~/netmon`.

To change the port or listen address, edit `ExecStart` in the unit file and run:

```
systemctl --user daemon-reload && systemctl --user restart netmon
```

## Updating

`update.sh` fetches the latest code, restarts the service and checks that it
actually came back:

```
./update.sh
```

It refuses to run while the working tree has uncommitted changes, syntax-checks
the new code before restarting, and if the service does not answer afterwards it
resets to the previous commit and restarts again — so a broken commit does not
leave you with a dead service. `config.json`, `auth.json` and the database are
ignored by git and are never touched.

## Notes

* The status view itself is open without a login — only bind to `0.0.0.0` on a
  network you trust.
* Stop the service with `pkill -f netmon.py` (match the whole command line if you
  use flags).
* Some hosts (a few routers and firewalls) do not answer ICMP even when they are
  up — test with `ping <ip>` in the terminal before adding one.

## License

MIT — see [LICENSE](LICENSE). Author: Glenn Leving.
