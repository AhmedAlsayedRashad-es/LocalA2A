"""Team A2A admin MCP server (stdio). Lets an admin's Claude talk to colleagues' Claude via the hub.

Env:  A2A_HUB_URL (default http://127.0.0.1:8765), A2A_ADMIN_TOKEN (from `python hub.py add-admin NAME`)
Python 3.8+, standard library only.
"""
import hashlib
import http.client
import json
import ntpath
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote, unquote, urlparse

DIRECT_CONNECT_S = float(os.environ.get("A2A_DIRECT_CONNECT_S", "5"))  # then fall back to the hub relay

def _installed():
    """The PC's own receiver.json (installed by TeamA2A-Setup). Its token works here once the
    hub owner promotes this PC to admin."""
    path = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TeamA2A",
                        "receiver.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


_INST = {} if os.environ.get("A2A_ADMIN_TOKEN") else _installed()
HUB = (os.environ.get("A2A_HUB_URL") or _INST.get("hub_url") or "http://127.0.0.1:8765").rstrip("/")
TOKEN = os.environ.get("A2A_ADMIN_TOKEN") or _INST.get("token", "")
IN_TASK = os.environ.get("A2A_IN_TASK") == "1"  # started by a remote task: give no tools
FINAL = ("done", "failed", "canceled", "needs_input")
# Direct connections only: the Windows system proxy must not catch LAN addresses.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def hub(method, path, body=None, timeout=30):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(HUB + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-A2A-Token": TOKEN})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error")
        except Exception:
            msg = str(e)
        raise RuntimeError(f"hub said HTTP {e.code}: {msg}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach the hub at {HUB}: {e.reason}")


def wait_tasks(task_ids, wait_s):
    """Poll the hub until every task is final or wait_s passes. Return {task_id: task}."""
    end = time.time() + max(0, float(wait_s))
    latest = {}
    while True:
        for tid in task_ids:
            if tid in latest and latest[tid]["status"] in FINAL:
                continue
            ctx = latest.get(tid, {}).get("context_id") or TASK_CTX.get(tid)
            th = hub("GET", f"/api/threads/{ctx}")
            for t in th["turns"]:
                if t["task_id"] == tid:
                    latest[tid] = t
        if all(latest.get(t, {}).get("status") in FINAL for t in task_ids) or time.time() >= end:
            return latest
        time.sleep(min(5, max(0.2, end - time.time())))


TASK_CTX = {}  # task_id -> context_id, filled by send


def fmt_turn(t):
    s = f"[{t['pc']}] status={t['status']} task={t['task_id']} thread={t['context_id']}"
    if t.get("output"):
        s += f"\n{t['output']}"
    if t.get("error") and t["status"] != "done":
        s += f"\nERROR: {t['error']}"
    return s


# ---------------------------------------------------------------- tools

def t_list_pcs(a):
    r = hub("GET", "/api/pcs")
    lines = [f"Groups: {', '.join(r['groups']) or '(none)'}"]
    for p in r["pcs"]:
        state = "ONLINE" if p["online"] else "offline"
        busy = f" busy={p['busy_task']}" if p["busy_task"] else ""
        busy += f" running={p.get('running', 1 if p['busy_task'] else 0)}"
        lines.append(f"- {p['name']}: {state}{busy} queued={p['queued']} groups={','.join(p['groups'])}"
                     f" user={p['user'] or '?'} last_seen={p['last_seen'] or 'never'}")
    return "\n".join(lines)


