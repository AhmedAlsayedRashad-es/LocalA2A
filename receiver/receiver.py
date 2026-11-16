"""Team A2A receiver: runs on a colleague PC, takes tasks from the hub, runs Claude Code.

Run:  python receiver.py   or   TeamA2A-Agent.exe (installed by TeamA2A-Setup.exe)
Send a file to another PC:  python receiver.py send-file --to PC-NAME PATH
SSH helper:                 python receiver.py ssh [--config FILE] render-config|sync|hub-cidr|shell ...
Config: receiver.json next to the program, else %LOCALAPPDATA%\\TeamA2A\\receiver.json.
Python 3.8+, standard library only.
"""
import argparse
import getpass
import hashlib
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
import zipfile
from urllib.parse import quote

VERSION = "1.2"
HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.append(HERE)
import a2a_direct  # noqa: E402
import a2a_sign  # noqa: E402
from a2a_util import canonical, safe_name, sha256_file, version_tuple  # noqa: E402

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
CHUNK = 65536

SYSTEM_PROMPT = (
    "You are the Claude Code agent on the PC '{name}' (Windows user '{user}'). "
    "This request comes from the admin '{admin}' through the company's Team A2A hub. "
    "It runs automatically: nobody is at this PC and nobody can answer a permission prompt. "
    "Do the whole task yourself, then reply with a short, clear report of what you found or did. "
    "If you cannot continue without information from the admin, start your reply with "
    "'QUESTION:' and ask it; the admin's answer comes as the next message in this conversation. "
    "To send a file to another PC on the hub, run: {send_cmd} send-file --to PC-NAME \"FILE\" "
    "(it arrives in that PC's a2a-inbox folder)."
)

# Env vars that make a child `claude` think it runs inside another Claude Code session.
STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")


def load_config(path=None):
    path = path or CONFIG_PATH
    with open(path, encoding="utf-8-sig") as f:
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
    # Direct file transfer: a small file server on direct_port. The installer turns it on after it
    # made the firewall rule; without the rule other PCs cannot reach it (they use the hub relay).
    cfg.setdefault("direct", False)
    cfg.setdefault("direct_port", a2a_direct.DEFAULT_PORT)
    wd = cfg["workdir"] or os.path.expanduser("~")
    cfg["workdir"] = wd if os.path.isabs(wd) else os.path.join(HERE, wd)
    cfg["_path"] = os.path.abspath(path)
    return cfg


_LOG_LOCK = threading.Lock()


def log(entry):
    with _LOG_LOCK:
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


def _hub_error(e):
    try:
        msg = json.loads(e.read().decode("utf-8")).get("error")
    except Exception:
        msg = str(e)
    return HubError(e.code, msg)


def upload_file(cfg, url_path, path, timeout=600):
    """POST the bytes of `path` to the hub with its sha256. Return the hub's JSON answer."""
    size = os.path.getsize(path)
    digest = sha256_file(path)
    with open(path, "rb") as f:
        req = urllib.request.Request(cfg["hub_url"] + url_path, data=f, method="POST", headers={
            "Content-Type": "application/octet-stream", "Content-Length": str(size),
            "X-A2A-Token": cfg["token"], "X-A2A-SHA256": digest})
        try:
            with OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise _hub_error(e)


def hub_json(cfg, path, body, timeout=30):
    req = urllib.request.Request(cfg["hub_url"] + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "X-A2A-Token": cfg["token"]})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise _hub_error(e)


def send_file(cfg, to, path):
    """Send a file to another PC: straight to it when it can be reached, else through the hub."""
    name = os.path.basename(path)
    safe_name(name)
    size, digest = os.path.getsize(path), sha256_file(path)
    try:
        d = hub_json(cfg, "/api/worker/files/direct",
                     {"op": "put", "to": to, "name": name, "size": size, "sha256": digest})
        a2a_direct.put_file(d["url"], d["ticket"], path)
        return {"transfer_id": d["transfer_id"], "mode": "direct"}
    except HubError as e:
        if e.code != 409:  # 409 = no direct route to that PC; anything else is a real refusal
            raise
    except a2a_direct.Unreachable:
        pass
    r = upload_file(cfg, f"/api/worker/files/send?to={quote(to)}&name={quote(name)}", path)
    return {**r, "mode": "hub"}


