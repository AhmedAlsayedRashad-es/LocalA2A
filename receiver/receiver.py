"""Team A2A receiver: runs on a colleague PC, takes tasks from the hub, runs Claude Code.

Run:  python receiver.py   or   TeamA2A-Agent.exe (installed by TeamA2A-Setup.exe)
Config: receiver.json next to the program, else %LOCALAPPDATA%\TeamA2A\receiver.json.
Python 3.8+, standard library only.
"""
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

VERSION = "1.1"
HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
# Installed by TeamA2A-Setup: %LOCALAPPDATA%\TeamA2A. A receiver.json next to the program wins.
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TeamA2A")


def _find_config():
    if os.environ.get("A2A_RECEIVER_CONFIG"):
        return os.environ["A2A_RECEIVER_CONFIG"]
    local = os.path.join(HERE, "receiver.json")
    return local if os.path.exists(local) else os.path.join(APP_DIR, "receiver.json")


CONFIG_PATH = _find_config()
LOG_DIR = os.path.join(os.path.dirname(CONFIG_PATH), "logs")
MCP_NAME = "team-a2a"

MAX_OUTPUT = 200_000  # characters sent back to the hub
DEFAULT_CMD = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]

SYSTEM_PROMPT = (
    "You are the Claude Code agent on the PC '{name}' (Windows user '{user}'). "
    "This request comes from the admin '{admin}' through the company's Team A2A hub. "
    "It runs automatically: nobody is at this PC and nobody can answer a permission prompt. "
    "Do the whole task yourself, then reply with a short, clear report of what you found or did. "
    "If you cannot continue without information from the admin, start your reply with "
    "'QUESTION:' and ask it; the admin's answer comes as the next message in this conversation."
)

# Env vars that make a child `claude` think it runs inside another Claude Code session.
STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


