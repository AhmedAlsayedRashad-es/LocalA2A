"""Team A2A direct file transfer: the bytes go straight from PC to PC (or admin to PC), not through the hub.

The hub only says yes: it gives the sender a short-lived ticket, signed (HMAC-SHA256) with the token of the
PC that serves or takes the file. That PC checks the ticket by itself, streams the file, checks the sha256 and
reports the result to the hub (audit). Needs an inbound firewall rule for `direct_port` (default 8766);
without it the sender falls back to the hub relay.

Routes on the PC (only with a valid ticket in the header X-A2A-Ticket):
  PUT /a2a/files/<id>   take a file into <workdir>\\a2a-inbox     (ticket op "put", dst = this PC)
  GET /a2a/files/<id>   send the file the ticket names              (ticket op "get", src = this PC)
  GET /a2a/health
Python 3.8+, standard library only.
"""
import base64
import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

from a2a_util import safe_name

TICKET_S = 600  # a ticket is good for 10 minutes
CHUNK = 65536
DEFAULT_PORT = 8766


# ---------------------------------------------------------------- tickets

def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_ticket(token, claims):
    payload = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    mac = hmac.new(str(token).encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{mac}"


def check_ticket(token, ticket, now=None):
    """Return the claims of a ticket signed with `token`, else raise PermissionError."""
    try:
        payload, mac = str(ticket).split(".")
        want = hmac.new(str(token).encode("utf-8"), payload.encode("ascii"), hashlib.sha256).hexdigest()
    except (ValueError, UnicodeError):
        raise PermissionError("bad ticket")
    if not hmac.compare_digest(want, mac):
        raise PermissionError("bad ticket signature")
    try:
        claims = json.loads(_unb64(payload).decode("utf-8"))
    except (ValueError, UnicodeError):
        raise PermissionError("bad ticket")
    if not isinstance(claims, dict) or float(claims.get("exp", 0)) < (now or time.time()):
        raise PermissionError("ticket expired")
    return claims


# ---------------------------------------------------------------- client

class Unreachable(Exception):
    """The PC did not answer: the caller may fall back to the hub relay."""


def put_file(url, ticket, path, timeout=600, connect_timeout=5):
    """PUT the file to a PC's direct server. Returns its JSON answer; raises Unreachable or RuntimeError."""
    import http.client
    from urllib.parse import urlparse
    u = urlparse(url)
    size = os.path.getsize(path)
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=connect_timeout)
        conn.connect()
    except OSError as e:
        raise Unreachable(f"{u.hostname}:{u.port} did not answer: {e}")
    try:
        conn.sock.settimeout(timeout)
        conn.putrequest("PUT", u.path)
        conn.putheader("X-A2A-Ticket", ticket)
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(size))
        conn.endheaders()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                conn.send(chunk)
        r = conn.getresponse()
        body = r.read()
    except OSError as e:
        raise RuntimeError(f"direct upload broke off: {e}")
    finally:
        conn.close()
    try:
        out = json.loads(body.decode("utf-8"))
    except ValueError:
        out = {"error": body[:200].decode("utf-8", "replace")}
    if r.status != 200:
        raise RuntimeError(f"the PC said HTTP {r.status}: {out.get('error')}")
    return out


# ---------------------------------------------------------------- server

def unique_path(folder, name):
    stem, ext = os.path.splitext(name)
    path, i = os.path.join(folder, name), 2
    while os.path.exists(path):
        path, i = os.path.join(folder, f"{stem} ({i}){ext}"), i + 1
    return path


