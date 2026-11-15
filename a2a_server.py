"""A2A server: receives a prompt over HTTP, runs the local Codex, returns the answer."""
import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from a2a_common import HERE, load_config

CFG = load_config()
LOCK = threading.Lock()
LOG_DIR = os.path.join(HERE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_PATH = os.path.join(LOG_DIR, "a2a.jsonl")


def log(entry):
    entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def run_codex(prompt):
    cmd = list(CFG["codex_cmd"])
    exe = shutil.which(cmd[0])  # finds codex.cmd on Windows
    if not exe:
        raise RuntimeError(f"'{cmd[0]}' not found on PATH")
    cmd[0] = exe
    p = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=CFG["workdir"], timeout=CFG["timeout_s"],
    )
    if p.returncode != 0:
        raise RuntimeError(f"codex exit {p.returncode}: {p.stderr[-2000:]}")
    return p.stdout.strip()


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._reply(200, {"ok": True, "name": CFG.get("name"), "busy": LOCK.locked()})
        else:
            self._reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/prompt":
            return self._reply(404, {"ok": False, "error": "not found"})
        if self.headers.get("X-A2A-Token") != CFG["token"]:
            return self._reply(401, {"ok": False, "error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            prompt = req["prompt"]
        except Exception as e:
            return self._reply(400, {"ok": False, "error": f"bad request: {e}"})
        sender = req.get("from", "?")
        print(f"[{sender}] -> {prompt[:120]!r}", flush=True)
        with LOCK:  # one Codex job at a time
            try:
                out = run_codex(prompt)
                log({"from": sender, "prompt": prompt, "output": out})
                print(f"[{CFG.get('name')}] <- {out[:120]!r}", flush=True)
                self._reply(200, {"ok": True, "from": CFG.get("name"), "output": out})
            except Exception as e:
                log({"from": sender, "prompt": prompt, "error": str(e)})
                self._reply(500, {"ok": False, "error": str(e)})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", CFG["port"]), Handler)
    print(f"A2A server '{CFG.get('name')}' on port {CFG['port']}", flush=True)
    srv.serve_forever()
