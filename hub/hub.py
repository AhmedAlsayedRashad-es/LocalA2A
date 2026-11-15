"""Team A2A hub: routes admin requests to colleague PCs and keeps the audit log.

Run:            python hub.py
Add a PC:       python hub.py add-pc PC-NAME --groups revit,cad --hub-url http://HUB-IP:8765
Add an admin:   python hub.py add-admin NAME
Python 3.8+, standard library only.
"""
import argparse
import hmac
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

if getattr(sys, "frozen", False):  # PyInstaller exe
    HERE = os.path.dirname(sys.executable)
    ASSETS = getattr(sys, "_MEIPASS", HERE)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    ASSETS = HERE
CONFIG_PATH = os.environ.get("A2A_HUB_CONFIG", os.path.join(HERE, "hub.json"))
ROLES = ("user", "admin")

# A PC is "online" when it sent a heartbeat or poll in this many seconds.
ONLINE_S = 90
POLL_WAIT_S = 20
FINAL = ("done", "failed", "canceled", "needs_input")


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
"""


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
                self.cfg["admins"], self.cfg["pcs"] = fresh["admins"], fresh["pcs"]
                self.cfg["join_code"] = fresh["join_code"]
                if getattr(self, "_cfg_mtime", None) is not None:
                    self.store.audit("hub", "config_reload", {"pcs": len(fresh["pcs"]),
                                                              "admins": len(fresh["admins"])})
                self._cfg_mtime = m

    def mutate(self, fn):
        """Read hub.json from disk, apply fn(cfg), write it, and use the result.
        Reading first keeps edits made by the CLI; the dicts are replaced, never changed in place,
        so threads that loop over the old ones are safe."""
        with self.cfg_lock:
            fresh = load_config(self.cfg["_path"])
            result = fn(fresh)
            save_config(fresh)
            self.cfg["admins"], self.cfg["pcs"] = fresh["admins"], fresh["pcs"]
            self.cfg["join_code"] = fresh["join_code"]
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
        return {"name": final, "token": token, "role": "pending"}

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
        out = []
        for name, pc in sorted(self.cfg["pcs"].items()):
            s = seen.get(name, {})
            last = s.get("last_seen")
            out.append({
                "name": name, "groups": pc.get("groups", []), "user": s.get("user") or pc.get("user"),
                "host": s.get("host"), "online": bool(last and now() - last < ONLINE_S),
                "last_seen": iso(last), "busy_task": s.get("busy_task"),
                "queued": queued.get(name, 0), "version": s.get("version"),
                "role": pc.get("role", "user"), "approved": pc.get("approved", True),
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
    def seen(self, pc, busy_task=None, host=None, user=None, version=None):
        self.store.x("INSERT INTO pcs_seen(pc,last_seen,busy_task,host,user,version) "
                     "VALUES(?,?,?,?,?,?) ON CONFLICT(pc) DO UPDATE SET last_seen=excluded.last_seen,"
                     " busy_task=excluded.busy_task, host=COALESCE(excluded.host,host),"
                     " user=COALESCE(excluded.user,user), version=COALESCE(excluded.version,version)",
                     (pc, now(), busy_task, host, user, version))

    def _claim(self, pc):
        if not self.approved(pc):
            return None  # a PC that waits for approval gets no work
        with self.store.lock:
            rows = self.store.q("SELECT * FROM tasks WHERE pc=? AND status='running'", (pc,))
            if rows:
                # The receiver asks for work, so it is not running anything: the old run was lost.
                for r in rows:
                    self.store.x("UPDATE tasks SET status='failed', error='receiver restarted "
                                 "while running', finished=? WHERE id=?", (now(), r["id"]))
                    self.store.audit(pc, "lost", {"task_id": r["id"]})
            rows = self.store.q("SELECT * FROM tasks WHERE pc=? AND status='queued' "
                                "ORDER BY created, rowid LIMIT 1", (pc,))
            if not rows:
                return None
            task = rows[0]
            self.store.x("UPDATE tasks SET status='running', started=? WHERE id=?", (now(), task["id"]))
            ctx = self.store.q("SELECT * FROM contexts WHERE id=?", (task["context_id"],))[0]
        self.store.audit(pc, "start", {"task_id": task["id"], "context_id": task["context_id"]})
        return {"task_id": task["id"], "context_id": task["context_id"], "admin": task["admin"],
                "message": task["message"], "session_id": ctx["session_id"],
                "timeout_s": self.cfg["task_timeout_s"]}

    def next_task(self, pc, wait=POLL_WAIT_S, **info):
        self.seen(pc, None, **info)
        end = now() + wait
        with self.store.cond:
            while True:
                task = self._claim(pc)
                if task or now() >= end:
                    return task
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

    # ---- housekeeping
    def sweep(self):
        """Fail tasks that run far past their timeout (receiver died without a word)."""
        limit = now() - self.cfg["task_timeout_s"] - 300
        rows = self.store.q("SELECT id, pc FROM tasks WHERE status='running' AND started<?", (limit,))
        for r in rows:
            self.store.x("UPDATE tasks SET status='failed', error='no result from receiver (timeout)', "
                         "finished=? WHERE id=?", (now(), r["id"]))
            self.store.audit("hub", "timeout", {"task_id": r["id"], "pc": r["pc"]})


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

    def _route(self, method):
        self._read_body()
        u = urlparse(self.path)
        qs = {k: v[-1] for k, v in parse_qs(u.query).items()}
        p = u.path.rstrip("/") or "/"
        h = self.hub
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
                info = {k: body.get(k) for k in ("host", "user", "version")}
                if p == "/api/worker/next" and method == "POST":
                    wait = min(float(body.get("wait", POLL_WAIT_S)), 60)
                    task = h.next_task(pc, wait=wait, **info)
                    return self._send(200, {"ok": True, "task": task, "role": h.role_of(pc)})
                if p == "/api/worker/heartbeat" and method == "POST":
                    return self._send(200, {"ok": True, **h.heartbeat(pc, body.get("task_id"), **info)})
                if p == "/api/worker/result" and method == "POST":
                    return self._send(200, h.result(pc, body["task_id"], body["status"],
                                                    body.get("output"), body.get("error"),
                                                    body.get("session_id"), body.get("cost_usd")))
                return self._send(404, {"ok": False, "error": "not found"})

            # ---- admin endpoints
            if p.startswith("/api/"):
                admin = self._auth("admin")
                if not admin:
                    return
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
        except (ValueError, KeyError) as e:
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
    args = ap.parse_args(argv)
    cfg = load_config()

    if args.cmd == "add-pc":
        token = secrets.token_urlsafe(24)
        cfg["pcs"][args.name] = {"token": token, "user": args.user,
                                 "groups": [g.strip() for g in args.groups.split(",") if g.strip()]}
        save_config(cfg)
        print(json.dumps({"name": args.name, "hub_url": args.hub_url, "token": token}, indent=2))
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
