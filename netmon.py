#!/usr/bin/env python3
"""
netmon - overvaager netvaerk/ISP ved at pinge testservere og logge nedetid.

Ingen eksterne dependencies. Kun Python stdlib + systemets 'ping'.

Start:  python3 netmon.py            (web-UI paa http://127.0.0.1:8420)
        python3 netmon.py --host 0.0.0.0 --port 8420

Author: Glenn Leving
"""

from __future__ import annotations

__author__ = "Glenn Leving"

import argparse
import getpass
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "netmon.db")
AUTH_PATH = os.path.join(BASE_DIR, "auth.json")

IS_WINDOWS = platform.system().lower().startswith("win")
IS_MAC = platform.system().lower() == "darwin"

DEFAULT_CONFIG = {
    "interval_seconds": 30,      # frekvens: hvor ofte der pinges
    "timeout_seconds": 2.0,      # wait-time: hvor laenge der ventes paa svar
    "ping_count": 1,             # antal pings pr. maaling
    "fail_threshold": 3,         # antal fejl i traek foer "nede"
    "recover_threshold": 2,      # antal ok i traek foer "oppe" igen
    "retention_days": 30,        # hvor laenge maalinger gemmes
    "targets": [
        {"name": "Cloudflare DNS", "host": "1.1.1.1", "kind": "internet", "enabled": True},
        {"name": "Google DNS", "host": "8.8.8.8", "kind": "internet", "enabled": True},
        {"name": "Google DNS 2", "host": "8.8.4.4", "kind": "internet", "enabled": True},
        {"name": "Gateway/Router", "host": "192.168.1.1", "kind": "lan", "enabled": True},
    ],
}

CONFIG_LIMITS = {
    "interval_seconds": (1, 86400),
    "timeout_seconds": (0.1, 60.0),
    "ping_count": (1, 10),
    "fail_threshold": (1, 100),
    "recover_threshold": (1, 100),
    "retention_days": (1, 3650),
}

HOST_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,255}$")
RTT_RE = re.compile(r"(?:time|tid)[=<]\s*([0-9.,]+)\s*ms", re.IGNORECASE)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

_cfg_lock = threading.RLock()
_config = json.loads(json.dumps(DEFAULT_CONFIG))


def load_config() -> dict:
    global _config
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            _config = validate_config(raw)
        except Exception as exc:  # korrupt config maa ikke vaelte programmet
            print(f"[netmon] kunne ikke laese config.json ({exc}) - bruger standard", file=sys.stderr)
            _config = json.loads(json.dumps(DEFAULT_CONFIG))
    save_config(_config)
    return _config


def save_config(cfg: dict) -> None:
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, CONFIG_PATH)