def t_send(a):
    body = {"message": a.get("message"), "to": a.get("to"), "context_id": a.get("context_id"),
            "title": a.get("title")}
    r = hub("POST", "/api/send", body)
    for t in r["tasks"]:
        TASK_CTX[t["task_id"]] = t["context_id"]
    wait_s = float(a.get("wait_seconds", 60))
    head = f"Sent {len(r['tasks'])} task(s)."
    if r.get("broadcast_id"):
        head += f" broadcast_id={r['broadcast_id']} (use get_broadcast for all answers)"
    if wait_s <= 0:
        return head + "\n" + "\n".join(f"- {t['pc']}: task={t['task_id']} thread={t['context_id']}"
                                       for t in r["tasks"])
    got = wait_tasks([t["task_id"] for t in r["tasks"]], wait_s)
    out = [head]
    for t in r["tasks"]:
        cur = got.get(t["task_id"])
        if cur and cur["status"] in FINAL:
            out.append(fmt_turn(cur))
        else:
            st = cur["status"] if cur else "queued"
            out.append(f"[{t['pc']}] still {st} task={t['task_id']} thread={t['context_id']} "
                       "(use get_thread later; offline PCs run it when they come online)")
    return "\n\n".join(out)


def t_get_thread(a):
    th = hub("GET", f"/api/threads/{a['context_id']}")
    out = [f"Thread {th['context_id']} with {th['pc']} (started by {th['admin']} {th['created']}): "
           f"{th['title']}"]
    for t in th["turns"]:
        out.append(f"--- {t['admin']} @ {t['created']}:\n"
                   f"{t['message']}\n--- {th['pc']} [{t['status']}]"
                   + (f" @ {t['finished']}" if t["finished"] else "") + ":\n"
                   + (t["output"] or t["error"] or "(no answer yet)"))
    return "\n".join(out)


def t_get_broadcast(a):
    r = hub("GET", f"/api/broadcasts/{a['broadcast_id']}")
    return "\n\n".join(f"[{x['pc']}] status={x['status']} thread={x['context_id']}\n"
                       + (x["output"] or x["error"] or "(no answer yet)") for x in r["results"])


def t_list_tasks(a):
    qs = "&".join(f"{k}={urllib.request.quote(str(a[k]))}" for k in ("status", "pc", "since_hours", "limit")
                  if a.get(k) not in (None, ""))
    r = hub("GET", "/api/tasks" + (f"?{qs}" if qs else ""))
    if not r["tasks"]:
        return "No tasks."
    return "\n".join(f"- {t['created']} {t['pc']} [{t['status']}] task={t['task_id']} "
                     f"thread={t['context_id']}: {t['message'].splitlines()[0][:80]}" for t in r["tasks"])


def t_morning_report(a):
    return hub("GET", f"/api/report?since_hours={float(a.get('since_hours', 16))}")["markdown"]


def t_cancel(a):
    r = hub("POST", f"/api/tasks/{a['task_id']}/cancel", {})
    return json.dumps(r)


# ---- files (they go through the hub: upload, store, the PC fetches it when it polls)
CHUNK = 65536
TRANSFER_FINAL = ("delivered", "failed", "expired")


def _sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _http_error(e):
    try:
        msg = json.loads(e.read().decode("utf-8")).get("error")
    except Exception:
        msg = str(e)
    return RuntimeError(f"hub said HTTP {e.code}: {msg}")


def _wait_transfer(tid, wait_s, until):
    """Poll the transfer until its status is in `until` (or final), or wait_s passes."""
    end = time.time() + max(0, float(wait_s))
    while True:
        t = hub("GET", f"/api/files/{quote(tid)}")["transfer"]
        if t["status"] in until or t["status"] in TRANSFER_FINAL or time.time() >= end:
            return t
        time.sleep(min(1, max(0.2, end - time.time())))


def _fmt_transfer(t):
    s = (f"transfer={t['id']} {t['src']} -> {t['dst']} name={t['name']} status={t['status']}"
         f" size={t['size'] if t['size'] is not None else '?'}")
    if t.get("error"):
        s += f"\nERROR: {t['error']}"
    return s


# ---- direct mode: the hub signs a ticket, the bytes go straight between this PC and the colleague PC
class _NoDirect(Exception):
    """No direct route now: use the hub relay."""