def sha256_path(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


class DirectServer:
    """report(tid, ok, sha256=None, error=None) tells the hub how it ended. log(dict) writes the PC log."""

    def __init__(self, name, token, workdir, report, log=None, bind="0.0.0.0", port=DEFAULT_PORT):
        self.name, self.token, self.workdir = name, token, workdir
        self.report, self.log = report, (log or (lambda e: None))
        self.used = {}  # ticket id -> exp; a ticket works once
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                outer._route(self, "GET")

            def do_PUT(self):
                outer._route(self, "PUT")

        ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
        self.httpd = ThreadingHTTPServer((bind, int(port)), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- helpers
    @staticmethod
    def _send(h, code, obj, close=False):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        if close:
            h.send_header("Connection", "close")
            h.close_connection = True
        h.end_headers()
        h.wfile.write(data)

    def _claim(self, h, op, tid):
        """Check the ticket for this request; return its claims or answer and return None."""
        try:
            c = check_ticket(self.token, h.headers.get("X-A2A-Ticket", ""))
        except PermissionError as e:
            self._send(h, 403, {"ok": False, "error": str(e)}, close=True)
            return None
        me = "dst" if op == "put" else "src"
        if c.get("op") != op or c.get("id") != tid or c.get(me) != f"pc:{self.name}":
            self._send(h, 403, {"ok": False, "error": "ticket is not for this request"}, close=True)
            return None
        with self.lock:
            now = time.time()
            for k in [k for k, exp in self.used.items() if exp < now]:
                del self.used[k]
            if tid in self.used:
                self._send(h, 409, {"ok": False, "error": "ticket already used"}, close=True)
                return None
            self.used[tid] = float(c["exp"])
        return c

    def _route(self, h, method):
        path = h.path.split("?", 1)[0].rstrip("/")
        if method == "GET" and path == "/a2a/health":
            return self._send(h, 200, {"ok": True, "pc": self.name})
        parts = path.split("/")  # /a2a/files/ID
        if len(parts) != 4 or parts[1:3] != ["a2a", "files"]:
            return self._send(h, 404, {"ok": False, "error": "not found"}, close=True)
        tid = unquote(parts[3])
        try:
            if method == "PUT":
                return self._put(h, tid)
            return self._get(h, tid)
        except Exception as e:  # keep the server alive
            self.log({"event": "direct_error", "transfer_id": tid, "error": f"{type(e).__name__}: {e}"})
            try:
                self._send(h, 500, {"ok": False, "error": f"{type(e).__name__}: {e}"}, close=True)
            except OSError:
                pass

    def _put(self, h, tid):
        c = self._claim(h, "put", tid)
        if c is None:
            return
        try:
            length = int(h.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length != int(c.get("size", -2)):
            return self._send(h, 400, {"ok": False, "error": "Content-Length differs from the ticket"},
                              close=True)
        try:
            name = safe_name(c.get("name"))
        except ValueError as e:
            return self._send(h, 400, {"ok": False, "error": str(e)}, close=True)
        inbox = os.path.join(self.workdir, "a2a-inbox")
        os.makedirs(inbox, exist_ok=True)
        part = os.path.join(inbox, f"{name}.{tid}.part")
        hsh, got = hashlib.sha256(), 0
        try:
            with open(part, "wb") as f:
                while got < length:
                    chunk = h.rfile.read(min(CHUNK, length - got))
                    if not chunk:
                        break
                    hsh.update(chunk)
                    f.write(chunk)
                    got += len(chunk)
        except OSError:
            got = -1
        digest = hsh.hexdigest()
        if got != length or digest != str(c.get("sha256", "")).lower():
            try:
                os.unlink(part)
            except OSError:
                pass
            err = ("upload broke off" if got != length else "sha256 mismatch: the file changed on the way")
            self.log({"event": "file_rejected", "transfer_id": tid, "name": name, "error": err, "mode": "direct"})
            self.report(tid, False, digest, err)
            return self._send(h, 400, {"ok": False, "error": err}, close=True)
        with self.lock:
            dest = unique_path(inbox, name)
            os.replace(part, dest)
        self.log({"event": "file_received", "transfer_id": tid, "name": name, "path": dest, "size": got,
                  "src": c.get("src"), "mode": "direct"})
        self.report(tid, True, digest, None)
        return self._send(h, 200, {"ok": True, "transfer_id": tid, "name": os.path.basename(dest)})

    def _get(self, h, tid):
        c = self._claim(h, "get", tid)
        if c is None:
            return
        path = os.path.expanduser(str(c.get("path") or ""))
        if path and not os.path.isabs(path):
            path = os.path.join(self.workdir, path)
        if not path or not os.path.isfile(path):
            err = f"file not found on {self.name}: {c.get('path')}"
            self.report(tid, False, None, err)
            return self._send(h, 404, {"ok": False, "error": err}, close=True)
        digest, size = sha256_path(path), os.path.getsize(path)
        h.send_response(200)
        h.send_header("Content-Type", "application/octet-stream")
        h.send_header("Content-Length", str(size))
        h.send_header("X-A2A-SHA256", digest)
        h.send_header("X-A2A-Name", quote(os.path.basename(path)))
        h.end_headers()
        sent = 0
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    h.wfile.write(chunk)
                    sent += len(chunk)
        except OSError as e:
            self.report(tid, False, digest, f"sending broke off: {e}")
            h.close_connection = True
            return
        self.log({"event": "file_sent", "transfer_id": tid, "path": path, "size": sent, "mode": "direct"})
        self.report(tid, True, digest, None)