def load_config(path=None):
    with open(path or CONFIG_PATH, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    for k in ("name", "hub_url", "token"):
        if not cfg.get(k):
            raise SystemExit(f"receiver.json: '{k}' is missing")
    cfg["hub_url"] = cfg["hub_url"].rstrip("/")
    cfg.setdefault("claude_cmd", DEFAULT_CMD)
    cfg.setdefault("workdir", "")  # empty = the user's home folder
    cfg.setdefault("heartbeat_s", 10)
    cfg.setdefault("poll_wait_s", 20)
    cfg.setdefault("extra_system_prompt", "")
    cfg.setdefault("mcp_cmd", None)  # [exe, args...] of the admin MCP; set by the installer
    wd = cfg["workdir"] or os.path.expanduser("~")
    cfg["workdir"] = wd if os.path.isabs(wd) else os.path.join(HERE, wd)
    return cfg


def log(entry):
    os.makedirs(LOG_DIR, exist_ok=True)
    entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(LOG_DIR, "receiver.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(f"[{entry['ts']}] {entry.get('event')} {entry.get('task_id', '')}", flush=True)


class HubError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code}: {msg}")
        self.code = code


# Direct connections only: the Windows system proxy must not catch LAN addresses.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def cap(text):
    if text and len(text) > MAX_OUTPUT:
        return text[:MAX_OUTPUT] + f"\n[truncated: {len(text)} characters in total]"
    return text


class Receiver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.info = {"host": socket.gethostname(), "user": getpass.getuser(), "version": VERSION}
        self.stop = threading.Event()
        self.role = None  # last role applied on this PC ("user" / "admin")

    # ---- role from the hub: admins get the team-a2a MCP in their Claude, users do not
    def sync_role(self, role):
        if role == self.role or not self.cfg.get("mcp_cmd"):
            return
        if time.time() < getattr(self, "_role_retry_at", 0):
            return  # the last try failed: wait before the next one
        claude = shutil.which(self.cfg["claude_cmd"][0])
        if not claude:
            return
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}

        def run(args):
            return subprocess.run([claude, "mcp"] + args, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=60,
                                  creationflags=flags, env=env)
        try:
            cur = run(["get", MCP_NAME])
            ours = os.path.normcase(os.path.abspath(self.cfg["mcp_cmd"][0]))
            text = cur.stdout or ""
            mine = ours in os.path.normcase(text) and "A2A_ADMIN_TOKEN" not in text
            if cur.returncode == 0 and not mine:
                # Registered by hand (for example the hub owner's own token): do not touch it.
                self.role = role
                log({"event": "role", "role": role, "note": "own team-a2a MCP kept"})
                return
            run(["remove", MCP_NAME, "-s", "user"])  # also clears an old path
            if role == "admin":
                r = run(["add", MCP_NAME, "--scope", "user", "--"] + list(self.cfg["mcp_cmd"]))
                if r.returncode != 0:
                    raise RuntimeError(r.stderr.strip() or r.stdout.strip())
            self.role = role
            log({"event": "role", "role": role})
        except Exception as e:
            self._role_retry_at = time.time() + 600
            log({"event": "role_error", "role": role, "error": str(e)})

    # ---- hub calls
    def call(self, path, body, timeout=15):
        data = json.dumps({**self.info, **body}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.cfg["hub_url"] + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-A2A-Token": self.cfg["token"]})
        try:
            with OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read().decode("utf-8")).get("error")
            except Exception:
                msg = str(e)
            raise HubError(e.code, msg)

    def post_result(self, task_id, **res):
        """Retry until the hub takes the result. Give up on 401/403/404, or after many 400s."""
        delay, bad = 2, 0
        res = {**res, "output": cap(res.get("output")), "error": cap(res.get("error"))}
        while not self.stop.is_set():
            try:
                return self.call("/api/worker/result", {"task_id": task_id, **res})
            except HubError as e:
                bad += e.code == 400
                if e.code in (401, 403, 404) or bad >= 10:
                    log({"event": "result_refused", "task_id": task_id, "error": str(e)})
                    return None
                log({"event": "result_retry", "task_id": task_id, "error": str(e)})
            except Exception as e:
                log({"event": "result_retry", "task_id": task_id, "error": str(e)})
            self.stop.wait(delay)
            delay = min(delay * 2, 60)

    # ---- claude
    def build_cmd(self, task, resume):
        cmd = list(self.cfg["claude_cmd"])
        exe = shutil.which(cmd[0])
        if not exe:
            raise RuntimeError(f"'{cmd[0]}' not found on PATH")
        cmd[0] = exe
        sp = SYSTEM_PROMPT.format(name=self.cfg["name"], user=self.info["user"], admin=task["admin"])
        if self.cfg["extra_system_prompt"]:
            sp += "\n" + self.cfg["extra_system_prompt"]
        # A file, not an argument: claude.cmd (npm install) breaks on newlines and % in arguments.
        os.makedirs(LOG_DIR, exist_ok=True)
        sp_path = os.path.join(LOG_DIR, "system_prompt.txt")
        with open(sp_path, "w", encoding="utf-8") as f:
            f.write(sp)
        cmd += ["--append-system-prompt-file", sp_path]
        if resume and task.get("session_id"):
            cmd += ["--resume", task["session_id"]]
        return cmd

    @staticmethod
    def kill_tree(p):
        if p.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            p.kill()

    def run_claude(self, task, resume=True):
        """Return (status, output, error, session_id, cost), or None when --resume found no session."""
        cmd = self.build_cmd(task, resume)
        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        env["A2A_IN_TASK"] = "1"  # the admin MCP refuses to work inside a remote task (no loops)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        # Files, not pipes: a background child of claude that holds a pipe open cannot hang us.
        run_dir = os.path.join(LOG_DIR, "run")
        os.makedirs(run_dir, exist_ok=True)
        base = os.path.join(run_dir, task["task_id"])
        with open(base + ".in", "wb") as f:
            f.write(task["message"].encode("utf-8"))
        with open(base + ".in", "rb") as fin, open(base + ".out", "wb") as fout, \
                open(base + ".err", "wb") as ferr:
            p = subprocess.Popen(cmd, stdin=fin, stdout=fout, stderr=ferr, cwd=self.cfg["workdir"],
                                 env=env, creationflags=flags)
            deadline = time.time() + float(task.get("timeout_s") or 3600)
            canceled = timed_out = False
            while True:
                try:
                    p.wait(self.cfg["heartbeat_s"])
                    break
                except subprocess.TimeoutExpired:
                    pass
                try:
                    if self.call("/api/worker/heartbeat", {"task_id": task["task_id"]}).get("cancel"):
                        canceled = True
                except Exception as e:
                    log({"event": "heartbeat_error", "task_id": task["task_id"], "error": str(e)})
                timed_out = time.time() > deadline
                if canceled or timed_out or self.stop.is_set():
                    self.kill_tree(p)
                    try:
                        p.wait(30)
                    except subprocess.TimeoutExpired:
                        pass
                    break
        out = open(base + ".out", "rb").read().decode("utf-8", "replace").strip()
        err = open(base + ".err", "rb").read().decode("utf-8", "replace").strip()
        for ext in (".in", ".out", ".err"):
            try:
                os.unlink(base + ext)
            except OSError:
                pass
        if canceled:
            return "canceled", None, "canceled by admin", None, None
        if timed_out:
            return "failed", None, f"timeout after {task.get('timeout_s')} s", None, None

        data = None
        for line in reversed(out.splitlines()):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
        if not isinstance(data, dict) or not ("result" in data or "is_error" in data):
            data = None
        # Only a missing session counts as a resume failure. Any other error does NOT run again,
        # because the first attempt may already have changed things on the PC.
        if (data is None and p.returncode != 0 and resume and task.get("session_id")
                and "no conversation found" in err.lower()):
            return None
        if data is not None:
            result = data.get("result") or ""
            sid = data.get("session_id")
            cost = data.get("total_cost_usd")
            if data.get("is_error"):
                return "failed", result, (result or err or "claude reported an error")[-4000:], sid, cost
            status = "needs_input" if result.lstrip().upper().startswith("QUESTION:") else "done"
            return status, result, None, sid, cost
        if p.returncode != 0:
            return "failed", out[-4000:] or None, f"exit {p.returncode}: {err[-3000:]}", None, None
        return "done", out, None, None, None  # plain-text output (custom claude_cmd)

    def handle(self, task):
        log({"event": "start", "task_id": task["task_id"], "admin": task["admin"],
             "message": task["message"]})
        try:
            r = self.run_claude(task, resume=True)
            if r is None:
                log({"event": "resume_failed", "task_id": task["task_id"]})
                note = ("[The earlier conversation could not be loaded on this PC. "
                        "Treat this message on its own.]\n\n")
                r = self.run_claude({**task, "message": note + task["message"], "session_id": None},
                                    resume=False)
            status, output, error, sid, cost = r
        except Exception as e:
            status, output, error, sid, cost = "failed", None, f"{type(e).__name__}: {e}", None, None
        log({"event": "finish", "task_id": task["task_id"], "status": status,
             "output": output, "error": error, "session_id": sid})
        self.post_result(task["task_id"], status=status, output=output, error=error,
                         session_id=sid, cost_usd=cost)

    # ---- main loop
    def loop(self):
        log({"event": "online", "hub": self.cfg["hub_url"], "name": self.cfg["name"]})
        delay = 2
        while not self.stop.is_set():
            try:
                r = self.call("/api/worker/next", {"wait": self.cfg["poll_wait_s"]},
                              timeout=self.cfg["poll_wait_s"] + 15)
                delay = 2
                if r.get("role"):
                    self.sync_role(r["role"])
                if r.get("task"):
                    self.handle(r["task"])
            except HubError as e:
                log({"event": "hub_error", "error": str(e)})
                if e.code == 401:
                    delay = 300  # bad token: do not flood the hub
                self.stop.wait(delay)
                delay = min(delay * 2, 300)
            except Exception as e:
                log({"event": "hub_unreachable", "error": f"{type(e).__name__}: {e}"})
                self.stop.wait(delay)
                delay = min(delay * 2, 60)


def main():
    cfg = load_config()
    rcv = Receiver(cfg)
    try:
        rcv.loop()
    except KeyboardInterrupt:
        rcv.stop.set()


if __name__ == "__main__":
    sys.exit(main())