def _direct_ticket(body):
    """Ask the hub for a direct-transfer ticket. Raises _NoDirect when the PC cannot be reached directly."""
    req = urllib.request.Request(HUB + "/api/files/direct", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json", "X-A2A-Token": TOKEN})
    try:
        with OPENER.open(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 409):  # 404: an older hub without direct mode
            raise _NoDirect(str(e))
        raise _http_error(e)
    except urllib.error.URLError as e:
        raise RuntimeError(f"cannot reach the hub at {HUB}: {e.reason}")


def _direct_conn(url):
    u = urlparse(url)
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=DIRECT_CONNECT_S)
        conn.connect()
    except OSError as e:
        raise _NoDirect(f"{u.hostname}:{u.port} did not answer: {e}")
    conn.sock.settimeout(600)
    return conn, u.path


def _direct_error(r):
    try:
        return json.loads(r.read().decode("utf-8")).get("error")
    except Exception:
        return f"HTTP {r.status}"


def _direct_put(path, to, size, digest):
    d = _direct_ticket({"op": "put", "to": to, "name": os.path.basename(path), "size": size, "sha256": digest})
    conn, upath = _direct_conn(d["url"])
    try:
        conn.putrequest("PUT", upath)
        conn.putheader("X-A2A-Ticket", d["ticket"])
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                conn.send(chunk)
        r = conn.getresponse()
        if r.status != 200:
            raise RuntimeError(f"{to} refused the file: {_direct_error(r)}")
        r.read()
    except OSError as e:
        raise RuntimeError(f"direct upload to {to} broke off: {e}")
    finally:
        conn.close()
    return d["transfer_id"]


def t_send_file(a):
    path = os.path.abspath(os.path.expanduser(str(a.get("path") or "")))
    if not os.path.isfile(path):
        raise RuntimeError(f"file not found: {path}")
    to = str(a.get("to") or "").strip()
    if not to:
        raise RuntimeError("give 'to' (the PC name)")
    size, digest = os.path.getsize(path), _sha256_file(path)
    note = ""
    try:
        tid = _direct_put(path, to, size, digest)
        t = _wait_transfer(tid, 15, ("delivered",))
        return (f"Sent {size} bytes straight to {to} (direct, sha256 {digest}). "
                f"It is in the a2a-inbox folder on {to}.\n" + _fmt_transfer(t))
    except _NoDirect as e:
        note = f" (direct not possible: {e}; sent through the hub)"
    url = f"{HUB}/api/files/send?to={quote(to)}&name={quote(os.path.basename(path))}"
    with open(path, "rb") as f:
        req = urllib.request.Request(url, data=f, method="POST", headers={
            "Content-Type": "application/octet-stream", "Content-Length": str(size),
            "X-A2A-Token": TOKEN, "X-A2A-SHA256": digest})
        try:
            with OPENER.open(req, timeout=600) as r:
                tid = json.loads(r.read().decode("utf-8"))["transfer_id"]
        except urllib.error.HTTPError as e:
            raise _http_error(e)
        except urllib.error.URLError as e:
            raise RuntimeError(f"cannot reach the hub at {HUB}: {e.reason}")
    t = _wait_transfer(tid, a.get("wait_seconds", 60), ("delivered",))
    head = f"Uploaded {size} bytes to the hub (sha256 {digest}){note}."
    if t["status"] not in TRANSFER_FINAL:
        head += f" Not delivered yet: {to} gets it when it polls the hub (see list_transfers)."
    elif t["status"] == "delivered":
        head += f" Delivered to the a2a-inbox folder on {to}."
    return head + "\n" + _fmt_transfer(t)


def _save_path(save_to, name):
    if save_to:
        dest = os.path.abspath(os.path.expanduser(save_to))
        if os.path.isdir(dest) or save_to.endswith(("/", "\\")):
            os.makedirs(dest, exist_ok=True)
            dest = os.path.join(dest, name)
        else:
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            return dest
    else:
        folder = os.path.join(os.path.expanduser("~"), "a2a-inbox")
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, name)
    stem, ext = os.path.splitext(dest)
    i = 2
    while os.path.exists(dest):
        dest, i = f"{stem} ({i}){ext}", i + 1
    return dest