def validate_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("config skal vaere et objekt")
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))

    for key, (lo, hi) in CONFIG_LIMITS.items():
        if key not in raw:
            continue
        try:
            val = float(raw[key]) if key == "timeout_seconds" else int(raw[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} skal vaere et tal")
        if not (lo <= val <= hi):
            raise ValueError(f"{key} skal vaere mellem {lo} og {hi}")
        cfg[key] = val

    targets = raw.get("targets", cfg["targets"])
    if not isinstance(targets, list):
        raise ValueError("targets skal vaere en liste")
    clean: list[dict] = []
    seen: set[str] = set()
    for item in targets:
        if not isinstance(item, dict):
            raise ValueError("hver testserver skal vaere et objekt")
        host = str(item.get("host", "")).strip()
        if not host:
            continue
        if not HOST_RE.match(host):
            raise ValueError(f"ugyldig vaert: {host!r}")
        if host in seen:
            continue
        seen.add(host)
        kind = str(item.get("kind", "internet")).strip().lower()
        if kind not in ("internet", "lan"):
            kind = "internet"
        clean.append({
            "name": (str(item.get("name", "")).strip() or host)[:64],
            "host": host,
            "kind": kind,
            "enabled": bool(item.get("enabled", True)),
        })
    if not clean:
        raise ValueError("der skal vaere mindst en testserver")
    cfg["targets"] = clean
    return cfg


def get_config() -> dict:
    with _cfg_lock:
        return json.loads(json.dumps(_config))


def set_config(new_cfg: dict) -> dict:
    global _config
    cfg = validate_config(new_cfg)
    with _cfg_lock:
        _config = cfg
        save_config(cfg)
    return cfg



# --------------------------------------------------------------------------
# Adgangskode / sessioner
#
# Kun laesning af status er aabent. Aendring af indstillinger kraever login.
# Adgangskoden gemmes aldrig i klartekst - kun som PBKDF2-hash i auth.json.
# --------------------------------------------------------------------------

PBKDF2_ITERATIONS = 240_000
SESSION_TTL = 12 * 3600          # hvor laenge et login holder
SESSION_COOKIE = "netmon_session"
MAX_FAILED = 5                   # forsoeg foer midlertidig spaerring
LOCKOUT_SECONDS = 300

_auth_lock = threading.Lock()
_sessions: dict[str, float] = {}          # token -> udloeber
_failed: dict[str, tuple[int, float]] = {}  # ip -> (antal, spaerret indtil)


def hash_password(password: str, salt: bytes | None = None) -> dict:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return {"salt": salt.hex(), "hash": digest.hex(), "iterations": PBKDF2_ITERATIONS}


def set_password(password: str) -> None:
    if len(password) < 4:
        raise ValueError("adgangskoden skal vaere mindst 4 tegn")
    tmp = AUTH_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(hash_password(password), fh, indent=2)
        fh.write("\n")
    os.replace(tmp, AUTH_PATH)
    os.chmod(AUTH_PATH, 0o600)
    with _auth_lock:
        _sessions.clear()          # skift af kode logger alle ud


def check_password(password: str) -> bool:
    try:
        with open(AUTH_PATH, "r", encoding="utf-8") as fh:
            rec = json.load(fh)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"),
            bytes.fromhex(rec["salt"]), int(rec["iterations"]),
        )
        return hmac.compare_digest(digest.hex(), rec["hash"])
    except Exception:
        return False


def new_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _auth_lock:
        for tok, exp in list(_sessions.items()):
            if exp < now:
                del _sessions[tok]
        _sessions[token] = now + SESSION_TTL
    return token


def valid_session(token: str | None) -> bool:
    if not token:
        return False
    with _auth_lock:
        exp = _sessions.get(token)
        if exp is None:
            return False
        if exp < time.time():
            del _sessions[token]
            return False
    return True


def drop_session(token: str | None) -> None:
    if token:
        with _auth_lock:
            _sessions.pop(token, None)


def lockout_left(ip: str) -> int:
    with _auth_lock:
        count, until = _failed.get(ip, (0, 0.0))
    return max(0, int(until - time.time()))


def note_login(ip: str, ok: bool) -> None:
    with _auth_lock:
        if ok:
            _failed.pop(ip, None)
            return
        count, until = _failed.get(ip, (0, 0.0))
        count += 1
        if count >= MAX_FAILED:
            _failed[ip] = (0, time.time() + LOCKOUT_SECONDS)
            print(f"[netmon] for mange fejlede logins fra {ip} - spaerret i {LOCKOUT_SECONDS}s")
        else:
            _failed[ip] = (count, until)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

_db_lock = threading.Lock()
_db: sqlite3.Connection


