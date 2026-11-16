"""Team A2A hub: routes admin requests to colleague PCs and keeps the audit log.

Run:            python hub.py
Add a PC:       python hub.py add-pc PC-NAME --groups revit,cad --hub-url http://HUB-IP:8765
Add an admin:   python hub.py add-admin NAME
Update key:     python hub.py update-key            (public key the receivers pin)
Release:        python hub.py release --version 1.3 [--source ../receiver]
SSH:            python hub.py ssh-add-key ADMIN "ssh-ed25519 AAAA..."   ssh-remove-key ADMIN   ssh-cidr 192.168.1.0/24
Python 3.8+, standard library only (plus receiver/a2a_sign.py and receiver/a2a_util.py).
"""
import argparse
import hashlib
import hmac
import json
import ntpath
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

if getattr(sys, "frozen", False):  # PyInstaller exe
    HERE = os.path.dirname(sys.executable)
    ASSETS = getattr(sys, "_MEIPASS", HERE)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    ASSETS = HERE
CONFIG_PATH = os.environ.get("A2A_HUB_CONFIG", os.path.join(HERE, "hub.json"))
# Shared with the receiver: Ed25519 signing and file-name checks (receiver/a2a_sign.py, a2a_util.py).
for _p in (HERE, os.path.join(os.path.dirname(HERE), "receiver")):
    if _p not in sys.path:
        sys.path.append(_p)
import a2a_direct  # noqa: E402
import a2a_sign  # noqa: E402
import a2a_ssh  # noqa: E402
from a2a_util import canonical, safe_name, version_tuple  # noqa: E402
ROLES = ("user", "admin")

# A PC is "online" when it sent a heartbeat or poll in this many seconds.
ONLINE_S = 90
POLL_WAIT_S = 20
FINAL = ("done", "failed", "canceled", "needs_input")
MAX_LIMITS_JSON = 4096  # plan limits a receiver reports (5-hour / weekly windows)


def _load_limits(text):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def now():
    return time.time()


def iso(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None


# ---------------------------------------------------------------- config

def load_config(path=None):
    path = path or CONFIG_PATH
    if not os.path.exists(path):
        cfg = {"port": 8765, "bind": "0.0.0.0", "admins": {}, "pcs": {},
               "data_dir": "data", "report_hour": 7, "task_timeout_s": 3600}
        save_config(cfg, path)
    with open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    cfg.setdefault("port", 8765)
    cfg.setdefault("bind", "0.0.0.0")
    cfg.setdefault("admins", {})
    cfg.setdefault("pcs", {})
    cfg.setdefault("data_dir", "data")
    cfg.setdefault("report_hour", 7)
    cfg.setdefault("task_timeout_s", 3600)
    cfg.setdefault("ssh_keys", {})  # admin name -> OpenSSH public key
    cfg.setdefault("ssh_allowed_cidr", "")  # network the PCs accept SSH from (the admins' LAN)
    if not cfg.get("join_code"):
        cfg["join_code"] = secrets.token_urlsafe(9)
        save_config(cfg, path)
    for pc in cfg["pcs"].values():
        pc.setdefault("role", "user")
        pc.setdefault("groups", [])
        pc.setdefault("approved", True)  # PCs made by hand (add-pc) need no approval
    cfg["_path"] = path
    return cfg


def save_config(cfg, path=None):
    path = path or cfg.get("_path") or CONFIG_PATH
    out = {k: v for k, v in cfg.items() if not k.startswith("_")}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, path)


# ---------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS contexts (
  id TEXT PRIMARY KEY, pc TEXT NOT NULL, admin TEXT NOT NULL, title TEXT,
  session_id TEXT, broadcast_id TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY, context_id TEXT NOT NULL, pc TEXT NOT NULL, admin TEXT NOT NULL,
  message TEXT NOT NULL, status TEXT NOT NULL, output TEXT, error TEXT,
  cost_usd REAL, cancel INTEGER DEFAULT 0,
  created REAL NOT NULL, started REAL, finished REAL);
CREATE INDEX IF NOT EXISTS tasks_pc_status ON tasks(pc, status);
CREATE INDEX IF NOT EXISTS tasks_context ON tasks(context_id);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL,
  event TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS pcs_seen (
  pc TEXT PRIMARY KEY, last_seen REAL, busy_task TEXT, host TEXT, user TEXT, version TEXT);