def t_fetch_file(a):
    pc, path = str(a.get("pc") or "").strip(), str(a.get("path") or "").strip()
    if not pc or not path:
        raise RuntimeError("give 'pc' and 'path'")
    try:
        return _direct_get(pc, path, a.get("save_to"))
    except _NoDirect:
        pass  # through the hub
    t = hub("POST", "/api/files/request", {"pc": pc, "path": path})["transfer"]
    t = _wait_transfer(t["id"], a.get("wait_seconds", 120), ("stored",))
    if t["status"] != "stored":
        if t["status"] in TRANSFER_FINAL:
            return "The file did not come.\n" + _fmt_transfer(t)
        return (f"Still waiting: {pc} sends the file when it polls the hub. "
                f"Run fetch_file again later, or list_transfers.\n" + _fmt_transfer(t))
    dest = _save_path(a.get("save_to"), t["name"])
    part = dest + ".part"
    req = urllib.request.Request(f"{HUB}/api/files/{quote(t['id'])}/download", method="GET",
                                 headers={"X-A2A-Token": TOKEN})
    h, size = hashlib.sha256(), 0
    try:
        with OPENER.open(req, timeout=600) as r, open(part, "wb") as out:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
                size += len(chunk)
    except urllib.error.HTTPError as e:
        _unlink(part)
        raise _http_error(e)
    except Exception:
        _unlink(part)
        raise
    if h.hexdigest() != t["sha256"]:
        _unlink(part)
        raise RuntimeError("sha256 mismatch: the file was damaged on the way; nothing was saved")
    os.replace(part, dest)
    return f"Saved {size} bytes from {pc}:{path} to {dest} (sha256 {t['sha256']})."


def _direct_get(pc, path, save_to):
    d = _direct_ticket({"op": "get", "pc": pc, "path": path})
    conn, upath = _direct_conn(d["url"])
    try:
        conn.putrequest("GET", upath)
        conn.putheader("X-A2A-Ticket", d["ticket"])
        conn.endheaders()
        r = conn.getresponse()
        if r.status != 200:
            err = _direct_error(r)
            return f"The file did not come (direct from {pc}): {err}"
        want = (r.getheader("X-A2A-SHA256") or "").lower()
        name = unquote(r.getheader("X-A2A-Name") or "") or ntpath.basename(path.rstrip("\\/"))
        dest = _save_path(save_to, os.path.basename(name))
        part = dest + ".part"
        h, size = hashlib.sha256(), 0
        try:
            with open(part, "wb") as out:
                for chunk in iter(lambda: r.read(CHUNK), b""):
                    h.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
        except Exception:
            _unlink(part)
            raise
    except OSError as e:
        raise RuntimeError(f"direct download from {pc} broke off: {e}")
    finally:
        conn.close()
    if h.hexdigest() != want:
        _unlink(part)
        raise RuntimeError("sha256 mismatch: the file was damaged on the way; nothing was saved")
    os.replace(part, dest)
    return f"Saved {size} bytes from {pc}:{path} to {dest} (direct, sha256 {want})."


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# ---- SSH: the hub says where the PC is; the PC's forced command logs every command
def ssh_exe():
    if os.environ.get("A2A_SSH_EXE"):
        return os.environ["A2A_SSH_EXE"]
    found = shutil.which("ssh")
    if found:
        return found
    win = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "OpenSSH", "ssh.exe")
    if os.path.exists(win):
        return win
    raise RuntimeError("no ssh client found (install the Windows OpenSSH client, or set A2A_SSH_EXE)")


