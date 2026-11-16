"""Feature 2: file transfer through the hub (real hub + real receivers + admin MCP over stdio).

Run from the project folder:  python -m unittest discover -s tests -p "test_transfer.py" -v
"""
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "hub"), os.path.join(ROOT, "receiver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import hub as hubmod  # noqa: E402
import receiver as rcvmod  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fake_claude.py")
ADMIN = "admin-xfer-token"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Transfer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="a2a_xfer_")
        os.environ["FAKE_CLAUDE_DIR"] = os.path.join(cls.tmp, "sessions")
        cls.old_log_dir = rcvmod.LOG_DIR
        rcvmod.LOG_DIR = os.path.join(cls.tmp, "receiver_logs")
        cls.cfg_path = os.path.join(cls.tmp, "hub.json")
        cfg = {"port": 0, "bind": "127.0.0.1", "data_dir": "data", "report_hour": 99,
               "task_timeout_s": 120, "max_transfer_mb": 1, "admins": {"ahmed": ADMIN},
               "pcs": {"pa": {"token": "ta"}, "pb": {"token": "tb"}, "poff": {"token": "toff"},
                       "pp": {"token": "tpp", "approved": False}}}
        with open(cls.cfg_path, "w") as f:
            json.dump(cfg, f)
        cls.old_cfg = hubmod.CONFIG_PATH
        hubmod.CONFIG_PATH = cls.cfg_path
        cls.srv, cls.hub, cls.stop = hubmod.serve(hubmod.load_config(cls.cfg_path))
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.wd, cls.rcfg, cls.receivers = {}, {}, []
        for name, tok in (("pa", "ta"), ("pb", "tb")):
            wd = os.path.join(cls.tmp, "wd_" + name)
            os.makedirs(wd)
            cls.wd[name] = wd
            path = os.path.join(cls.tmp, f"{name}.json")
            with open(path, "w") as f:
                json.dump({"name": name, "hub_url": cls.url, "token": tok, "workdir": wd,
                           "claude_cmd": [sys.executable, FAKE], "heartbeat_s": 1, "poll_wait_s": 2,
                           "auto_update": False}, f)
            cls.rcfg[name] = path
            r = rcvmod.Receiver(rcvmod.load_config(path))
            threading.Thread(target=r.loop, daemon=True).start()
            cls.receivers.append(r)
        end = time.time() + 10
        while time.time() < end:
            on = {p["name"] for p in cls.api("GET", "/api/pcs")["pcs"] if p["online"]}
            if {"pa", "pb"} <= on:
                break
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        for r in cls.receivers:
            r.stop.set()
        cls.stop.set()
        cls.srv.shutdown()
        cls.srv.server_close()
        time.sleep(0.5)
        cls.hub.store.db.close()
        hubmod.CONFIG_PATH = cls.old_cfg
        rcvmod.LOG_DIR = cls.old_log_dir
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---- helpers
    @classmethod
    def api(cls, method, path, body=None, token=ADMIN, expect=200):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(cls.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-A2A-Token": token})
        try:
            with OPENER.open(req, timeout=30) as r:
                code, out = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, out = e.code, json.loads(e.read())
        if expect is not None:
            assert code == expect, f"{method} {path}: HTTP {code} {out}"
        return out

    def upload(self, path, data, token=ADMIN, digest=None, expect=200):
        req = urllib.request.Request(self.url + path, data=data, method="POST", headers={
            "Content-Type": "application/octet-stream", "X-A2A-Token": token,
            "X-A2A-SHA256": digest if digest is not None else sha(data)})
        try:
            with OPENER.open(req, timeout=30) as r:
                code, out = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, out = e.code, json.loads(e.read())
        self.assertEqual(code, expect, out)
        return out

    def admin_send(self, to, name, data, **kw):
        return self.upload(f"/api/files/send?to={quote(to)}&name={quote(name)}", data, **kw)

    def wait_transfer(self, tid, statuses=("delivered", "failed", "expired"), timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            t = self.api("GET", f"/api/files/{tid}")["transfer"]
            if t["status"] in statuses:
                return t
            time.sleep(0.1)
        self.fail(f"transfer {tid} did not reach {statuses}: {t}")

    def audit(self, event):
        return [a for a in self.api("GET", "/api/audit?limit=1000")["audit"] if a["event"] == event]

    def inbox(self, pc):
        d = os.path.join(self.wd[pc], "a2a-inbox")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    # X1
    def test_x1_admin_to_pc_delivered(self):
        data = os.urandom(100_000)
        tid = self.admin_send("pa", "x1.bin", data)["transfer_id"]
        t = self.wait_transfer(tid)
        self.assertEqual(t["status"], "delivered")
        with open(os.path.join(self.wd["pa"], "a2a-inbox", "x1.bin"), "rb") as f:
            self.assertEqual(sha(f.read()), sha(data))
        self.assertFalse(os.path.exists(self.hub.transfer_file(tid)), "stored copy not removed")

    # X2
    def test_x2_size_limit_and_length_required(self):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.putrequest("POST", "/api/files/send?to=pa&name=big.bin")
        c.putheader("X-A2A-Token", ADMIN)
        c.putheader("X-A2A-SHA256", "0" * 64)
        c.putheader("Content-Length", str(2 * 1048576))
        c.endheaders()  # no body is sent: the hub must answer from the header alone
        r = c.getresponse()
        self.assertEqual(r.status, 413)
        r.read()
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        c.request("POST", "/api/files/send?to=pa&name=chunked.bin", body=iter([b"abc"]),
                  headers={"X-A2A-Token": ADMIN, "X-A2A-SHA256": sha(b"abc")}, encode_chunked=True)
        r = c.getresponse()
        self.assertEqual(r.status, 411)
        r.read()
        c.close()
        self.assertNotIn("big.bin", self.inbox("pa"))

    # X3 + X11 (transfer_rejected)
    def test_x3_wrong_sha256_rejected(self):
        out = self.admin_send("pa", "x3.bin", b"hello", digest=sha(b"other"), expect=400)
        self.assertIn("sha256", out["error"])
        self.assertTrue(any(a["detail"].get("name") == "x3.bin" for a in self.audit("transfer_rejected")))
        self.assertNotIn("x3.bin", self.inbox("pa"))

    # X4
    def test_x4_damaged_stored_file_refused_by_receiver(self):
        good = b"the real content"
        tid = "x4" + os.urandom(5).hex()
        os.makedirs(os.path.dirname(self.hub.transfer_file(tid)), exist_ok=True)
        with open(self.hub.transfer_file(tid), "wb") as f:
            f.write(b"the REAL content")  # same size, other bytes
        self.hub.new_transfer("admin:ahmed", "pc:pa", "x4.txt", len(good), sha(good), "stored", tid=tid)
        self.hub.store.notify()
        t = self.wait_transfer(tid)
        self.assertEqual(t["status"], "failed")
        self.assertIn("sha256", t["error"])
        self.assertFalse(any(n.startswith("x4") for n in self.inbox("pa")), self.inbox("pa"))

    # X5
    def test_x5_who_may_send(self):
        self.upload("/api/worker/files/send?to=pa&name=x5.txt", b"x", token="tpp", expect=403)
        self.admin_send("pp", "x5.txt", b"x", expect=400)
        self.admin_send("nobody", "x5.txt", b"x", expect=400)
        self.admin_send("pa", "x5.txt", b"x", token="tb", expect=401)  # PC token on an admin route
        self.admin_send("pa", "x5.txt", b"x", token="wrong", expect=401)

    # X6
    def test_x6_bad_names_and_duplicates(self):
        for bad in ("../x.txt", "..\\x", "CON", "", "a:b", "x."):
            self.admin_send("pa", bad, b"x", expect=400)
        a = self.admin_send("pa", "dup.txt", b"one")["transfer_id"]
        self.wait_transfer(a)
        b = self.admin_send("pa", "dup.txt", b"two")["transfer_id"]
        self.wait_transfer(b)
        inbox = self.inbox("pa")
        self.assertIn("dup.txt", inbox)
        self.assertIn("dup (2).txt", inbox)
        with open(os.path.join(self.wd["pa"], "a2a-inbox", "dup (2).txt"), "rb") as f:
            self.assertEqual(f.read(), b"two")

    # X7
    def test_x7_pc_to_pc_with_cli(self):
        src = os.path.join(self.tmp, "x7 report.txt")
        with open(src, "wb") as f:
            f.write(b"from pa to pb")
        env = {k: v for k, v in os.environ.items() if k != "A2A_RECEIVER_CONFIG"}
        r = subprocess.run([sys.executable, os.path.join(ROOT, "receiver", "receiver.py"),
                            "--config", self.rcfg["pa"], "send-file", "--to", "pb", src],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        tid = r.stdout.strip().splitlines()[-1]
        self.assertEqual(self.wait_transfer(tid)["status"], "delivered")
        self.assertIn("x7 report.txt", self.inbox("pb"))
        ups = [a for a in self.audit("transfer_upload") if a["detail"]["transfer_id"] == tid]
        self.assertEqual((ups[0]["detail"]["src"], ups[0]["detail"]["dst"]), ("pc:pa", "pc:pb"))
        bad = subprocess.run([sys.executable, os.path.join(ROOT, "receiver", "receiver.py"),
                              "--config", self.rcfg["pa"], "send-file", "--to", "pp", src],
                             capture_output=True, text=True, env=env, timeout=60)
        self.assertNotEqual(bad.returncode, 0)

    # X8
    def test_x8_admin_fetches_file_from_pc(self):
        data = os.urandom(50_000)
        with open(os.path.join(self.wd["pa"], "x8.dat"), "wb") as f:
            f.write(data)
        t = self.api("POST", "/api/files/request", {"pc": "pa", "path": "x8.dat"})["transfer"]
        t = self.wait_transfer(t["id"], statuses=("stored", "failed"))
        self.assertEqual(t["status"], "stored", t)
        req = urllib.request.Request(f"{self.url}/api/files/{t['id']}/download",
                                     headers={"X-A2A-Token": ADMIN})
        with OPENER.open(req, timeout=30) as r:
            got = r.read()
            self.assertEqual(r.headers["X-A2A-SHA256"], sha(data))
        self.assertEqual(got, data)
        self.assertEqual(self.api("GET", f"/api/files/{t['id']}")["transfer"]["status"], "delivered")
        miss = self.api("POST", "/api/files/request", {"pc": "pa", "path": "C:\\no\\such\\file.txt"})
        t = self.wait_transfer(miss["transfer"]["id"], statuses=("failed", "stored"))
        self.assertEqual(t["status"], "failed")
        self.assertIn("not found", t["error"])
        self.api("POST", "/api/files/request", {"pc": "pp", "path": "x"}, expect=400)

    # X9
    def test_x9_admin_mcp_tools(self):
        env = dict(os.environ, A2A_HUB_URL=self.url, A2A_ADMIN_TOKEN=ADMIN, HOME=self.tmp,
                   USERPROFILE=self.tmp)
        env.pop("A2A_IN_TASK", None)
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "admin_mcp", "admin_mcp.py")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)

        def rpc(i, method, params=None):
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                       "params": params or {}}) + "\n").encode())
            p.stdin.flush()
            return json.loads(p.stdout.readline())

        def call(i, name, args):
            res = rpc(i, "tools/call", {"name": name, "arguments": args})["result"]
            return res.get("isError", False), res["content"][0]["text"]

        try:
            rpc(1, "initialize", {"protocolVersion": "2025-06-18"})
            src = os.path.join(self.tmp, "x9 mcp.txt")
            with open(src, "wb") as f:
                f.write(b"via the admin mcp")
            err, txt = call(2, "send_file", {"to": "pb", "path": src, "wait_seconds": 20})
            self.assertFalse(err, txt)
            self.assertIn("status=delivered", txt)
            self.assertIn("x9 mcp.txt", self.inbox("pb"))
            with open(os.path.join(self.wd["pb"], "x9 back.txt"), "wb") as f:
                f.write(b"back to the admin")
            save = os.path.join(self.tmp, "fetched") + os.sep
            err, txt = call(3, "fetch_file", {"pc": "pb", "path": "x9 back.txt", "save_to": save,
                                              "wait_seconds": 20})
            self.assertFalse(err, txt)
            with open(os.path.join(save, "x9 back.txt"), "rb") as f:
                self.assertEqual(f.read(), b"back to the admin")
            err, txt = call(4, "list_transfers", {"limit": 50})
            self.assertIn("x9 mcp.txt", txt)
            err, txt = call(5, "send_file", {"to": "pb", "path": os.path.join(self.tmp, "missing.txt")})
            self.assertTrue(err)
            err, txt = call(6, "fetch_file", {"pc": "pb", "path": "nope.txt", "wait_seconds": 20})
            self.assertIn("did not come", txt)
        finally:
            p.stdin.close()
            p.wait(10)
            p.stdout.close()

    # X10
    def test_x10_sweep_expires_old_transfers(self):
        tid = "x10" + os.urandom(4).hex()
        with open(self._mk(tid), "wb") as f:
            f.write(b"old")
        self.hub.new_transfer("admin:ahmed", "pc:poff", "x10.txt", 3, sha(b"old"), "stored", tid=tid)
        self.hub.store.x("UPDATE transfers SET created=? WHERE id=?", (time.time() - 25 * 3600, tid))
        self.hub.sweep()
        t = self.api("GET", f"/api/files/{tid}")["transfer"]
        self.assertEqual(t["status"], "expired")
        self.assertFalse(os.path.exists(self.hub.transfer_file(tid)))
        self.assertTrue(any(a["detail"].get("transfer_id") == tid for a in self.audit("transfer_expired")))

    def _mk(self, tid):
        os.makedirs(os.path.dirname(self.hub.transfer_file(tid)), exist_ok=True)
        return self.hub.transfer_file(tid)

    # X11
    def test_x11_audit_events(self):
        tid = self.admin_send("pb", "x11.txt", b"audit me")["transfer_id"]
        self.wait_transfer(tid)
        self.admin_send("pb", "x11b.txt", b"x", digest=sha(b"y"), expect=400)
        for ev in ("transfer_upload", "transfer_delivered", "transfer_rejected"):
            self.assertTrue(self.audit(ev), ev)
        self.assertTrue(any(a["detail"].get("transfer_id") == tid for a in self.audit("transfer_delivered")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