def init_db() -> None:
    global _db
    _db = sqlite3.connect(DB_PATH, check_same_thread=False)
    _db.row_factory = sqlite3.Row
    with _db_lock:
        _db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS samples (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      REAL    NOT NULL,
                host    TEXT    NOT NULL,
                ok      INTEGER NOT NULL,
                rtt_ms  REAL,
                error   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_samples_host_ts ON samples(host, ts);
            CREATE TABLE IF NOT EXISTS outages (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                scope     TEXT    NOT NULL,   -- 'target' | 'isp' | 'lan' | 'internet'
                name      TEXT    NOT NULL,
                started   REAL    NOT NULL,
                ended     REAL,
                detail    TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_outages_started ON outages(started);
            """
        )
        _db.commit()


def db_write(sql: str, params: tuple = ()) -> int:
    with _db_lock:
        cur = _db.execute(sql, params)
        _db.commit()
        return cur.lastrowid


def db_read(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _db_lock:
        return _db.execute(sql, params).fetchall()


# --------------------------------------------------------------------------
# Ping
# --------------------------------------------------------------------------

def ping_once(host: str, count: int, timeout: float) -> tuple[bool, float | None, str | None]:
    """Returnerer (ok, rtt_ms, fejltekst)."""
    if IS_WINDOWS:
        cmd = ["ping", "-n", str(count), "-w", str(int(timeout * 1000)), host]
    elif IS_MAC:
        cmd = ["ping", "-n", "-c", str(count), "-W", str(int(timeout * 1000)), host]
    else:
        cmd = ["ping", "-n", "-c", str(count), "-W", str(max(1, int(round(timeout)))), host]

    hard_timeout = timeout * count + 5
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=hard_timeout, text=True, errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, None, "timeout"
    except FileNotFoundError:
        return False, None, "ping-kommandoen blev ikke fundet"
    except Exception as exc:
        return False, None, str(exc)[:200]

    out = proc.stdout or ""
    rtts = [float(m.replace(",", ".")) for m in RTT_RE.findall(out)]
    rtt = round(min(rtts), 2) if rtts else None

    if proc.returncode == 0 and rtt is not None:
        return True, rtt, None

    err = "ingen svar"
    low = out.lower()
    if "unknown host" in low or "name or service not known" in low or "could not find host" in low:
        err = "ukendt vaert"
    elif "unreachable" in low or "uopnaaelig" in low:
        err = "unreachable"
    elif "operation not permitted" in low:
        err = "ingen rettigheder til ping"
    return False, rtt, err


# --------------------------------------------------------------------------
# Monitor
# --------------------------------------------------------------------------

class TargetState:
    def __init__(self, host: str, name: str, kind: str):
        self.host = host
        self.name = name
        self.kind = kind
        self.status = "unknown"          # unknown | up | down
        self.consecutive_fail = 0
        self.consecutive_ok = 0
        self.last_ts: float | None = None
        self.last_ok: bool | None = None
        self.last_rtt: float | None = None
        self.last_error: str | None = None
        self.since: float | None = None   # hvornaar nuvaerende status startede
        self.outage_id: int | None = None
        self.history: deque = deque(maxlen=120)


class Monitor(threading.Thread):
    daemon = True

    def __init__(self):
        super().__init__(name="monitor")
        self.lock = threading.RLock()
        self.targets: dict[str, TargetState] = {}
        self.link_status = {"lan": "unknown", "isp": "unknown", "internet": "unknown"}
        self.link_outage: dict[str, int | None] = {"lan": None, "isp": None, "internet": None}
        self.link_since: dict[str, float | None] = {"lan": None, "isp": None, "internet": None}
        self.last_round: float | None = None
        self.next_round: float | None = None
        self.started_at = time.time()
        self._wake = threading.Event()
        self._last_prune = 0.0

    def wake(self) -> None:
        self._wake.set()

    # -- livscyklus -------------------------------------------------------
    def run(self) -> None:
        while True:
            cfg = get_config()
            try:
                self.run_round(cfg)
            except Exception as exc:
                print(f"[netmon] fejl i maalerunde: {exc}", file=sys.stderr)
            self.prune(cfg)
            self.next_round = time.time() + cfg["interval_seconds"]
            self._wake.wait(timeout=cfg["interval_seconds"])
            self._wake.clear()

    def sync_targets(self, cfg: dict) -> list[TargetState]:
        active: list[TargetState] = []
        wanted: set[str] = set()
        with self.lock:
            for t in cfg["targets"]:
                st = self.targets.get(t["host"])
                if st is None:
                    st = TargetState(t["host"], t["name"], t["kind"])
                    self.targets[t["host"]] = st
                st.name, st.kind = t["name"], t["kind"]
                wanted.add(t["host"])
                if t["enabled"]:
                    active.append(st)
            for host in list(self.targets):
                if host not in wanted:
                    del self.targets[host]
        return active

    def run_round(self, cfg: dict) -> None:
        active = self.sync_targets(cfg)
        if not active:
            self.last_round = time.time()
            return

        count, timeout = cfg["ping_count"], cfg["timeout_seconds"]
        with ThreadPoolExecutor(max_workers=min(16, len(active))) as pool:
            results = list(pool.map(lambda st: (st, ping_once(st.host, count, timeout)), active))

        now = time.time()
        for st, (ok, rtt, err) in results:
            self.record(st, ok, rtt, err, now, cfg)
        self.assess_link(active, now)
        self.last_round = now

    def record(self, st: TargetState, ok: bool, rtt: float | None, err: str | None,
               now: float, cfg: dict) -> None:
        db_write(
            "INSERT INTO samples(ts, host, ok, rtt_ms, error) VALUES (?,?,?,?,?)",
            (now, st.host, 1 if ok else 0, rtt, err),
        )
        with self.lock:
            st.last_ts, st.last_ok, st.last_rtt, st.last_error = now, ok, rtt, err
            st.history.append({"ts": now, "ok": ok, "rtt": rtt})
            if ok:
                st.consecutive_ok += 1
                st.consecutive_fail = 0
            else:
                st.consecutive_fail += 1
                st.consecutive_ok = 0

            if st.status != "down" and st.consecutive_fail >= cfg["fail_threshold"]:
                st.status, st.since = "down", now
                st.outage_id = db_write(
                    "INSERT INTO outages(scope, name, started, detail) VALUES (?,?,?,?)",
                    ("target", st.host, now, err or "ingen svar"),
                )
                print(f"[netmon] NEDE: {st.name} ({st.host}) - {err or 'ingen svar'}")
            elif st.status != "up" and st.consecutive_ok >= cfg["recover_threshold"]:
                was = st.status
                st.status, st.since = "up", now
                if st.outage_id is not None:
                    db_write("UPDATE outages SET ended=? WHERE id=?", (now, st.outage_id))
                    st.outage_id = None
                if was == "down":
                    print(f"[netmon] OPPE igen: {st.name} ({st.host})")

    def assess_link(self, active: list[TargetState], now: float) -> None:
        """Skelner mellem lokalt netvaerk nede og ISP/internet nede."""
        lan = [s for s in active if s.kind == "lan"]
        net = [s for s in active if s.kind == "internet"]

        def state(group: list[TargetState]) -> str:
            if not group:
                return "none"
            if any(s.status == "up" for s in group):
                return "up"
            if all(s.status == "down" for s in group):
                return "down"
            return "unknown"

        lan_s, net_s = state(lan), state(net)

        lan_down = lan_s == "down"
        if lan_s == "none":
            isp_down = False
            internet_down = net_s == "down"
        else:
            isp_down = (net_s == "down") and (lan_s == "up")
            internet_down = False

        self.set_link("lan", lan_down, now, "alle LAN-vaerter svarer ikke")
        self.set_link("isp", isp_down, now, "LAN er oppe, men ingen internet-vaerter svarer")
        self.set_link("internet", internet_down, now, "ingen internet-vaerter svarer")

    def set_link(self, key: str, is_down: bool, now: float, detail: str) -> None:
        cur = self.link_status[key]
        if is_down and cur != "down":
            self.link_status[key] = "down"
            self.link_since[key] = now
            self.link_outage[key] = db_write(
                "INSERT INTO outages(scope, name, started, detail) VALUES (?,?,?,?)",
                (key, key.upper(), now, detail),
            )
            print(f"[netmon] {key.upper()} NEDE: {detail}")
        elif not is_down and cur != "up":
            self.link_status[key] = "up"
            self.link_since[key] = now
            if self.link_outage[key] is not None:
                db_write("UPDATE outages SET ended=? WHERE id=?", (now, self.link_outage[key]))
                self.link_outage[key] = None
                print(f"[netmon] {key.upper()} OPPE igen")

    def prune(self, cfg: dict) -> None:
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        cutoff = now - cfg["retention_days"] * 86400
        db_write("DELETE FROM samples WHERE ts < ?", (cutoff,))
        db_write("DELETE FROM outages WHERE ended IS NOT NULL AND ended < ?", (cutoff,))

    # -- status til UI ----------------------------------------------------
    def snapshot(self) -> dict:
        cfg = get_config()
        now = time.time()
        day_ago = now - 86400
        enabled = {t["host"] for t in cfg["targets"] if t["enabled"]}
        out = []
        with self.lock:
            for t in cfg["targets"]:
                st = self.targets.get(t["host"])
                row = db_read(
                    "SELECT COUNT(*) n, SUM(ok) k, AVG(rtt_ms) avg FROM samples "
                    "WHERE host=? AND ts>?", (t["host"], day_ago),
                )[0]
                n, k = row["n"] or 0, row["k"] or 0
                out.append({
                    "name": t["name"],
                    "host": t["host"],
                    "kind": t["kind"],
                    "enabled": t["enabled"],
                    "status": (st.status if st else "unknown") if t["enabled"] else "paused",
                    "last_ts": st.last_ts if st else None,
                    "last_ok": st.last_ok if st else None,
                    "rtt": st.last_rtt if st else None,
                    "error": st.last_error if st else None,
                    "since": st.since if st else None,
                    "fails": st.consecutive_fail if st else 0,
                    "uptime24h": round(100.0 * k / n, 2) if n else None,
                    "avg_rtt24h": round(row["avg"], 1) if row["avg"] is not None else None,
                    "history": list(st.history) if st else [],
                })
        return {
            "now": now,
            "targets": out,
            "link": {
                "lan": {"status": self.link_status["lan"], "since": self.link_since["lan"]},
                "isp": {"status": self.link_status["isp"], "since": self.link_since["isp"]},
                "internet": {"status": self.link_status["internet"], "since": self.link_since["internet"]},
            },
            "monitor": {
                "last_round": self.last_round,
                "next_round": self.next_round,
                "started_at": self.started_at,
                "active_targets": len(enabled),
            },
            "config": cfg,
        }


MON = Monitor()


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "netmon"

    def log_message(self, fmt, *args):  # stille som standard
        pass

    # -- hjaelpere --------------------------------------------------------
    def send_json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        cookie = getattr(self, "_set_cookie", None)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def session_token(self) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            return SimpleCookie(raw).get(SESSION_COOKIE).value  # type: ignore[union-attr]
        except Exception:
            return None

    def client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "?"

    def authed(self) -> bool:
        return valid_session(self.session_token())

    def require_auth(self) -> bool:
        """True hvis kaldet maa fortsaette; sender ellers 401."""
        if self.authed():
            return True
        self.send_json({"error": "login kraeves", "auth_required": True}, 401)
        return False

    def send_session_cookie(self, token: str | None) -> None:
        self._set_cookie = (
            f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"
            if token else
            f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
        )

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1_000_000:
            raise ValueError("tom eller for stor anmodning")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # -- ruter ------------------------------------------------------------
    def do_GET(self):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        try:
            if path in ("/", "/index.html"):
                return self.serve_static("index.html")
            if path.startswith("/static/"):
                return self.serve_static(path[len("/static/"):])
            if path == "/api/status":
                return self.send_json(MON.snapshot())
            if path == "/api/session":
                return self.send_json({"authed": self.authed(),
                                       "lockout": lockout_left(self.client_ip())})
            if path == "/api/config":
                return self.send_json(get_config())
            if path == "/api/outages":
                return self.send_json({"outages": self.outages(qs)})
            if path == "/api/history":
                return self.send_json({"samples": self.history(qs)})
            if path == "/api/export.csv":
                return self.export_csv(qs)
            self.send_json({"error": "ukendt endpoint"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/login":
                return self.login()
            if path == "/api/logout":
                drop_session(self.session_token())
                self.send_session_cookie(None)
                return self.send_json({"ok": True, "authed": False})
            if path == "/api/config":
                if not self.require_auth():
                    return
                cfg = set_config(self.read_json())
                MON.wake()
                print(f"[netmon] indstillinger aendret fra {self.client_ip()}")
                return self.send_json({"ok": True, "config": cfg})
            if path == "/api/check-now":
                MON.wake()
                return self.send_json({"ok": True})
            if path == "/api/clear-log":
                if not self.require_auth():
                    return
                db_write("DELETE FROM outages WHERE ended IS NOT NULL", ())
                return self.send_json({"ok": True})
            if path == "/api/set-password":
                if not self.require_auth():
                    return
                body = self.read_json()
                if not check_password(str(body.get("current", ""))):
                    return self.send_json({"error": "forkert nuvaerende adgangskode"}, 403)
                set_password(str(body.get("new", "")))
                self.send_session_cookie(None)
                print(f"[netmon] adgangskode aendret fra {self.client_ip()}")
                return self.send_json({"ok": True, "authed": False})
            self.send_json({"error": "ukendt endpoint"}, 404)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def login(self) -> None:
        ip = self.client_ip()
        left = lockout_left(ip)
        if left:
            return self.send_json(
                {"error": f"for mange forsoeg - proev igen om {left} sekunder"}, 429)
        try:
            password = str(self.read_json().get("password", ""))
        except Exception:
            password = ""
        ok = bool(password) and check_password(password)
        note_login(ip, ok)
        if not ok:
            time.sleep(0.5)          # bremser gaetteforsoeg
            return self.send_json({"error": "forkert adgangskode"}, 401)
        self.send_session_cookie(new_session())
        self.send_json({"ok": True, "authed": True})

    # -- dataudtraek ------------------------------------------------------
    def outages(self, qs) -> list[dict]:
        limit = min(int(qs.get("limit", ["200"])[0]), 2000)
        scope = qs.get("scope", ["all"])[0]
        sql = "SELECT * FROM outages"
        params: tuple = ()
        if scope in ("target", "isp", "lan", "internet"):
            sql += " WHERE scope=?"
            params = (scope,)
        elif scope == "link":
            sql += " WHERE scope IN ('isp','lan','internet')"
        sql += " ORDER BY started DESC LIMIT ?"
        rows = db_read(sql, params + (limit,))
        return [
            {
                "id": r["id"], "scope": r["scope"], "name": r["name"],
                "started": r["started"], "ended": r["ended"], "detail": r["detail"],
                "duration": (r["ended"] - r["started"]) if r["ended"] else None,
            }
            for r in rows
        ]

    def history(self, qs) -> list[dict]:
        host = qs.get("host", [""])[0]
        hours = min(float(qs.get("hours", ["24"])[0]), 24 * 365)
        limit = min(int(qs.get("limit", ["1000"])[0]), 20000)
        if not host:
            raise ValueError("host mangler")
        rows = db_read(
            "SELECT ts, ok, rtt_ms, error FROM samples WHERE host=? AND ts>? "
            "ORDER BY ts DESC LIMIT ?",
            (host, time.time() - hours * 3600, limit),
        )
        return [{"ts": r["ts"], "ok": bool(r["ok"]), "rtt": r["rtt_ms"], "error": r["error"]}
                for r in reversed(rows)]

    def export_csv(self, qs) -> None:
        lines = ["scope,name,start,slut,varighed_sekunder,detalje"]
        for o in self.outages(qs):
            start = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(o["started"]))
            end = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(o["ended"])) if o["ended"] else ""
            dur = f'{o["duration"]:.0f}' if o["duration"] is not None else ""
            detail = (o["detail"] or "").replace('"', "'")
            lines.append(f'{o["scope"]},{o["name"]},{start},{end},{dur},"{detail}"')
        self.send_bytes("\n".join(lines).encode("utf-8"), "text/csv; charset=utf-8")

    def serve_static(self, rel: str) -> None:
        safe = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not safe.startswith(STATIC_DIR) or not os.path.isfile(safe):
            return self.send_json({"error": "ikke fundet"}, 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(safe)[1], "application/octet-stream")
        with open(safe, "rb") as fh:
            self.send_bytes(fh.read(), ctype)


def main() -> None:
    ap = argparse.ArgumentParser(description="Netvaerks-/ISP-overvaagning med web-UI")
    ap.add_argument("--host", default="127.0.0.1", help="lytte-adresse (0.0.0.0 for hele nettet)")
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--set-password", action="store_true",
                    help="saet ny adgangskode til indstillinger og afslut")
    args = ap.parse_args()

    if args.set_password:
        pw = getpass.getpass("Ny adgangskode: ")
        if pw != getpass.getpass("Gentag: "):
            sys.exit("adgangskoderne er ikke ens")
        set_password(pw)
        print(f"[netmon] adgangskode gemt i {AUTH_PATH}")
        return

    if not os.path.exists(AUTH_PATH):
        sys.exit(f"[netmon] ingen adgangskode sat - koer: python3 netmon.py --set-password")

    sys.stdout.reconfigure(line_buffering=True)

    load_config()
    init_db()
    MON.start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    shown = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    print(f"[netmon] web-UI: http://{shown}:{args.port}")
    print(f"[netmon] database: {DB_PATH}")
    print("[netmon] indstillinger er beskyttet med adgangskode")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[netmon] stopper")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