def unique_path(folder, name):
    """folder/name, or 'name (2).ext', 'name (3).ext' ... when it exists."""
    stem, ext = os.path.splitext(name)
    path, i = os.path.join(folder, name), 2
    while os.path.exists(path):
        path, i = os.path.join(folder, f"{stem} ({i}){ext}"), i + 1
    return path


class Receiver:
    def __init__(self, cfg):
        self.cfg = cfg
        self.version = VERSION
        self.info = {"host": socket.gethostname(), "user": getpass.getuser(), "version": self.version}
        self.stop = threading.Event()
        self.role = None  # last role applied on this PC ("user" / "admin")
        self.max_parallel = max(1, int(cfg.get("max_parallel", 3)))
        self.running = {}  # task_id -> thread; an id leaves only after its result reached the hub
        self.run_lock = threading.Lock()
        self.slot_free = threading.Event()
        self.files_busy = set()  # transfer ids handled now
        self.file_lock = threading.Lock()
        # updates
        self.install_dir = HERE
        self.restart = self._restart
        self.updating = threading.Event()  # set: take no new tasks
        self.update_lock = threading.Lock()
        self.update_wake = threading.Event()
        self.last_update_check = 0.0
        self.update_installed_at = None

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
            raise _hub_error(e)

    def get(self, path, timeout=60):
        """GET from the hub; return the response object (caller reads and closes it)."""
        req = urllib.request.Request(self.cfg["hub_url"] + path, method="GET",
                                     headers={"X-A2A-Token": self.cfg["token"]})
        try:
            return OPENER.open(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            raise _hub_error(e)

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
    def send_cmd(self):
        if getattr(sys, "frozen", False):
            return f'"{sys.executable}"'
        return f'"{sys.executable}" "{os.path.join(HERE, "receiver.py")}"'

    def build_cmd(self, task, resume):
        cmd = list(self.cfg["claude_cmd"])
        exe = shutil.which(cmd[0])
        if not exe:
            raise RuntimeError(f"'{cmd[0]}' not found on PATH")
        cmd[0] = exe
        sp = SYSTEM_PROMPT.format(name=self.cfg["name"], user=self.info["user"], admin=task["admin"],
                                  send_cmd=self.send_cmd())
        if self.cfg.get("extra_system_prompt"):
            sp += "\n" + self.cfg["extra_system_prompt"]
        # A file, not an argument: claude.cmd (npm install) breaks on newlines and % in arguments.
        # One file per task: tasks run in parallel.
        run_dir = os.path.join(LOG_DIR, "run")
        os.makedirs(run_dir, exist_ok=True)
        sp_path = os.path.join(run_dir, task["task_id"] + ".sp.txt")
        with open(sp_path, "w", encoding="utf-8") as f:
            f.write(sp)
        cmd += ["--append-system-prompt-file", sp_path]
        if resume and task.get("session_id"):
            cmd += ["--resume", task["session_id"]]
        return cmd

    def session_dir(self, task):
        """Each thread runs in its own folder, so its Claude session (--resume) is found again
        and parallel threads do not share a project folder."""
        try:
            name = safe_name(str(task.get("context_id") or ""))
        except ValueError:
            return self.cfg["workdir"]
        d = os.path.join(self.cfg["workdir"], "a2a-sessions", name)
        os.makedirs(d, exist_ok=True)
        return d

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
        if self.cfg.get("_path"):
            env["A2A_RECEIVER_CONFIG"] = self.cfg["_path"]  # for `receiver.py send-file`
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        # Files, not pipes: a background child of claude that holds a pipe open cannot hang us.
        run_dir = os.path.join(LOG_DIR, "run")
        os.makedirs(run_dir, exist_ok=True)
        base = os.path.join(run_dir, task["task_id"])
        with open(base + ".in", "wb") as f:
            f.write(task["message"].encode("utf-8"))
        with open(base + ".in", "rb") as fin, open(base + ".out", "wb") as fout, \
                open(base + ".err", "wb") as ferr:
            p = subprocess.Popen(cmd, stdin=fin, stdout=fout, stderr=ferr, cwd=self.session_dir(task),
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
        with open(base + ".out", "rb") as f:
            out = f.read().decode("utf-8", "replace").strip()
        with open(base + ".err", "rb") as f:
            err = f.read().decode("utf-8", "replace").strip()
        for ext in (".in", ".out", ".err", ".sp.txt"):
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

    # ---- parallel tasks
    def start_task(self, task):
        with self.run_lock:
            if task["task_id"] in self.running:
                return
            th = threading.Thread(target=self._run_task, args=(task,), daemon=True)
            self.running[task["task_id"]] = th
        th.start()

    def _run_task(self, task):
        try:
            self.handle(task)
        finally:
            with self.run_lock:
                self.running.pop(task["task_id"], None)
            self.slot_free.set()

    # ---- files from the hub
    def start_file(self, f):
        with self.file_lock:
            if f["id"] in self.files_busy:
                return
            self.files_busy.add(f["id"])
        threading.Thread(target=self._file_job, args=(f, self.get_file), daemon=True).start()

    def start_request(self, q):
        with self.file_lock:
            if q["id"] in self.files_busy:
                return
            self.files_busy.add(q["id"])
        threading.Thread(target=self._file_job, args=(q, self.answer_request), daemon=True).start()

    def _file_job(self, item, fn):
        try:
            fn(item)
        except Exception as e:
            log({"event": "file_error", "transfer_id": item.get("id"), "error": f"{type(e).__name__}: {e}"})
        finally:
            with self.file_lock:
                self.files_busy.discard(item["id"])

    def ack(self, tid, ok, sha256=None, error=None):
        return self.call(f"/api/worker/files/{quote(tid)}/ack", {"ok": ok, "sha256": sha256, "error": error})

    def get_file(self, f):
        """Download a file for this PC into <workdir>/a2a-inbox and check its sha256."""
        tid = f["id"]
        try:
            name = safe_name(f["name"])
        except ValueError as e:
            return self.ack(tid, False, error=str(e))
        inbox = os.path.join(self.cfg["workdir"], "a2a-inbox")
        os.makedirs(inbox, exist_ok=True)
        part = os.path.join(inbox, f"{name}.{tid}.part")
        h, size = hashlib.sha256(), 0
        try:
            with self.get(f"/api/worker/files/{quote(tid)}", timeout=120) as r, open(part, "wb") as out:
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    h.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
        except Exception:
            try:
                os.unlink(part)
            except OSError:
                pass
            raise  # network trouble: the hub offers the file again later
        digest = h.hexdigest()
        if digest != str(f.get("sha256") or "").lower() or (f.get("size") is not None and size != f["size"]):
            os.unlink(part)
            log({"event": "file_rejected", "transfer_id": tid, "name": name, "error": "sha256 mismatch"})
            return self.ack(tid, False, sha256=digest, error="sha256 mismatch: the file was damaged")
        with self.file_lock:
            dest = unique_path(inbox, name)
            os.replace(part, dest)
        log({"event": "file_received", "transfer_id": tid, "name": name, "path": dest, "size": size,
             "src": f.get("src")})
        return self.ack(tid, True, sha256=digest)

    def answer_request(self, q):
        """An admin asked for a file on this PC: upload it, or say why not."""
        tid, path = q["id"], os.path.expanduser(str(q.get("path") or ""))
        if path and not os.path.isabs(path):
            path = os.path.join(self.cfg["workdir"], path)
        if not path or not os.path.isfile(path):
            log({"event": "file_request_failed", "transfer_id": tid, "path": path})
            return self.ack(tid, False, error=f"file not found on {self.cfg['name']}: {q.get('path')}")
        try:
            name = q.get("name") or os.path.basename(path)
            upload_file(self.cfg, f"/api/worker/files/{quote(tid)}/upload?name={quote(name)}", path)
        except HubError as e:
            if e.code in (400, 411, 413):
                return self.ack(tid, False, error=str(e))
            raise
        except OSError as e:
            return self.ack(tid, False, error=f"cannot read {path}: {e}")
        log({"event": "file_sent", "transfer_id": tid, "path": path})

    # ---- updates (signed by the hub's update key, pinned in receiver.json)
    def check_update(self):
        if not self.cfg.get("auto_update", True):
            return "disabled"
        with self.update_lock:
            self.last_update_check = time.time()
            try:
                res = self._check_update()
            except Exception as e:
                res = f"error:{type(e).__name__}: {e}"
            finally:
                self.updating.clear()
                self.slot_free.set()
        log({"event": "update_check", "result": res})
        return res

    def _check_update(self):
        key = str(self.cfg.get("update_pubkey") or "").strip()
        if not key:
            return "no_key"
        try:
            pub = bytes.fromhex(key)
        except ValueError:
            return "error:update_pubkey is not hex"
        with self.get("/api/update/manifest", timeout=30) as r:
            rel = json.loads(r.read().decode("utf-8")).get("release")
        if not rel:
            return "none"
        man = rel.get("manifest")
        try:
            sig = bytes.fromhex(rel.get("signature") or "")
        except ValueError:
            return "bad_signature"
        if not isinstance(man, dict) or not a2a_sign.verify(pub, canonical(man), sig):
            return "bad_signature"
        try:
            if version_tuple(man["version"]) <= version_tuple(self.version):
                return "not_newer"
            files = man["files"]
            for n in files:
                safe_name(n)
        except (KeyError, TypeError, ValueError):
            return "bad_signature"
        need = os.path.basename(sys.executable) if getattr(sys, "frozen", False) else "receiver.py"
        if need not in files:
            return "wrong_kind"
        # No new tasks; let the running ones finish first.
        self.updating.set()
        while not self.stop.is_set():
            with self.run_lock:
                if not self.running:
                    break
            self.slot_free.wait(1)
            self.slot_free.clear()
        if self.stop.is_set():
            return "error:stopping"
        staging = os.path.join(self.install_dir, "update_staging")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        pkg = os.path.join(staging, "package.zip")
        h, size = hashlib.sha256(), 0
        with self.get(f"/api/update/package?version={quote(man['version'])}", timeout=120) as r, \
                open(pkg, "wb") as out:
            while True:
                chunk = r.read(CHUNK)
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
                size += len(chunk)
        if size != man.get("size") or h.hexdigest() != man.get("sha256"):
            shutil.rmtree(staging, ignore_errors=True)
            return "sha256_mismatch"
        new = os.path.join(staging, "files")
        os.makedirs(new)
        with zipfile.ZipFile(pkg) as z:
            if sorted(z.namelist()) != sorted(files):
                shutil.rmtree(staging, ignore_errors=True)
                return "sha256_mismatch"
            for n in files:
                data = z.read(n)
                if hashlib.sha256(data).hexdigest() != files[n]:
                    shutil.rmtree(staging, ignore_errors=True)
                    return "sha256_mismatch"
                with open(os.path.join(new, n), "wb") as f:
                    f.write(data)
        # Back up what is there now, then swap file by file (rename the old one first: Windows
        # cannot overwrite a running exe, but it can rename it).
        backup = os.path.join(self.install_dir, "update_backup", self.version)
        os.makedirs(backup, exist_ok=True)
        for n in files:
            cur = os.path.join(self.install_dir, n)
            if os.path.exists(cur):
                shutil.copy2(cur, os.path.join(backup, n))
        done = []
        try:
            for n in files:
                cur = os.path.join(self.install_dir, n)
                existed = os.path.exists(cur)
                if existed:
                    if os.path.exists(cur + ".old"):
                        os.unlink(cur + ".old")
                    os.replace(cur, cur + ".old")
                done.append((n, existed))
                os.replace(os.path.join(new, n), cur)
        except Exception as e:
            for n, existed in reversed(done):
                cur = os.path.join(self.install_dir, n)
                try:
                    if existed:
                        if os.path.exists(cur) and os.path.exists(cur + ".old"):
                            os.unlink(cur)
                        if os.path.exists(cur + ".old"):
                            os.replace(cur + ".old", cur)
                        elif not os.path.exists(cur):
                            shutil.copy2(os.path.join(backup, n), cur)
                    elif os.path.exists(cur):
                        os.unlink(cur)
                except OSError:
                    pass
            shutil.rmtree(staging, ignore_errors=True)
            return f"error:install failed, old files restored: {e}"
        shutil.rmtree(staging, ignore_errors=True)
        self.update_installed_at = time.time()
        log({"event": "update_installed", "from": self.version, "to": man["version"]})
        self.restart()
        return "installed"

    def _restart(self):
        """Start the new version detached, then end this process."""
        flags = 0
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        if getattr(sys, "frozen", False):
            cmd = [os.path.join(self.install_dir, os.path.basename(sys.executable))]
        else:
            cmd = [sys.executable, os.path.join(self.install_dir, "receiver.py")]
        subprocess.Popen(cmd, cwd=self.install_dir, creationflags=flags, close_fds=True)
        self.stop.set()
        os._exit(0)

    def clean_old_files(self):
        try:
            for n in os.listdir(self.install_dir):
                if n.endswith(".old"):
                    try:
                        os.unlink(os.path.join(self.install_dir, n))
                    except OSError:
                        pass
        except OSError:
            pass

    def update_loop(self):
        every = float(self.cfg.get("update_check_s", 3600))
        while not self.stop.is_set():
            self.check_update()
            self.update_wake.wait(every)
            self.update_wake.clear()

    # ---- main loop
    # ---- direct file transfer (bytes straight from the sender; the hub signs a ticket)
    def start_direct(self):
        self.direct = None
        self.info["direct_port"] = 0  # 0 = no direct route: senders use the hub relay
        if not self.cfg.get("direct"):
            return
        try:
            self.direct = a2a_direct.DirectServer(
                self.cfg["name"], self.cfg["token"], self.cfg["workdir"], self.direct_report, log,
                bind=self.cfg.get("direct_bind", "0.0.0.0"), port=self.cfg["direct_port"]).start()
            self.info["direct_port"] = self.direct.port
            log({"event": "direct_on", "port": self.direct.port})
        except OSError as e:
            log({"event": "direct_error", "error": f"cannot listen on port {self.cfg['direct_port']}: {e}"})

    def direct_report(self, tid, ok, sha256=None, error=None):
        for _ in range(5):
            try:
                return self.ack(tid, ok, sha256=sha256, error=error)
            except HubError as e:
                if e.code in (400, 401, 403, 404):
                    log({"event": "direct_ack_refused", "transfer_id": tid, "error": str(e)})
                    return None
            except Exception as e:
                log({"event": "direct_ack_retry", "transfer_id": tid, "error": str(e)})
            self.stop.wait(2)

    def loop(self):
        self.start_direct()
        log({"event": "online", "hub": self.cfg["hub_url"], "name": self.cfg["name"],
             "version": self.version, "max_parallel": self.max_parallel})
        if self.cfg.get("auto_update", True):
            self.clean_old_files()
            threading.Thread(target=self.update_loop, daemon=True).start()
        delay = 2
        while not self.stop.is_set():
            try:
                with self.run_lock:
                    ids = list(self.running)
                free = self.max_parallel - len(ids)
                full = free <= 0 or self.updating.is_set()
                if not full:
                    self.slot_free.clear()
                    r = self.call("/api/worker/next", {"wait": self.cfg["poll_wait_s"], "slots": free,
                                                       "running": ids},
                                  timeout=self.cfg["poll_wait_s"] + 15)
                else:  # all slots busy: keep reporting what runs, wait for a free slot
                    self.slot_free.clear()
                    r = self.call("/api/worker/next", {"wait": 0, "slots": 0, "running": ids})
                delay = 2
                if r.get("role"):
                    self.sync_role(r["role"])
                tasks = r.get("tasks")
                if tasks is None:
                    tasks = [r["task"]] if r.get("task") else []
                for t in tasks:
                    self.start_task(t)
                for f in r.get("files") or []:
                    self.start_file(f)
                for q in r.get("file_requests") or []:
                    self.start_request(q)
                if (r.get("update") and self.cfg.get("auto_update", True)
                        and time.time() - self.last_update_check > 60):
                    self.update_wake.set()
                if full:
                    self.slot_free.wait(self.cfg["heartbeat_s"])
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


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["ssh"]:
        # SSH helper (render-config, sync, hub-cidr, shell): see a2a_ssh.py.
        import a2a_ssh
        return a2a_ssh.main(argv[1:], default_config=CONFIG_PATH)
    ap = argparse.ArgumentParser(description="Team A2A receiver")
    ap.add_argument("--config", help="receiver.json (default: next to the program, else %%LOCALAPPDATA%%)")
    sub = ap.add_subparsers(dest="cmd")
    sf = sub.add_parser("send-file", help="send a file to another PC through the hub")
    sf.add_argument("--to", required=True, help="name of the PC on the hub")
    sf.add_argument("path")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.cmd == "send-file":
        try:
            r = send_file(cfg, args.to, args.path)
        except (HubError, OSError, ValueError, RuntimeError, urllib.error.URLError) as e:
            print(f"send-file failed: {e}", file=sys.stderr)
            return 1
        print("sent straight to the PC" if r.get("mode") == "direct"
              else "sent through the hub (the PC gets it when it polls)", file=sys.stderr)
        print(r.get("transfer_id"))
        return 0
    rcv = Receiver(cfg)
    try:
        rcv.loop()
    except KeyboardInterrupt:
        rcv.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
