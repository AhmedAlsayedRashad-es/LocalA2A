"""Team A2A admin MCP server (stdio). Lets an admin's Claude talk to colleagues' Claude via the hub.

Env:  A2A_HUB_URL (default http://127.0.0.1:8765), A2A_ADMIN_TOKEN (from `python hub.py add-admin NAME`)
Python 3.8+, standard library only.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

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