CREATE TABLE IF NOT EXISTS transfers (
  id TEXT PRIMARY KEY, src TEXT NOT NULL, dst TEXT NOT NULL, name TEXT NOT NULL, size INTEGER,
  sha256 TEXT, status TEXT NOT NULL, path TEXT, error TEXT, offered REAL,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS transfers_dst ON transfers(dst, status);
CREATE INDEX IF NOT EXISTS transfers_src ON transfers(src, status);
"""
TRANSFER_FINAL = ("delivered", "failed", "expired")
OFFER_AGAIN_S = 300  # a file or request offered to a PC is offered again after this long
CHUNK = 65536


def data_dir_of(cfg):
    return os.path.join(os.path.dirname(cfg["_path"]), cfg["data_dir"])


def signing_key(data_dir, create=True):
    """(seed, pubkey) of the update signing key in data_dir; made when missing. Never served."""
    path = os.path.join(data_dir, "update_signing_key.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            k = json.load(f)
        return bytes.fromhex(k["seed"]), bytes.fromhex(k["pubkey"])
    if not create:
        return None, None
    os.makedirs(data_dir, exist_ok=True)
    seed = a2a_sign.generate_seed()
    pub = a2a_sign.public_key(seed)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"seed": seed.hex(), "pubkey": pub.hex()}, f)
    os.replace(tmp, path)
    return seed, pub


class Store:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.db = sqlite3.connect(os.path.join(data_dir, "hub.db"), check_same_thread=False, timeout=30)
        # WAL: fewer disk syncs, and a virus scanner or backup tool that reads the file blocks less.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(pcs_seen)").fetchall()}
        for col, typ in (("ip", "TEXT"), ("direct_port", "INTEGER"),  # hub.db from an older version
                         ("limits", "TEXT")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE pcs_seen ADD COLUMN {col} {typ}")
        self.db.commit()
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.audit_path = os.path.join(data_dir, "audit.jsonl")

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def x(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur.rowcount

    def audit(self, actor, event, detail):
        ts = now()
        d = json.dumps(detail, ensure_ascii=False)
        with self.lock:
            self.db.execute("INSERT INTO audit(ts,actor,event,detail) VALUES(?,?,?,?)",
                            (ts, actor, event, d))
            self.db.commit()
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": iso(ts), "actor": actor, "event": event,
                                    "detail": detail}, ensure_ascii=False) + "\n")

    def notify(self):
        with self.cond:
            self.cond.notify_all()


# ---------------------------------------------------------------- hub logic

class NoDirectRoute(Exception):
    """The PC cannot be reached directly now (HTTP 409): the sender uses the hub relay."""


class Hub:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = Store(os.path.join(os.path.dirname(cfg["_path"]), cfg["data_dir"]))
        self.cfg_lock = threading.Lock()
        # Tasks that were running when the hub stopped cannot report back: fail them.
        n = self.store.x("UPDATE tasks SET status='failed', error='hub restarted while running', "
                         "finished=? WHERE status='running'", (now(),))
        if n:
            self.store.audit("hub", "recover", {"failed_running_tasks": n})
        self.update_pubkey = signing_key(self.store.data_dir)[1].hex()
        self._release_cache = (None, None)

    def maybe_reload(self):
        """Load hub.json again when add-pc / add-admin / remove changed it."""
        try:
            m = os.path.getmtime(self.cfg["_path"])
        except OSError:
            return
        if m != getattr(self, "_cfg_mtime", None):
            with self.cfg_lock:
                try:
                    fresh = load_config(self.cfg["_path"])
                except (ValueError, OSError) as e:
                    if getattr(self, "_bad_mtime", None) != m:
                        self._bad_mtime = m
                        print(f"hub.json is not valid, the old config stays: {e}", flush=True)
                        self.store.audit("hub", "config_error", {"error": str(e)})
                    return
                self._take(fresh)
                if getattr(self, "_cfg_mtime", None) is not None:
                    self.store.audit("hub", "config_reload", {"pcs": len(fresh["pcs"]),
                                                              "admins": len(fresh["admins"])})
                self._cfg_mtime = m

    LIVE_KEYS = ("admins", "pcs", "join_code", "ssh_keys", "ssh_allowed_cidr")

    def _take(self, fresh):
        """Use the parts of a freshly read hub.json that may change while the hub runs."""
        for k in self.LIVE_KEYS:
            self.cfg[k] = fresh[k]

    def mutate(self, fn):
        """Read hub.json from disk, apply fn(cfg), write it, and use the result.
        Reading first keeps edits made by the CLI; the dicts are replaced, never changed in place,
        so threads that loop over the old ones are safe."""
        with self.cfg_lock:
            fresh = load_config(self.cfg["_path"])
            result = fn(fresh)
            save_config(fresh)
            self._take(fresh)
            self._cfg_mtime = os.path.getmtime(self.cfg["_path"])
            return result

    # ---- membership (join code, roles)
    def enroll(self, code, name, host=None, user=None):
        if not code or not hmac.compare_digest(str(self.cfg["join_code"]), str(code)):
            raise PermissionError("wrong join code")
        base = "".join(c for c in (name or host or "pc") if c.isascii() and (c.isalnum() or c in "-_."))
        base = base[:40] or "pc"
        token = secrets.token_urlsafe(24)

        def add(cfg):
            final, i = base, 2
            while final in cfg["pcs"]:
                final, i = f"{base}-{i}", i + 1
            pcs = dict(cfg["pcs"])
            pcs[final] = {"token": token, "user": user or "", "groups": [], "role": "user",
                          "approved": False, "host": host or ""}
            cfg["pcs"] = pcs
            return final
        final = self.mutate(add)
        self.store.audit(final, "enroll", {"host": host, "user": user})
        return {"name": final, "token": token, "role": "pending", "update_pubkey": self.update_pubkey}

    def approve(self, owner, name):
        self.mutate(lambda cfg: self._edit_pc(cfg, name, approved=True))
        self.store.audit(owner, "approve", {"pc": name})
        return {"ok": True, "pc": name, "approved": True}

    def approved(self, pc):
        return bool(self.cfg["pcs"].get(pc, {}).get("approved", True))

    def set_role(self, owner, name, role):
        if role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}")
        self.mutate(lambda cfg: self._edit_pc(cfg, name, role=role))
        self.store.audit(owner, "set_role", {"pc": name, "role": role})
        return {"ok": True, "pc": name, "role": role}

    def set_groups(self, owner, name, groups):
        if isinstance(groups, str):
            groups = groups.split(",")
        clean = sorted({g.strip() for g in groups if g and g.strip()})
        self.mutate(lambda cfg: self._edit_pc(cfg, name, groups=clean))
        self.store.audit(owner, "set_groups", {"pc": name, "groups": clean})
        return {"ok": True, "pc": name, "groups": clean}

    def remove_pc(self, owner, name):
        def rm(cfg):
            if name not in cfg["pcs"]:
                raise ValueError(f"unknown PC '{name}'")
            cfg["pcs"] = {k: v for k, v in cfg["pcs"].items() if k != name}
        self.mutate(rm)
        self.store.x("UPDATE tasks SET status='canceled', cancel=1, finished=? "
                     "WHERE pc=? AND status='queued'", (now(), name))
        self.store.audit(owner, "remove_pc", {"pc": name})
        return {"ok": True, "pc": name}

    def rotate_join_code(self, owner):
        code = secrets.token_urlsafe(9)
        self.mutate(lambda cfg: cfg.__setitem__("join_code", code))
        self.store.audit(owner, "rotate_join_code", {})
        return {"ok": True, "join_code": self.cfg["join_code"]}

    @staticmethod
    def _edit_pc(cfg, name, **changes):
        if name not in cfg["pcs"]:
            raise ValueError(f"unknown PC '{name}'")
        pcs = dict(cfg["pcs"])
        pcs[name] = {**pcs[name], **changes}
        cfg["pcs"] = pcs

    def role_of(self, pc):
        """'pending' until the owner approves the PC, then 'user' or 'admin'."""
        if not self.approved(pc):
            return "pending"
        return self.cfg["pcs"].get(pc, {}).get("role", "user")

    # ---- auth
    def who(self, token):
        """Return ('admin', name) or ('pc', name) or (None, None)."""
        self.maybe_reload()
        if not token:
            return None, None
        for name, t in list(self.cfg["admins"].items()):
            if hmac.compare_digest(str(t), token):
                return "admin", name
        for name, pc in list(self.cfg["pcs"].items()):
            if hmac.compare_digest(str(pc["token"]), token):
                return "pc", name
        return None, None

    # ---- PCs
    def pcs(self):
        seen = {r["pc"]: r for r in self.store.q("SELECT * FROM pcs_seen")}
        queued = {r["pc"]: r["n"] for r in self.store.q(
            "SELECT pc, COUNT(*) n FROM tasks WHERE status='queued' GROUP BY pc")}
        running = {}
        for r in self.store.q("SELECT pc, id FROM tasks WHERE status='running' "
                              "ORDER BY started, rowid"):
            running.setdefault(r["pc"], []).append(r["id"])
        out = []
        for name, pc in sorted(self.cfg["pcs"].items()):
            s = seen.get(name, {})
            last = s.get("last_seen")
            run = running.get(name, [])
            out.append({
                "name": name, "groups": pc.get("groups", []), "user": s.get("user") or pc.get("user"),
                "host": s.get("host"), "online": bool(last and now() - last < ONLINE_S),
                "last_seen": iso(last), "busy_task": run[0] if run else None,
                "running": len(run), "running_tasks": run,
                "queued": queued.get(name, 0), "version": s.get("version"),
                "role": pc.get("role", "user"), "approved": pc.get("approved", True),
                "ip": s.get("ip"), "direct_port": s.get("direct_port") or 0,
                "limits": _load_limits(s.get("limits")),
            })
        return out

    def resolve_targets(self, to):
        """'pc-name', 'group:revit', 'all', or a list of those."""
        items = to if isinstance(to, list) else [to]
        names = []
        for item in items:
            item = str(item).strip()
            if item == "all":
                names += [n for n in self.cfg["pcs"] if self.approved(n)]
            elif item.startswith("group:"):
                g = item[6:]
                hit = [n for n, pc in self.cfg["pcs"].items()
                       if g in pc.get("groups", []) and self.approved(n)]
                if not hit:
                    raise ValueError(f"no PC in group '{g}'")
                names += hit
            elif item in self.cfg["pcs"]:
                if not self.approved(item):
                    raise ValueError(f"PC '{item}' waits for approval by the hub owner")
                names.append(item)
            else:
                raise ValueError(f"unknown PC '{item}'")
        seen, uniq = set(), []
        for n in names:
            if n not in seen:
                seen.add(n)
                uniq.append(n)
        return uniq

    # ---- send
    def send(self, admin, message, to=None, context_id=None, title=None):
        message = (message or "").strip()
        if not message:
            raise ValueError("message is empty")
        t = now()
        created = []
        if context_id:
            ctx = self.store.q("SELECT * FROM contexts WHERE id=?", (context_id,))
            if not ctx:
                raise ValueError(f"unknown context_id '{context_id}'")
            pairs = [(ctx[0]["pc"], context_id)]
            broadcast_id = ctx[0]["broadcast_id"]
        else:
            if not to:
                raise ValueError("give 'to' (PC, group:NAME, all) or 'context_id'")
            names = self.resolve_targets(to)
            broadcast_id = uuid.uuid4().hex[:12] if len(names) > 1 else None
            pairs = []
            for pc in names:
                cid = uuid.uuid4().hex[:12]
                self.store.x("INSERT INTO contexts(id,pc,admin,title,broadcast_id,created,updated) "
                             "VALUES(?,?,?,?,?,?,?)",
                             (cid, pc, admin, (title or message.splitlines()[0])[:120],
                              broadcast_id, t, t))
                pairs.append((pc, cid))
        for pc, cid in pairs:
            tid = uuid.uuid4().hex[:12]
            self.store.x("INSERT INTO tasks(id,context_id,pc,admin,message,status,created) "
                         "VALUES(?,?,?,?,?,'queued',?)", (tid, cid, pc, admin, message, t))
            self.store.x("UPDATE contexts SET updated=? WHERE id=?", (t, cid))
            created.append({"task_id": tid, "context_id": cid, "pc": pc})
        self.store.audit(admin, "send", {"to": to, "context_id": context_id,
                                         "broadcast_id": broadcast_id, "message": message,
                                         "tasks": created})
        self.store.notify()
        return {"broadcast_id": broadcast_id, "tasks": created}

    # ---- worker side
    def seen(self, pc, busy_task=None, host=None, user=None, version=None, ip=None, direct_port=None,
             limits=None):
        if direct_port is not None:
            try:
                direct_port = max(0, min(65535, int(direct_port)))
            except (TypeError, ValueError):
                direct_port = None
        # Plan limits from the receiver: a small dict, else ignored (the last good value stays).
        lim = json.dumps(limits) if isinstance(limits, dict) else None
        if lim and (len(lim) > MAX_LIMITS_JSON or not isinstance(limits.get("windows"), dict)):
            lim = None
        self.store.x("INSERT INTO pcs_seen(pc,last_seen,busy_task,host,user,version,ip,direct_port,limits) "
                     "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(pc) DO UPDATE SET last_seen=excluded.last_seen,"
                     " busy_task=excluded.busy_task, host=COALESCE(excluded.host,host),"
                     " user=COALESCE(excluded.user,user), version=COALESCE(excluded.version,version),"
                     " ip=COALESCE(excluded.ip,ip), direct_port=COALESCE(excluded.direct_port,direct_port),"
                     " limits=COALESCE(excluded.limits,limits)",
                     (pc, now(), busy_task, host, user, version, ip, direct_port, lim))

    def _claim(self, pc, slots=None, running=None):
        """Give the PC up to `slots` queued tasks (old receivers: running=None, at most 1).
        `running` lists the task ids the receiver still runs; other 'running' tasks of this PC
        were lost (receiver restarted). Only one task per thread runs at a time, in order."""
        if not self.approved(pc):
            return []  # a PC that waits for approval gets no work
        keep = None if running is None else {str(x) for x in running}
        limit = 1 if running is None else max(0, int(slots or 0))
        picked = []
        with self.store.lock:
            rows = self.store.q("SELECT * FROM tasks WHERE pc=? AND status='running'", (pc,))
            busy_ctx = set()
            for r in rows:
                if keep is not None and r["id"] in keep:
                    busy_ctx.add(r["context_id"])
                    continue
                # The receiver does not run it any more: the run was lost.
                self.store.x("UPDATE tasks SET status='failed', error='receiver restarted "
                             "while running', finished=? WHERE id=?", (now(), r["id"]))
                self.store.audit(pc, "lost", {"task_id": r["id"]})
            if limit <= 0:
                return []
            rows = self.store.q("SELECT * FROM tasks WHERE pc=? AND status='queued' "
                                "ORDER BY created, rowid", (pc,))
            for task in rows:
                if len(picked) >= limit:
                    break
                if task["context_id"] in busy_ctx:
                    continue  # the thread's earlier turn still runs (or was just picked)
                busy_ctx.add(task["context_id"])
                self.store.x("UPDATE tasks SET status='running', started=? WHERE id=?",
                             (now(), task["id"]))
                ctx = self.store.q("SELECT * FROM contexts WHERE id=?", (task["context_id"],))[0]
                picked.append({"task_id": task["id"], "context_id": task["context_id"],
                               "admin": task["admin"], "message": task["message"],
                               "session_id": ctx["session_id"],
                               "timeout_s": self.cfg["task_timeout_s"]})
        for t in picked:
            self.store.audit(pc, "start", {"task_id": t["task_id"], "context_id": t["context_id"]})
        return picked

    def next_task(self, pc, wait=POLL_WAIT_S, slots=None, running=None, **info):
        """Long poll. Return {'tasks': [...], 'files': [...], 'file_requests': [...]}."""
        self.seen(pc, None, **info)
        end = now() + wait
        with self.store.cond:
            while True:
                tasks = self._claim(pc, slots, running)
                files, reqs = self.offer_files(pc)
                if tasks or files or reqs or now() >= end:
                    return {"tasks": tasks, "files": files, "file_requests": reqs}
                self.store.cond.wait(timeout=max(0.1, end - now()))

    def heartbeat(self, pc, task_id=None, **info):
        self.seen(pc, task_id, **info)
        if task_id:
            r = self.store.q("SELECT cancel, status FROM tasks WHERE id=? AND pc=?", (task_id, pc))
            if r and (r[0]["cancel"] or r[0]["status"] == "canceled"):
                return {"cancel": True}
        return {"cancel": False}

    def result(self, pc, task_id, status, output=None, error=None, session_id=None, cost_usd=None):
        if status not in ("done", "failed", "canceled", "needs_input"):
            raise ValueError(f"bad status '{status}'")
        with self.store.lock:
            rows = self.store.q("SELECT * FROM tasks WHERE id=? AND pc=?", (task_id, pc))
            if not rows:
                raise ValueError("unknown task for this PC")
            task = rows[0]
            if session_id:
                # Save it even for a late answer, so the next turn resumes the newest session.
                self.store.x("UPDATE contexts SET session_id=?, updated=? WHERE id=?",
                             (session_id, now(), task["context_id"]))
            if task["status"] != "running":
                # Late answer after cancel, sweep or hub restart: store the output, not the status.
                self.store.x("UPDATE tasks SET output=COALESCE(output,?) WHERE id=?", (output, task_id))
                self.store.audit(pc, "late_result", {"task_id": task_id, "status": status,
                                                     "session_id": session_id})
                return {"ok": True, "ignored": True}
            if task["cancel"]:
                status = "canceled"
            self.store.x("UPDATE tasks SET status=?, output=?, error=?, cost_usd=?, finished=? "
                         "WHERE id=?", (status, output, error, cost_usd, now(), task_id))
        self.seen(pc, None)
        self.store.audit(pc, "result", {"task_id": task_id, "status": status, "output": output,
                                        "error": error, "session_id": session_id})
        self.store.notify()
        return {"ok": True}

    # ---- admin reads
    def cancel(self, admin, task_id):
        with self.store.lock:  # _claim takes the same lock, so the status cannot change here
            rows = self.store.q("SELECT status FROM tasks WHERE id=?", (task_id,))
            if not rows:
                raise ValueError("unknown task")
            st = rows[0]["status"]
            if st == "queued":
                self.store.x("UPDATE tasks SET status='canceled', cancel=1, finished=? "
                             "WHERE id=? AND status='queued'", (now(), task_id))
            elif st == "running":
                self.store.x("UPDATE tasks SET cancel=1 WHERE id=?", (task_id,))
            else:
                return {"ok": False, "status": st, "error": "task already finished"}
        self.store.audit(admin, "cancel", {"task_id": task_id, "was": st})
        return {"ok": True, "was": st}

    @staticmethod
    def _task_view(t):
        return {"task_id": t["id"], "context_id": t["context_id"], "pc": t["pc"], "admin": t["admin"],
                "message": t["message"], "status": t["status"], "output": t["output"],
                "error": t["error"], "cost_usd": t["cost_usd"], "created": iso(t["created"]),
                "started": iso(t["started"]), "finished": iso(t["finished"])}

    def thread(self, context_id):
        ctx = self.store.q("SELECT * FROM contexts WHERE id=?", (context_id,))
        if not ctx:
            raise ValueError("unknown context_id")
        c = ctx[0]
        tasks = self.store.q("SELECT * FROM tasks WHERE context_id=? ORDER BY created, rowid",
                             (context_id,))
        return {"context_id": c["id"], "pc": c["pc"], "admin": c["admin"], "title": c["title"],
                "broadcast_id": c["broadcast_id"], "has_session": bool(c["session_id"]),
                "created": iso(c["created"]), "turns": [self._task_view(t) for t in tasks]}

    def broadcast(self, broadcast_id):
        ctxs = self.store.q("SELECT id FROM contexts WHERE broadcast_id=? ORDER BY pc", (broadcast_id,))
        if not ctxs:
            raise ValueError("unknown broadcast_id")
        out = []
        for c in ctxs:
            th = self.thread(c["id"])
            last = th["turns"][-1] if th["turns"] else {}
            out.append({"pc": th["pc"], "context_id": th["context_id"], "status": last.get("status"),
                        "output": last.get("output"), "error": last.get("error")})
        return {"broadcast_id": broadcast_id, "results": out}

    def tasks(self, status=None, pc=None, since_hours=None, limit=50):
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if status:
            sql += " AND status=?"
            args.append(status)
        if pc:
            sql += " AND pc=?"
            args.append(pc)
        if since_hours:
            sql += " AND created>=?"
            args.append(now() - float(since_hours) * 3600)
        sql += " ORDER BY created DESC, rowid DESC LIMIT ?"
        args.append(int(limit))
        return [self._task_view(t) for t in self.store.q(sql, args)]

    def threads(self, limit=50):
        rows = self.store.q("SELECT * FROM contexts ORDER BY updated DESC LIMIT ?", (int(limit),))
        out = []
        for c in rows:
            last = self.store.q("SELECT status FROM tasks WHERE context_id=? "
                                "ORDER BY created DESC, rowid DESC LIMIT 1", (c["id"],))
            out.append({"context_id": c["id"], "pc": c["pc"], "admin": c["admin"], "title": c["title"],
                        "broadcast_id": c["broadcast_id"], "updated": iso(c["updated"]),
                        "status": last[0]["status"] if last else None})
        return out

    def audit_log(self, limit=100):
        rows = self.store.q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),))
        return [{"ts": iso(r["ts"]), "actor": r["actor"], "event": r["event"],
                 "detail": json.loads(r["detail"])} for r in rows]

    # ---- report
    def report(self, since_hours=16):
        since = now() - float(since_hours) * 3600
        rows = self.store.q("SELECT * FROM tasks WHERE created>=? OR finished>=? "
                            "ORDER BY pc, created", (since, since))
        groups = {k: [] for k in ("needs_input", "failed", "done", "running", "queued", "canceled")}
        answered = {r["id"] for r in self.store.q(
            "SELECT DISTINCT a.id FROM tasks a JOIN tasks b ON a.context_id=b.context_id "
            "AND b.created>a.created WHERE a.status='needs_input'")}
        for t in rows:
            key = t["status"]
            if key == "needs_input" and t["id"] in answered:
                key = "done"  # the admin already answered in this thread
            groups.setdefault(key, []).append(t)
        lines = [f"# Team A2A report — {iso(now())}",
                 f"Tasks since {iso(since)}: {len(rows)}  "
                 + "  ".join(f"{k}: {len(v)}" for k, v in groups.items() if v), ""]
        titles = {"needs_input": "Waiting for your answer", "failed": "Failed", "done": "Done",
                  "running": "Still running", "queued": "Still queued (PC offline or busy)",
                  "canceled": "Canceled"}
        for key, title in titles.items():
            items = groups.get(key) or []
            if not items:
                continue
            lines.append(f"## {title} ({len(items)})")
            for t in items:
                first = t["message"].strip().splitlines()[0][:100]
                lines.append(f"- **{t['pc']}** — {first}  (task {t['id']}, thread {t['context_id']})")
                body = (t["output"] or t["error"] or "").strip()
                if body and key != "queued":
                    short = body if len(body) <= 600 else body[:600] + " …"
                    lines.extend("  > " + ln for ln in short.splitlines())
            lines.append("")
        offline = [p["name"] for p in self.pcs() if not p["online"]]
        if offline:
            lines.append(f"Offline now: {', '.join(offline)}")
        return {"since": iso(since), "counts": {k: len(v) for k, v in groups.items()},
                "markdown": "\n".join(lines)}

    def write_daily_report(self):
        rep = self.report(since_hours=24)
        d = os.path.join(self.store.data_dir, "reports")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, datetime.now().strftime("%Y-%m-%d") + ".md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(rep["markdown"])
        self.store.audit("hub", "report", {"path": path})
        return path

    # ---- file transfer (the hub stores the file; the receiver fetches it when it polls)
    def max_transfer_bytes(self):
        return int(float(self.cfg.get("max_transfer_mb", 200)) * 1048576)

    def transfer_file(self, tid):
        return os.path.join(self.store.data_dir, "transfers", f"{tid}.bin")

    def check_pc_target(self, name):
        """A destination / source PC must exist and be approved (else ValueError -> HTTP 400)."""
        if name not in self.cfg["pcs"]:
            raise ValueError(f"unknown PC '{name}'")
        if not self.approved(name):
            raise ValueError(f"PC '{name}' waits for approval by the hub owner")
        return name

    @staticmethod
    def _transfer_view(t):
        return {"id": t["id"], "src": t["src"], "dst": t["dst"], "name": t["name"], "size": t["size"],
                "sha256": t["sha256"], "status": t["status"], "path": t["path"], "error": t["error"],
                "created": iso(t["created"]), "updated": iso(t["updated"])}

    def transfer(self, tid):
        rows = self.store.q("SELECT * FROM transfers WHERE id=?", (tid,))
        if not rows:
            raise LookupError("unknown transfer")
        return rows[0]

    def transfers(self, limit=50):
        rows = self.store.q("SELECT * FROM transfers ORDER BY created DESC, rowid DESC LIMIT ?",
                            (int(limit),))
        return [self._transfer_view(t) for t in rows]

    def _drop_file(self, tid):
        for path in (self.transfer_file(tid), self.transfer_file(tid) + ".part"):
            try:
                os.unlink(path)
            except OSError:
                pass

    def store_upload(self, rfile, length, sha256, tid):
        """Stream `length` bytes into the transfer file while hashing. Return (ok, error)."""
        os.makedirs(os.path.join(self.store.data_dir, "transfers"), exist_ok=True)
        part = self.transfer_file(tid) + ".part"
        h, got = hashlib.sha256(), 0
        try:
            with open(part, "wb") as f:
                while got < length:
                    chunk = rfile.read(min(CHUNK, length - got))
                    if not chunk:
                        break
                    h.update(chunk)
                    f.write(chunk)
                    got += len(chunk)
        except OSError as e:
            self._drop_file(tid)
            return False, f"upload broke: {e}"
        if got != length:
            self._drop_file(tid)
            return False, f"upload ended after {got} of {length} bytes"
        if h.hexdigest() != sha256.strip().lower():
            self._drop_file(tid)
            return False, "sha256 mismatch: the file changed on the way"
        os.replace(part, self.transfer_file(tid))
        return True, None

    def new_transfer(self, src, dst, name, size, sha256, status, path=None, tid=None):
        tid = tid or uuid.uuid4().hex[:12]
        t = now()
        self.store.x("INSERT INTO transfers(id,src,dst,name,size,sha256,status,path,created,updated) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", (tid, src, dst, name, size, sha256, status, path, t, t))
        return tid

    def set_transfer(self, tid, **changes):
        changes["updated"] = now()
        keys = sorted(changes)
        self.store.x(f"UPDATE transfers SET {', '.join(k + '=?' for k in keys)} WHERE id=?",
                     [changes[k] for k in keys] + [tid])

    def request_file(self, admin, pc, path):
        self.check_pc_target(pc)
        path = str(path or "").strip()
        if not path:
            raise ValueError("give the 'path' of the file on the PC")
        name = safe_name(ntpath.basename(path.rstrip("\\/")))
        tid = self.new_transfer(f"pc:{pc}", f"admin:{admin}", name, None, None, "requested", path)
        self.store.audit(admin, "transfer_request", {"transfer_id": tid, "pc": pc, "path": path})
        self.store.notify()
        return self._transfer_view(self.transfer(tid))

    def offer_files(self, pc):
        """Files waiting for this PC and file requests to it; marks them offered."""
        if not self.approved(pc):
            return [], []
        t, again = now(), now() - OFFER_AGAIN_S
        with self.store.lock:
            files = self.store.q(
                "SELECT * FROM transfers WHERE dst=? AND (status='stored' OR "
                "(status='offered' AND offered<?)) ORDER BY created, rowid", (f"pc:{pc}", again))
            reqs = self.store.q(
                "SELECT * FROM transfers WHERE src=? AND (status='requested' OR "
                "(status='offered_req' AND offered<?)) ORDER BY created, rowid", (f"pc:{pc}", again))
            for f in files:
                self.store.x("UPDATE transfers SET status='offered', offered=?, updated=? WHERE id=?",
                             (t, t, f["id"]))
            for r in reqs:
                self.store.x("UPDATE transfers SET status='offered_req', offered=?, updated=? WHERE id=?",
                             (t, t, r["id"]))
        return ([{"id": f["id"], "name": f["name"], "size": f["size"], "sha256": f["sha256"],
                  "src": f["src"]} for f in files],
                [{"id": r["id"], "path": r["path"], "name": r["name"]} for r in reqs])

    def direct_route(self, pc):
        """(ip, port) of a PC's direct file server, or raise NoDirectRoute."""
        row = self.store.q("SELECT ip, direct_port, last_seen FROM pcs_seen WHERE pc=?", (pc,))
        if not row or not row[0]["ip"] or not row[0]["direct_port"]:
            raise NoDirectRoute(f"'{pc}' has no direct file transfer (use the hub relay)")
        if not row[0]["last_seen"] or now() - row[0]["last_seen"] > ONLINE_S:
            raise NoDirectRoute(f"'{pc}' is offline (use the hub relay; it gets the file later)")
        return row[0]["ip"], int(row[0]["direct_port"])

    def direct_transfer(self, actor, src, body):
        """Plan a direct transfer and return a signed ticket for it. src: 'admin:NAME' or 'pc:NAME'.
        op 'put': src sends `name` to PC `to`. op 'get' (admins only): src fetches `path` from PC `pc`."""
        op = body.get("op") or "put"
        tid = uuid.uuid4().hex[:12]
        exp = now() + a2a_direct.TICKET_S
        if op == "put":
            to = str(body.get("to") or "")
            self.check_pc_target(to)
            if src == f"pc:{to}":
                raise ValueError("a PC cannot send a file to itself")
            name = safe_name(str(body.get("name") or ""))
            try:
                size = int(body.get("size"))
            except (TypeError, ValueError):
                raise ValueError("give the file 'size' in bytes")
            if size < 0:
                raise ValueError("bad size")
            if size > self.max_transfer_bytes():
                raise ValueError(f"file is larger than the hub limit ({self.cfg.get('max_transfer_mb', 200)} MB)")
            sha = str(body.get("sha256") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError("give the file 'sha256' (hex)")
            ip, port = self.direct_route(to)
            dst, host_pc = f"pc:{to}", to
            self.new_transfer(src, dst, name, size, sha, "direct", tid=tid)
            claims = {"id": tid, "op": "put", "src": src, "dst": dst, "name": name, "size": size,
                      "sha256": sha, "exp": exp}
        elif op == "get":
            if not src.startswith("admin:"):
                raise PermissionError("only admins can fetch files from a PC")
            pc = str(body.get("pc") or "")
            self.check_pc_target(pc)
            path = str(body.get("path") or "").strip()
            if not path:
                raise ValueError("give the 'path' of the file on the PC")
            name = safe_name(ntpath.basename(path.rstrip("\\/")))
            ip, port = self.direct_route(pc)
            dst, host_pc = src, pc
            self.new_transfer(f"pc:{pc}", dst, name, None, None, "direct_get", path, tid=tid)
            claims = {"id": tid, "op": "get", "src": f"pc:{pc}", "dst": dst, "path": path, "exp": exp}
        else:
            raise ValueError("op must be 'put' or 'get'")
        ticket = a2a_direct.make_ticket(self.cfg["pcs"][host_pc]["token"], claims)
        host = f"[{ip}]" if ":" in ip else ip
        self.store.audit(actor, "transfer_direct", {k: v for k, v in claims.items() if k != "exp"})
        return {"transfer_id": tid, "url": f"http://{host}:{port}/a2a/files/{tid}", "ticket": ticket,
                "expires": iso(exp)}

    def ack_file(self, pc, tid, ok, sha256=None, error=None):
        t = self.transfer(tid)
        if t["src"] == f"pc:{pc}" and t["status"] == "direct_get":
            if ok:
                self.set_transfer(tid, status="delivered", sha256=sha256, error=None)
                self.store.audit(pc, "transfer_delivered", {"transfer_id": tid, "name": t["name"], "src": t["src"],
                                                            "dst": t["dst"], "mode": "direct"})
            else:
                err = str(error or "the PC could not send the file")[:2000]
                self.set_transfer(tid, status="failed", error=err)
                self.store.audit(pc, "transfer_failed", {"transfer_id": tid, "error": err, "mode": "direct"})
            self.store.notify()
            return {"ok": True}
        if t["dst"] == f"pc:{pc}" and t["status"] in ("stored", "offered", "direct"):
            if ok and (not sha256 or str(sha256).lower() == t["sha256"]):
                self.set_transfer(tid, status="delivered", error=None)
                self._drop_file(tid)
                self.store.audit(pc, "transfer_delivered", {"transfer_id": tid, "name": t["name"],
                                                            "src": t["src"], "dst": t["dst"],
                                                            "mode": "direct" if t["status"] == "direct" else "hub"})
            else:
                err = str(error or "receiver refused the file (sha256 mismatch)")[:2000]
                self.set_transfer(tid, status="failed", error=err)
                self._drop_file(tid)
                self.store.audit(pc, "transfer_failed", {"transfer_id": tid, "error": err})
            self.store.notify()
            return {"ok": True}
        if t["src"] == f"pc:{pc}" and t["status"] in ("requested", "offered_req") and not ok:
            err = str(error or "the PC could not send the file")[:2000]
            self.set_transfer(tid, status="failed", error=err)
            self.store.audit(pc, "transfer_failed", {"transfer_id": tid, "error": err})
            return {"ok": True}
        raise ValueError(f"transfer {tid} is {t['status']}: nothing to acknowledge")

    # ---- updates (signed receiver releases)
    def releases_dir(self):
        return os.path.join(self.store.data_dir, "releases")

    def newest_release(self):
        """The release.json with the highest version whose signature checks out, or None."""
        d = self.releases_dir()
        try:
            names = sorted(os.listdir(d))
        except OSError:
            return None
        key = []
        for n in names:
            try:
                key.append((n, os.path.getmtime(os.path.join(d, n, "release.json"))))
            except OSError:
                pass
        key = tuple(key)
        if self._release_cache[0] == key:
            return self._release_cache[1]
        best, best_v = None, None
        pub = bytes.fromhex(self.update_pubkey)
        for n, _ in key:
            try:
                with open(os.path.join(d, n, "release.json"), encoding="utf-8") as f:
                    rel = json.load(f)
                v = version_tuple(rel["manifest"]["version"])
                if not a2a_sign.verify(pub, canonical(rel["manifest"]), bytes.fromhex(rel["signature"])):
                    continue
                if not os.path.exists(os.path.join(d, n, "package.zip")):
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if best_v is None or v > best_v:
                best, best_v = rel, v
        self._release_cache = (key, best)
        return best

    def update_for(self, version):
        rel = self.newest_release()
        if not rel:
            return None
        try:
            if version_tuple(rel["manifest"]["version"]) > version_tuple(version):
                return {"version": rel["manifest"]["version"]}
        except (ValueError, TypeError):
            pass
        return None

    def package_path(self, version):
        version_tuple(version)  # ValueError for anything that is not a plain version
        path = os.path.join(self.releases_dir(), version, "package.zip")
        if not os.path.exists(path):
            raise LookupError(f"no release {version}")
        return path

    # ---- SSH (key-only, forced command on the PC; see receiver/a2a_ssh.py)
    def admin_names(self):
        """Hub admins: the admin tokens plus PCs promoted to admin."""
        return set(self.cfg["admins"]) | {n for n, pc in self.cfg["pcs"].items()
                                          if pc.get("approved", True) and pc.get("role") == "admin"}

    def ssh_keys_for(self, pc):
        """Keys the PC must accept: those of current admins only. PermissionError if pending."""
        if not self.approved(pc):
            raise PermissionError("PC waits for approval by the hub owner")
        cidr = str(self.cfg.get("ssh_allowed_cidr") or "").strip()
        if not cidr:
            raise ValueError("the hub owner has not set ssh_allowed_cidr (python hub.py ssh-cidr CIDR)")
        cidr = a2a_ssh.validate_cidr(cidr)
        admins = self.admin_names()
        keys = []
        for name, key in sorted(self.cfg.get("ssh_keys", {}).items()):
            if name not in admins:
                continue
            try:
                keys.append({"name": a2a_ssh.validate_name(name), "key": a2a_ssh.validate_pubkey(key)})
            except ValueError:
                continue
        self.store.audit(pc, "ssh_keys", {"keys": [k["name"] for k in keys], "cidr": cidr})
        return {"cidr": cidr, "keys": keys}

    def ssh_log(self, pc, entry):
        keep = ("id", "event", "time", "requester", "command", "client_ip", "exit_code",
                "duration_s", "error")
        detail = {k: entry.get(k) for k in keep if k in entry}
        phases = {"ssh_start": "start", "ssh_end": "end", "ssh_refused": "refused"}
        if detail.get("event") not in phases:
            raise ValueError("bad ssh log event")
        detail["pc"] = pc
        detail["phase"] = phases[detail["event"]]
        self.store.audit(pc, "ssh_command", detail)
        return {"ok": True}

    def ssh_target(self, admin, pc, command=None):
        """Where an admin's ssh client must connect. Audits the request."""
        if pc not in self.cfg["pcs"]:
            raise ValueError(f"unknown PC '{pc}'")
        if not self.approved(pc):
            raise PermissionError(f"PC '{pc}' waits for approval by the hub owner")
        if not self.cfg.get("ssh_keys", {}).get(admin):
            raise PermissionError(f"admin '{admin}' has no SSH key on the hub "
                                  "(python hub.py ssh-add-key NAME PUBKEY)")
        row = self.store.q("SELECT ip, user FROM pcs_seen WHERE pc=?", (pc,))
        ip = row[0]["ip"] if row else None
        if not ip:
            raise RuntimeError(f"the hub does not know the address of '{pc}' yet (it never polled)")
        user = (row[0]["user"] or self.cfg["pcs"][pc].get("user") or "").strip()
        self.store.audit(admin, "ssh_request", {"pc": pc, "ip": ip, "user": user, "command": command})
        return {"pc": pc, "ip": ip, "user": user}

    # ---- housekeeping
    def sweep(self):
        """Fail tasks that run far past their timeout (receiver died without a word)."""
        limit = now() - self.cfg["task_timeout_s"] - 300
        rows = self.store.q("SELECT id, pc FROM tasks WHERE status='running' AND started<?", (limit,))
        for r in rows:
            self.store.x("UPDATE tasks SET status='failed', error='no result from receiver (timeout)', "
                         "finished=? WHERE id=?", (now(), r["id"]))
            self.store.audit("hub", "timeout", {"task_id": r["id"], "pc": r["pc"]})
        # Direct transfers whose ticket ran out without a result from the PC.
        rows = self.store.q("SELECT id FROM transfers WHERE status IN ('direct','direct_get') AND created<?",
                            (now() - a2a_direct.TICKET_S - 300,))
        for r in rows:
            self.set_transfer(r["id"], status="failed", error="direct transfer did not finish")
            self.store.audit("hub", "transfer_failed", {"transfer_id": r["id"], "mode": "direct",
                                                        "error": "direct transfer did not finish"})
        # Transfers nobody finished in time: expire them and free the disk.
        old = now() - float(self.cfg.get("transfer_ttl_h", 24)) * 3600
        rows = self.store.q("SELECT id FROM transfers WHERE status NOT IN ('delivered','failed','expired') "
                            "AND created<?", (old,))
        for r in rows:
            self.set_transfer(r["id"], status="expired", error="not delivered in time")
            self._drop_file(r["id"])
            self.store.audit("hub", "transfer_expired", {"transfer_id": r["id"]})


def background(hub, stop):
    last_report_day = None
    while not stop.wait(30):
        try:
            hub.sweep()
            d = datetime.now()
            if d.hour == hub.cfg["report_hour"] and last_report_day != d.date():
                hub.write_daily_report()
                last_report_day = d.date()
        except Exception as e:  # keep the hub alive
            print("background error:", e, flush=True)


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    hub = None  # set in serve()
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    MAX_BODY = 8_000_000

    def _read_body(self):
        """Read the whole body first, so the next request on a reused connection starts clean."""
        n = int(self.headers.get("Content-Length") or 0)
        if n > self.MAX_BODY:
            self.close_connection = True  # body not read: do not reuse this connection
            self._raw = None
            return
        self._raw = self.rfile.read(n) if n else b""

    def _body(self):
        if self._raw is None:
            raise ValueError("body too large")
        return json.loads(self._raw.decode("utf-8")) if self._raw else {}

    def _auth(self, need):
        role, name = self.hub.who(self.headers.get("X-A2A-Token", ""))
        self._owner = role == "admin"
        if need == "admin" and role == "pc" and self.hub.role_of(name) == "admin":
            return name  # a PC promoted to admin on the hub
        if role != need:
            self._send(401, {"ok": False, "error": "bad token"})
            return None
        return name

    def _need_owner(self):
        if not self._owner:
            self._send(403, {"ok": False, "error": "only the hub owner can do this"})
            return False
        return True

    # ---- files: streamed, never held in memory
    def _send_path(self, path, headers=None):
        size = os.path.getsize(path)
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _refuse(self, code, msg):
        """Answer an upload without taking its body: read a small body, else close the connection."""
        try:
            n = int(self.headers.get("Content-Length") or -1)
        except ValueError:
            n = -1
        if 0 <= n <= CHUNK:
            self.rfile.read(n)
        else:
            self.close_connection = True
        return self._send(code, {"ok": False, "error": msg})

    @staticmethod
    def _is_upload(method, p):
        return method == "POST" and (p in ("/api/files/send", "/api/worker/files/send")
                                     or (p.startswith("/api/worker/files/") and p.endswith("/upload")))

    def _upload(self, p, qs):
        """POST /api/files/send?to=&name= (admin), /api/worker/files/send?to=&name= (PC),
        /api/worker/files/<id>/upload (PC answers a file request). Body = the file bytes,
        header X-A2A-SHA256 = its sha256."""
        h = self.hub
        role, name = h.who(self.headers.get("X-A2A-Token", ""))
        t = None
        if p == "/api/files/send":
            if not (role == "admin" or (role == "pc" and h.role_of(name) == "admin")):
                return self._refuse(401, "bad token")
            src = f"admin:{name}"
        else:
            if role != "pc":
                return self._refuse(401, "bad token")
            if h.role_of(name) == "pending":
                return self._refuse(403, "PC waits for approval by the hub owner")
            src = f"pc:{name}"
            if p != "/api/worker/files/send":
                try:
                    t = h.transfer(unquote(p.split("/")[-2]))
                except LookupError:
                    return self._refuse(404, "unknown transfer")
                if t["src"] != src or t["status"] not in ("requested", "offered_req"):
                    return self._refuse(409, f"transfer {t['id']} is {t['status']}")
        if t is None:
            to = qs.get("to", "")
            try:
                h.check_pc_target(to)
            except ValueError as e:
                return self._refuse(400, str(e))
            dst, fname = f"pc:{to}", qs.get("name", "")
        else:
            dst, fname = t["dst"], qs.get("name") or t["name"]
        try:
            safe_name(fname)
        except ValueError as e:
            return self._refuse(400, str(e))
        sha = (self.headers.get("X-A2A-SHA256") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            return self._refuse(400, "header X-A2A-SHA256 (sha256 of the file, hex) is missing")
        cl = self.headers.get("Content-Length")
        if cl is None:
            self.close_connection = True
            return self._send(411, {"ok": False, "error": "Content-Length is required"})
        try:
            length = int(cl)
        except ValueError:
            length = -1
        if length < 0:
            self.close_connection = True
            return self._send(400, {"ok": False, "error": "bad Content-Length"})
        if length > h.max_transfer_bytes():
            self.close_connection = True  # the body is never read
            return self._send(413, {"ok": False, "error": f"file is larger than the hub limit "
                                    f"({h.cfg.get('max_transfer_mb', 200)} MB)"})
        tid = t["id"] if t else uuid.uuid4().hex[:12]
        ok, err = h.store_upload(self.rfile, length, sha, tid)
        if not ok:
            h.store.audit(name, "transfer_rejected", {"transfer_id": tid, "src": src, "dst": dst,
                                                      "name": fname, "size": length, "error": err})
            if "ended after" in err:
                self.close_connection = True
            return self._send(400, {"ok": False, "error": err})
        if t is None:
            h.new_transfer(src, dst, fname, length, sha, "stored", tid=tid)
        else:
            h.set_transfer(tid, status="stored", size=length, sha256=sha, name=fname, error=None)
        h.store.audit(name, "transfer_upload", {"transfer_id": tid, "src": src, "dst": dst,
                                                "name": fname, "size": length, "sha256": sha})
        h.store.notify()
        return self._send(200, {"ok": True, "transfer_id": tid, "status": "stored"})

    def _route(self, method):
        u = urlparse(self.path)
        qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
        p = u.path.rstrip("/") or "/"
        h = self.hub
        if self._is_upload(method, p):
            try:
                return self._upload(p, qs)
            except Exception as e:
                self.close_connection = True
                return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        self._read_body()
        try:
            if method == "GET" and p in ("/", "/dashboard"):
                with open(os.path.join(ASSETS, "dashboard.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if method == "GET" and p == "/health":
                return self._send(200, {"ok": True, "service": "team-a2a-hub"})

            if method == "POST" and p == "/api/enroll":
                b = self._body()
                try:
                    return self._send(200, {"ok": True, **h.enroll(b.get("join_code"), b.get("name"),
                                                                   b.get("host"), b.get("user"))})
                except PermissionError as e:
                    return self._send(403, {"ok": False, "error": str(e)})

            # ---- receiver endpoints
            if p.startswith("/api/worker/"):
                pc = self._auth("pc")
                if not pc:
                    return
                body = self._body() if method == "POST" else {}
                info = {k: body.get(k) for k in ("host", "user", "version", "direct_port", "limits")}
                info["ip"] = self.client_address[0]
                if p == "/api/worker/next" and method == "POST":
                    wait = min(float(body.get("wait", POLL_WAIT_S)), 60)
                    slots = body.get("slots")
                    running = body.get("running") if slots is not None else None
                    if slots is not None and running is None:
                        running = []
                    got = h.next_task(pc, wait=wait, slots=slots, running=running, **info)
                    out = {"ok": True, "task": got["tasks"][0] if got["tasks"] else None,
                           "role": h.role_of(pc), "files": got["files"],
                           "file_requests": got["file_requests"],
                           "update": h.update_for(body.get("version"))}
                    if slots is not None:
                        out["tasks"] = got["tasks"]
                    return self._send(200, out)
                if p == "/api/worker/files/direct" and method == "POST":
                    try:
                        if h.role_of(pc) == "pending":
                            raise PermissionError("PC waits for approval by the hub owner")
                        return self._send(200, {"ok": True, **h.direct_transfer(pc, f"pc:{pc}", body)})
                    except PermissionError as e:
                        return self._send(403, {"ok": False, "error": str(e)})
                    except NoDirectRoute as e:
                        return self._send(409, {"ok": False, "error": str(e)})
                if p.startswith("/api/worker/files/"):
                    parts = p.split("/")  # /api/worker/files/ID[/ack]
                    tid = unquote(parts[4]) if len(parts) > 4 else ""
                    if method == "GET" and len(parts) == 5:
                        t = h.transfer(tid)
                        if t["dst"] != f"pc:{pc}" or t["status"] not in ("stored", "offered"):
                            return self._send(404, {"ok": False, "error": "no such file for this PC"})
                        return self._send_path(h.transfer_file(tid), {
                            "X-A2A-SHA256": t["sha256"], "X-A2A-Name": quote(t["name"])})
                    if method == "POST" and len(parts) == 6 and parts[5] == "ack":
                        return self._send(200, h.ack_file(pc, tid, bool(body.get("ok")),
                                                          body.get("sha256"), body.get("error")))
                if p == "/api/worker/ssh_keys" and method == "POST":
                    try:
                        return self._send(200, {"ok": True, **h.ssh_keys_for(pc)})
                    except PermissionError as e:
                        return self._send(403, {"ok": False, "error": str(e)})
                if p == "/api/worker/ssh_log" and method == "POST":
                    return self._send(200, h.ssh_log(pc, body))
                if p == "/api/worker/heartbeat" and method == "POST":
                    return self._send(200, {"ok": True, **h.heartbeat(pc, body.get("task_id"), **info)})
                if p == "/api/worker/result" and method == "POST":
                    return self._send(200, h.result(pc, body["task_id"], body["status"],
                                                    body.get("output"), body.get("error"),
                                                    body.get("session_id"), body.get("cost_usd")))
                return self._send(404, {"ok": False, "error": "not found"})

            # ---- updates: any approved PC or admin
            if p.startswith("/api/update/") and method == "GET":
                role, who = h.who(self.headers.get("X-A2A-Token", ""))
                if role is None:
                    return self._send(401, {"ok": False, "error": "bad token"})
                if p == "/api/update/key":  # public key only; members that joined before updates pin it
                    return self._send(200, {"ok": True, "update_pubkey": h.update_pubkey})
                if role == "pc" and h.role_of(who) == "pending":
                    return self._send(403, {"ok": False, "error": "PC waits for approval by the hub owner"})
                if p == "/api/update/manifest":
                    return self._send(200, {"ok": True, "release": h.newest_release()})
                if p == "/api/update/package":
                    return self._send_path(h.package_path(qs.get("version", "")))
                return self._send(404, {"ok": False, "error": "not found"})

            # ---- admin endpoints
            if p.startswith("/api/"):
                admin = self._auth("admin")
                if not admin:
                    return
                if p == "/api/files" and method == "GET":
                    return self._send(200, {"ok": True, "transfers": h.transfers(qs.get("limit", 50))})
                if p == "/api/files/direct" and method == "POST":
                    try:
                        return self._send(200, {"ok": True, **h.direct_transfer(admin, f"admin:{admin}",
                                                                                self._body())})
                    except PermissionError as e:
                        return self._send(403, {"ok": False, "error": str(e)})
                    except NoDirectRoute as e:
                        return self._send(409, {"ok": False, "error": str(e)})
                if p == "/api/files/request" and method == "POST":
                    b = self._body()
                    return self._send(200, {"ok": True, "transfer": h.request_file(
                        admin, b.get("pc", ""), b.get("path", ""))})
                if p.startswith("/api/files/") and method == "GET":
                    parts = p.split("/")  # /api/files/ID[/download]
                    t = h.transfer(unquote(parts[3]))
                    if len(parts) == 4:
                        return self._send(200, {"ok": True, "transfer": h._transfer_view(t)})
                    if len(parts) == 5 and parts[4] == "download":
                        if t["dst"] != f"admin:{admin}" and not self._owner:
                            return self._send(403, {"ok": False, "error": "this file is for another admin"})
                        if t["status"] != "stored":
                            return self._send(409, {"ok": False, "error": f"transfer is {t['status']}"})
                        self._send_path(h.transfer_file(t["id"]), {
                            "X-A2A-SHA256": t["sha256"], "X-A2A-Name": quote(t["name"])})
                        h.set_transfer(t["id"], status="delivered")
                        h._drop_file(t["id"])
                        h.store.audit(admin, "transfer_delivered", {"transfer_id": t["id"], "name": t["name"],
                                                                    "src": t["src"], "dst": t["dst"]})
                        return
                if method == "POST" and p == "/api/ssh/target":
                    b = self._body()
                    try:
                        return self._send(200, {"ok": True, **h.ssh_target(admin, b.get("pc", ""),
                                                                           b.get("command"))})
                    except PermissionError as e:
                        return self._send(403, {"ok": False, "error": str(e)})
                    except RuntimeError as e:
                        return self._send(409, {"ok": False, "error": str(e)})
                if method == "GET" and p == "/api/whoami":
                    return self._send(200, {"ok": True, "admin": admin, "owner": self._owner})
                # ---- owner only: membership
                if p.startswith("/api/pcs/") or p == "/api/joincode":
                    if not self._need_owner():
                        return
                    b = self._body() if method == "POST" else {}
                    if method == "GET" and p == "/api/joincode":
                        return self._send(200, {"ok": True, "join_code": h.cfg["join_code"]})
                    if method == "POST" and p == "/api/joincode":
                        return self._send(200, h.rotate_join_code(admin))
                    parts = p.split("/")  # /api/pcs/NAME/ACTION
                    if method == "POST" and len(parts) == 5:
                        name, action = unquote(parts[3]), parts[4]
                        if action == "role":
                            return self._send(200, h.set_role(admin, name, b.get("role")))
                        if action == "groups":
                            return self._send(200, h.set_groups(admin, name, b.get("groups", [])))
                        if action == "remove":
                            return self._send(200, h.remove_pc(admin, name))
                        if action == "approve":
                            return self._send(200, h.approve(admin, name))
                if method == "GET" and p == "/api/pcs":
                    return self._send(200, {"ok": True, "pcs": h.pcs(),
                                            "groups": sorted({g for pc in h.cfg["pcs"].values()
                                                              for g in pc.get("groups", [])})})
                if method == "POST" and p == "/api/send":
                    b = self._body()
                    return self._send(200, {"ok": True, **h.send(admin, b.get("message"), b.get("to"),
                                                                 b.get("context_id"), b.get("title"))})
                if method == "GET" and p == "/api/tasks":
                    return self._send(200, {"ok": True, "tasks": h.tasks(
                        qs.get("status"), qs.get("pc"), qs.get("since_hours"), qs.get("limit", 50))})
                if method == "GET" and p == "/api/threads":
                    return self._send(200, {"ok": True, "threads": h.threads(qs.get("limit", 50))})
                if method == "GET" and p.startswith("/api/threads/"):
                    return self._send(200, {"ok": True, **h.thread(p.split("/")[-1])})
                if method == "GET" and p.startswith("/api/broadcasts/"):
                    return self._send(200, {"ok": True, **h.broadcast(p.split("/")[-1])})
                if method == "POST" and p.startswith("/api/tasks/") and p.endswith("/cancel"):
                    return self._send(200, h.cancel(admin, p.split("/")[-2]))
                if method == "GET" and p == "/api/report":
                    return self._send(200, {"ok": True, **h.report(qs.get("since_hours", 16))})
                if method == "GET" and p == "/api/audit":
                    return self._send(200, {"ok": True, "audit": h.audit_log(qs.get("limit", 100))})
            return self._send(404, {"ok": False, "error": "not found"})
        except LookupError as e:
            if isinstance(e, KeyError):
                return self._send(400, {"ok": False, "error": str(e)})
            return self._send(404, {"ok": False, "error": str(e.args[0] if e.args else e)})
        except ValueError as e:
            return self._send(400, {"ok": False, "error": str(e)})
        except Exception as e:
            return self._send(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")


def serve(cfg):
    hub = Hub(cfg)
    Handler.hub = hub
    if os.name == "nt":
        # On Windows SO_REUSEADDR lets a second hub bind the same port and steal requests.
        ThreadingHTTPServer.allow_reuse_address = False
    srv = ThreadingHTTPServer((cfg["bind"], cfg["port"]), Handler)
    srv.daemon_threads = True
    stop = threading.Event()
    threading.Thread(target=background, args=(hub, stop), daemon=True).start()
    print(f"Team A2A hub on {cfg['bind']}:{cfg['port']}  "
          f"({len(cfg['pcs'])} PCs, {len(cfg['admins'])} admins)", flush=True)
    return srv, hub, stop


# ---------------------------------------------------------------- CLI

def make_release(cfg, version, source):
    """Sign and store a receiver update package. Return the release dict."""
    version_tuple(version)  # ValueError: not a plain dotted version
    source = os.path.abspath(source)
    rp = os.path.join(source, "receiver.py")
    if not os.path.isfile(rp):
        raise SystemExit(f"{rp} not found")
    with open(rp, encoding="utf-8") as f:
        text = f.read()
    if not re.search(r'^VERSION = "' + re.escape(version) + r'"\s*$', text, re.M):
        raise SystemExit(f'{rp} must contain VERSION = "{version}" (else the PCs update again and again)')
    dd = data_dir_of(cfg)
    out = os.path.join(dd, "releases", version)
    if os.path.exists(out):
        raise SystemExit(f"release {version} already exists: {out}")
    # *.py for script installs; *.exe (TeamA2A-Agent.exe, built from the same receiver.py) for PCs
    # installed with TeamA2A-Setup. Never *.json: receiver.json holds the PC's own token.
    names = sorted(n for n in os.listdir(source)
                   if n.lower().endswith((".py", ".exe")) and os.path.isfile(os.path.join(source, n)))
    for n in names:
        safe_name(n)
    seed, pub = signing_key(dd)
    tmp = out + ".tmp"
    if os.path.exists(tmp):
        import shutil
        shutil.rmtree(tmp)
    os.makedirs(tmp)
    files = {}
    zpath = os.path.join(tmp, "package.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            with open(os.path.join(source, n), "rb") as f:
                data = f.read()
            files[n] = hashlib.sha256(data).hexdigest()
            z.writestr(n, data)
    with open(zpath, "rb") as f:
        pkg = f.read()
    manifest = {"version": version, "sha256": hashlib.sha256(pkg).hexdigest(), "size": len(pkg),
                "files": files, "created": iso(now())}
    rel = {"manifest": manifest, "signature": a2a_sign.sign(seed, canonical(manifest)).hex()}
    with open(os.path.join(tmp, "release.json"), "w", encoding="utf-8") as f:
        json.dump(rel, f, indent=2)
    os.replace(tmp, out)
    st = Store(dd)
    st.audit("hub", "release", {"version": version, "files": sorted(files),
                                "sha256": manifest["sha256"], "size": manifest["size"]})
    st.db.close()
    return rel


def main(argv=None):
    ap = argparse.ArgumentParser(description="Team A2A hub")
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add-pc", help="register a PC and print its receiver.json")
    a.add_argument("name")
    a.add_argument("--groups", default="")
    a.add_argument("--user", default="")
    a.add_argument("--hub-url", default="http://HUB-IP:8765")
    b = sub.add_parser("add-admin", help="create an admin token")
    b.add_argument("name")
    r = sub.add_parser("remove", help="remove a PC or admin (its token stops working)")
    r.add_argument("name")
    sub.add_parser("list", help="list PCs and admins")
    j = sub.add_parser("join-code", help="show the join code for the client installer")
    j.add_argument("--new", action="store_true", help="make a new code (the old one stops working)")
    sub.add_parser("approve", help="approve a PC that joined with the join code").add_argument("name")
    sr = sub.add_parser("set-role", help="make a PC an admin or a regular user")
    sr.add_argument("name")
    sr.add_argument("role", choices=ROLES)
    sub.add_parser("update-key", help="show the public key of the update signing key")
    sk = sub.add_parser("ssh-add-key", help="set the SSH public key of an admin")
    sk.add_argument("name")
    sk.add_argument("pubkey")
    sub.add_parser("ssh-remove-key", help="remove the SSH key of an admin").add_argument("name")
    sub.add_parser("ssh-cidr", help="set the network PCs accept SSH from").add_argument("cidr")
    rl = sub.add_parser("release", help="sign and publish a receiver update")
    rl.add_argument("--version", required=True)
    rl.add_argument("--source", default=os.path.join(os.path.dirname(HERE), "receiver"),
                    help="folder with receiver.py, its modules and optionally TeamA2A-Agent.exe "
                         "(default: ..\\receiver)")
    args = ap.parse_args(argv)
    cfg = load_config()

    if args.cmd == "add-pc":
        token = secrets.token_urlsafe(24)
        cfg["pcs"][args.name] = {"token": token, "user": args.user,
                                 "groups": [g.strip() for g in args.groups.split(",") if g.strip()]}
        save_config(cfg)
        pub = signing_key(data_dir_of(cfg))[1].hex()
        print(json.dumps({"name": args.name, "hub_url": args.hub_url, "token": token,
                          "update_pubkey": pub}, indent=2))
        print(f"\nSave the JSON above as receiver\\receiver.json on {args.name}. "
              "The running hub loads it by itself.", file=sys.stderr)
    elif args.cmd == "add-admin":
        token = secrets.token_urlsafe(24)
        cfg["admins"][args.name] = token
        save_config(cfg)
        print(token)
        print(f"\nGive this token to {args.name} (admin MCP env A2A_ADMIN_TOKEN, dashboard login). "
              "The running hub loads it by itself.", file=sys.stderr)
    elif args.cmd == "remove":
        hit = cfg["pcs"].pop(args.name, None) or cfg["admins"].pop(args.name, None)
        save_config(cfg)
        print("removed" if hit else "not found")
    elif args.cmd == "list":
        for n, pc in cfg["pcs"].items():
            state = pc.get("role", "user") if pc.get("approved", True) else "PENDING"
            print(f"pc     {n:20} role={state:7} groups={','.join(pc.get('groups', []))}")
        for n in cfg["admins"]:
            print(f"admin  {n}")
    elif args.cmd == "join-code":
        if args.new:
            cfg["join_code"] = secrets.token_urlsafe(9)
            save_config(cfg)
        print(cfg["join_code"])
    elif args.cmd == "approve":
        if args.name not in cfg["pcs"]:
            raise SystemExit(f"unknown PC '{args.name}'")
        cfg["pcs"][args.name]["approved"] = True
        save_config(cfg)
        print(f"{args.name} is approved")
    elif args.cmd == "set-role":
        if args.name not in cfg["pcs"]:
            raise SystemExit(f"unknown PC '{args.name}'")
        cfg["pcs"][args.name]["role"] = args.role
        save_config(cfg)
        print(f"{args.name} is now {args.role}")
    elif args.cmd == "ssh-add-key":
        pc_admins = {n for n, pc in cfg["pcs"].items() if pc.get("role") == "admin"}
        if args.name not in cfg["admins"] and args.name not in pc_admins:
            raise SystemExit(f"'{args.name}' is not an admin on this hub")
        try:
            a2a_ssh.validate_name(args.name)
            key = a2a_ssh.validate_pubkey(args.pubkey)
        except ValueError as e:
            raise SystemExit(f"refused: {e}")
        cfg["ssh_keys"] = {**cfg.get("ssh_keys", {}), args.name: key}
        save_config(cfg)
        print(f"SSH key of {args.name} saved. PCs pick it up at their next key sync (15 min).")
    elif args.cmd == "ssh-remove-key":
        keys = dict(cfg.get("ssh_keys", {}))
        hit = keys.pop(args.name, None)
        cfg["ssh_keys"] = keys
        save_config(cfg)
        print("removed" if hit else "not found")
    elif args.cmd == "ssh-cidr":
        try:
            cfg["ssh_allowed_cidr"] = a2a_ssh.validate_cidr(args.cidr)
        except ValueError as e:
            raise SystemExit(str(e))
        save_config(cfg)
        print(f"PCs accept SSH from {cfg['ssh_allowed_cidr']} after their next key sync.")
    elif args.cmd == "update-key":
        print(signing_key(data_dir_of(cfg))[1].hex())
    elif args.cmd == "release":
        try:
            rel = make_release(cfg, args.version, args.source)
        except ValueError as e:
            raise SystemExit(str(e))
        m = rel["manifest"]
        print(f"Release {m['version']}: {len(m['files'])} files, {m['size']} bytes, sha256 {m['sha256']}")
        print(f"Stored in {os.path.join(data_dir_of(cfg), 'releases', m['version'])}. "
              "Receivers with auto_update install it when idle.")
    else:
        srv, _, stop = serve(cfg)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()


if __name__ == "__main__":
    main()