def ssh_args(key, user, host, command, connect_timeout=10):
    """Arguments after the ssh program. Key only, no prompts, no forwarding, no tty."""
    host, user = str(host), str(user)
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        raise ValueError(f"bad host '{host}'")
    if not user or user.startswith("-") or any(c.isspace() for c in user):
        raise ValueError(f"bad user '{user}'")
    return ["-T", "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", "-o", "PreferredAuthentications=publickey",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "IdentitiesOnly=yes",
            "-o", "ClearAllForwardings=yes", "-o", f"ConnectTimeout={int(connect_timeout)}",
            "-i", key, "-l", user, "--", host, command]


def t_ssh_run(a):
    pc, command = str(a.get("pc") or "").strip(), str(a.get("command") or "")
    if not pc or not command.strip():
        raise RuntimeError("give 'pc' and 'command'")
    timeout = min(3600, max(1, int(a.get("timeout", 60))))
    key = os.environ.get("A2A_SSH_KEY") or os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519")
    if not os.path.isfile(key):
        raise RuntimeError(f"no SSH private key at {key} (set A2A_SSH_KEY)")
    exe = ssh_exe()
    t = hub("POST", "/api/ssh/target", {"pc": pc, "command": command})
    args = [exe] + ssh_args(key, t["user"], t["ip"], command, min(timeout, 15))
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        return f"[{pc}] timeout after {timeout} s (the command may still run on the PC)"
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    text = f"[{pc} {t['user']}@{t['ip']}] exit={p.returncode}"
    if out.strip():
        text += "\n" + out.rstrip()
    if err.strip():
        text += "\nSTDERR:\n" + err.rstrip()
    if p.returncode == 255:
        text += "\n(exit 255 = ssh could not connect or log in: is SSH set up on that PC and the key synced?)"
    return text


def t_list_transfers(a):
    r = hub("GET", f"/api/files?limit={int(a.get('limit', 20))}")
    if not r["transfers"]:
        return "No transfers."
    return "\n".join(f"- {t['created']} " + _fmt_transfer(t) for t in r["transfers"])


TOOLS = [
    {"name": "list_pcs", "fn": t_list_pcs,
     "description": "List colleague PCs on the Team A2A hub: online or offline, busy, queued tasks, groups.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "send", "fn": t_send,
     "description": ("Send a request to the Claude Code agent on one or more colleague PCs. It runs there "
                     "automatically with full permissions, with nobody at the PC. Start a new thread with "
                     "'to', or continue a thread with 'context_id' (the remote Claude keeps the conversation). "
                     "Write a full, self-contained request: the remote agent does not see this chat. "
                     "Offline PCs get the task when they come online."),
     "inputSchema": {"type": "object", "properties": {
         "message": {"type": "string", "description": "The request for the remote Claude."},
         "to": {"description": "PC name, 'group:NAME', 'all', or a list of these. Not with context_id.",
                "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
         "context_id": {"type": "string", "description": "Continue this thread (reply to a QUESTION)."},
         "title": {"type": "string", "description": "Short thread title (optional)."},
         "wait_seconds": {"type": "number", "description": "Wait this long for answers (default 60, 0 = do not wait)."}},
         "required": ["message"]}},
    {"name": "get_thread", "fn": t_get_thread,
     "description": "Read the whole conversation of one thread with a PC.",
     "inputSchema": {"type": "object", "properties": {"context_id": {"type": "string"}},
                     "required": ["context_id"]}},
    {"name": "get_broadcast", "fn": t_get_broadcast,
     "description": "Read the latest answer from every PC of a group/all send.",
     "inputSchema": {"type": "object", "properties": {"broadcast_id": {"type": "string"}},
                     "required": ["broadcast_id"]}},
    {"name": "list_tasks", "fn": t_list_tasks,
     "description": "List recent tasks. Filter by status (queued, running, done, failed, canceled, needs_input), pc, since_hours.",
     "inputSchema": {"type": "object", "properties": {
         "status": {"type": "string"}, "pc": {"type": "string"},
         "since_hours": {"type": "number"}, "limit": {"type": "integer"}}}},
    {"name": "morning_report", "fn": t_morning_report,
     "description": "Summary of all tasks in the last hours (default 16): waiting for your answer, failed, done, still queued.",
     "inputSchema": {"type": "object", "properties": {"since_hours": {"type": "number"}}}},
    {"name": "cancel", "fn": t_cancel,
     "description": "Cancel a queued task, or stop a running one.",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}},
                     "required": ["task_id"]}},
    {"name": "send_file", "fn": t_send_file,
     "description": ("Send a file from this PC to a colleague PC. It goes straight to the PC when the PC can "
                     "be reached (direct mode), else through the hub. It arrives in the a2a-inbox folder of "
                     "the PC's work folder. Size and sha256 are checked."),
     "inputSchema": {"type": "object", "properties": {
         "to": {"type": "string", "description": "PC name on the hub."},
         "path": {"type": "string", "description": "Path of the file on this PC."},
         "wait_seconds": {"type": "number", "description": "Wait this long for delivery (default 60)."}},
         "required": ["to", "path"]}},
    {"name": "fetch_file", "fn": t_fetch_file,
     "description": ("Get a file from a colleague PC: straight from the PC when it can be reached (direct "
                     "mode), else through the hub (the PC uploads it when it polls). The file is saved here "
                     "(default: ~/a2a-inbox)."),
     "inputSchema": {"type": "object", "properties": {
         "pc": {"type": "string", "description": "PC name on the hub."},
         "path": {"type": "string", "description": "Path of the file on that PC (relative = its work folder)."},
         "save_to": {"type": "string", "description": "File or folder on this PC (optional)."},
         "wait_seconds": {"type": "number", "description": "Wait this long for the PC (default 120)."}},
         "required": ["pc", "path"]}},
    {"name": "list_transfers", "fn": t_list_transfers,
     "description": "List recent file transfers and their status (stored, offered, delivered, failed, expired).",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "ssh_run", "fn": t_ssh_run,
     "description": ("Run one cmd.exe command on a colleague PC over SSH (key only) and get its output "
                     "and exit code. The PC logs the command and the hub audits it. Needs SSH set up on "
                     "the PC (setup_ssh.ps1) and your key on the hub. No interactive shell."),
     "inputSchema": {"type": "object", "properties": {
         "pc": {"type": "string", "description": "PC name on the hub."},
         "command": {"type": "string", "description": "cmd.exe command line, for example 'ipconfig /all'."},
         "timeout": {"type": "integer", "description": "Seconds (default 60, max 3600)."}},
         "required": ["pc", "command"]}},
]
BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------- MCP stdio

def reply(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(req):
    method, mid, params = req.get("method"), req.get("id"), req.get("params") or {}
    if method == "initialize":
        return reply(mid, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "team-a2a", "version": "1.0"},
                           "instructions": ("Team A2A: talk to the Claude Code agents on colleague PCs. "
                                            "list_pcs first; send to start or continue a thread; "
                                            "morning_report for overnight results.")})
    if method == "ping":
        return reply(mid, {})
    if method == "tools/list":
        if IN_TASK:
            return reply(mid, {"tools": []})
        return reply(mid, {"tools": [{k: v for k, v in t.items() if k != "fn"} for t in TOOLS]})
    if method == "tools/call":
        tool = None if IN_TASK else BY_NAME.get(params.get("name"))
        if not tool:
            return reply(mid, error={"code": -32602, "message": f"unknown tool {params.get('name')}"})
        try:
            text = tool["fn"](params.get("arguments") or {})
            return reply(mid, {"content": [{"type": "text", "text": text}]})
        except Exception as e:
            return reply(mid, {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
    if mid is not None:  # unknown request (notifications get no reply)
        return reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError as e:
            reply(None, error={"code": -32700, "message": f"parse error: {e}"})
            continue
        handle(req)


if __name__ == "__main__":
    main()
