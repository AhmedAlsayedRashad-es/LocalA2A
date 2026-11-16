"""Direct file transfer: bytes straight between PCs, the hub only signs tickets (and audits).

Run from the project folder:  python -m unittest discover -s tests -p "test_direct.py" -v
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
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "hub"), os.path.join(ROOT, "receiver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import a2a_direct  # noqa: E402
import hub as hubmod  # noqa: E402
import receiver as rcvmod  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fake_claude.py")
ADMIN = "admin-direct-token"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Direct(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="a2a_direct_")
        os.environ["FAKE_CLAUDE_DIR"] = os.path.join(cls.tmp, "sessions")
        cls.old_log_dir = rcvmod.LOG_DIR
        rcvmod.LOG_DIR = os.path.join(cls.tmp, "receiver_logs")
        cls.cfg_path = os.path.join(cls.tmp, "hub.json")
        cfg = {"port": 0, "bind": "127.0.0.1", "data_dir": "data", "report_hour": 99, "task_timeout_s": 60,
               "max_transfer_mb": 1, "admins": {"ahmed": ADMIN},
               "pcs": {"pa": {"token": "ta"}, "pb": {"token": "tb"}, "poff": {"token": "toff"},
                       "pz": {"token": "tz"}, "pp": {"token": "tpp", "approved": False}}}
        with open(cls.cfg_path, "w") as f:
            json.dump(cfg, f)
        cls.old_cfg = hubmod.CONFIG_PATH
        hubmod.CONFIG_PATH = cls.cfg_path
        cls.srv, cls.hub, cls.stop = hubmod.serve(hubmod.load_config(cls.cfg_path))
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.wd, cls.rcfg, cls.rcv = {}, {}, {}
        for name, tok, direct in (("pa", "ta", True), ("pb", "tb", True), ("poff", "toff", False)):
            wd = os.path.join(cls.tmp, "wd_" + name)
            os.makedirs(wd)
            cls.wd[name] = wd
            path = os.path.join(cls.tmp, f"{name}.json")
            with open(path, "w") as f:
                json.dump({"name": name, "hub_url": cls.url, "token": tok, "workdir": wd,
                           "claude_cmd": [sys.executable, FAKE], "heartbeat_s": 1, "poll_wait_s": 2,
                           "auto_update": False, "direct": direct, "direct_port": 0,
                           "direct_bind": "127.0.0.1"}, f)
            cls.rcfg[name] = path
            r = rcvmod.Receiver(rcvmod.load_config(path))
            threading.Thread(target=r.loop, daemon=True).start()
            cls.rcv[name] = r
        # pz says it has a direct port, but nothing listens there (firewall closed, PC gone ...)
        cls.api("POST", "/api/worker/next", {"wait": 0, "slots": 0, "running": [], "direct_port": 9}, token="tz")
        end = time.time() + 10
        while time.time() < end:
            pcs = {p["name"]: p for p in cls.api("GET", "/api/pcs")["pcs"]}
            if all(pcs[n]["online"] for n in ("pa", "pb", "poff")) and pcs["pa"]["direct_port"] \
                    and pcs["pb"]["direct_port"]:
                break
            time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        for r in cls.rcv.values():
            r.stop.set()
            if getattr(r, "direct", None):
                r.direct.stop()
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
        assert expect is None or code == expect, f"{method} {path}: HTTP {code} {out}"
        return out

    def file(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def ticket_put(self, to, name, data, token=ADMIN, route="/api/files/direct", **over):
        body = {"op": "put", "to": to, "name": name, "size": len(data), "sha256": sha(data), **over}
        return self.api("POST", route, body, token=token)

    def raw(self, method, url, ticket, data=b"", length=None):
        u = urlparse(url)
        c = http.client.HTTPConnection(u.hostname, u.port, timeout=10)
        c.putrequest(method, u.path)
        c.putheader("X-A2A-Ticket", ticket)
        if method == "PUT":
            c.putheader("Content-Length", str(len(data) if length is None else length))
        c.endheaders()
        if method == "PUT":
            try:
                c.send(data)
            except OSError:
                pass  # the PC refused before it read the body and closed the connection
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def transfer(self, tid):
        return self.api("GET", f"/api/files/{tid}")["transfer"]

    def wait_status(self, tid, statuses, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            t = self.transfer(tid)
            if t["status"] in statuses:
                return t
            time.sleep(0.1)
        self.fail(f"{tid} did not reach {statuses}: {t}")

    def inbox(self, pc):
        d = os.path.join(self.wd[pc], "a2a-inbox")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def audit(self, event):
        return [a for a in self.api("GET", "/api/audit?limit=2000")["audit"] if a["event"] == event]

    def mcp(self, env_extra=None):
        env = dict(os.environ, A2A_HUB_URL=self.url, A2A_ADMIN_TOKEN=ADMIN, HOME=self.tmp, USERPROFILE=self.tmp,
                   **(env_extra or {}))
        env.pop("A2A_IN_TASK", None)
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "admin_mcp", "admin_mcp.py")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)

        def call(name, args):
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                       "params": {"name": name, "arguments": args}}) + "\n").encode())
            p.stdin.flush()
            res = json.loads(p.stdout.readline())["result"]
            return res.get("isError", False), res["content"][0]["text"]

        def close():
            p.stdin.close()
            p.wait(10)
            p.stdout.close()
        return call, close

    # D1
    def test_d1_admin_put_direct(self):
        data = os.urandom(200_000)
        d = self.ticket_put("pa", "d1.bin", data)
        self.assertTrue(d["url"].startswith("http://127.0.0.1:"))
        out = a2a_direct.put_file(d["url"], d["ticket"], self.file("d1.bin", data))
        self.assertTrue(out["ok"])
        with open(os.path.join(self.wd["pa"], "a2a-inbox", "d1.bin"), "rb") as f:
            self.assertEqual(f.read(), data)
        t = self.wait_status(d["transfer_id"], ("delivered",))
        self.assertEqual(t["sha256"], sha(data))
        self.assertFalse(os.path.exists(self.hub.transfer_file(d["transfer_id"])), "the hub must not hold the bytes")
        ev = [a for a in self.audit("transfer_delivered") if a["detail"]["transfer_id"] == d["transfer_id"]]
        self.assertEqual(ev[0]["detail"]["mode"], "direct")
        self.assertTrue(any(a["detail"]["id"] == d["transfer_id"] for a in self.audit("transfer_direct")))

    # D2
    def test_d2_tickets_cannot_be_forged_moved_or_reused(self):
        data = b"ticket test"
        d = self.ticket_put("pa", "d2.txt", data)
        payload, mac = d["ticket"].split(".")
        claims = json.loads(a2a_direct._unb64(payload))
        forged = a2a_direct._b64(json.dumps(dict(claims, name="evil.txt"), sort_keys=True,
                                            separators=(",", ":")).encode()) + "." + mac
        self.assertEqual(self.raw("PUT", d["url"], forged, data)[0], 403)
        self.assertEqual(self.raw("PUT", d["url"], "garbage", data)[0], 403)
        self.assertEqual(self.raw("PUT", d["url"], a2a_direct.make_ticket("wrong-token", claims), data)[0], 403)
        expired = a2a_direct.make_ticket("ta", dict(claims, exp=time.time() - 1))
        self.assertEqual(self.raw("PUT", d["url"], expired, data)[0], 403)
        other = d["url"].rsplit("/", 1)[0] + "/someotherid"
        self.assertEqual(self.raw("PUT", other, d["ticket"], data)[0], 403)
        # a ticket for pb does not work at pa
        db = self.ticket_put("pb", "d2b.txt", data)
        pa_url = d["url"].rsplit("/", 1)[0] + "/" + db["transfer_id"]
        self.assertEqual(self.raw("PUT", pa_url, db["ticket"], data)[0], 403)
        # the real ticket works once
        self.assertEqual(self.raw("PUT", d["url"], d["ticket"], data)[0], 200)
        self.assertEqual(self.raw("PUT", d["url"], d["ticket"], data)[0], 409)
        self.assertEqual(self.inbox("pa").count("d2.txt"), 1)
        self.assertNotIn("evil.txt", self.inbox("pa"))

    # D3 + D4
    def test_d3_d4_damaged_or_wrong_length_refused(self):
        d = self.ticket_put("pa", "d3.txt", b"the real bytes")
        code, body = self.raw("PUT", d["url"], d["ticket"], b"the REAL bytes")
        self.assertEqual(code, 400)
        self.assertIn(b"sha256", body)
        t = self.wait_status(d["transfer_id"], ("failed",))
        self.assertIn("sha256", t["error"])
        self.assertFalse(any(n.startswith("d3") for n in self.inbox("pa")))
        d = self.ticket_put("pa", "d4.txt", b"12345")
        code, body = self.raw("PUT", d["url"], d["ticket"], b"1234")
        self.assertEqual(code, 400)
        self.assertIn(b"Content-Length", body)
        self.assertNotIn("d4.txt", self.inbox("pa"))

    # D5
    def test_d5_admin_get_direct(self):
        data = os.urandom(70_000)
        with open(os.path.join(self.wd["pb"], "d5.dat"), "wb") as f:
            f.write(data)
        d = self.api("POST", "/api/files/direct", {"op": "get", "pc": "pb", "path": "d5.dat"})
        code, body = self.raw("GET", d["url"], d["ticket"])
        self.assertEqual((code, body), (200, data))
        t = self.wait_status(d["transfer_id"], ("delivered",))
        self.assertEqual(t["sha256"], sha(data))
        d = self.api("POST", "/api/files/direct", {"op": "get", "pc": "pb", "path": "C:\\no\\such.txt"})
        code, body = self.raw("GET", d["url"], d["ticket"])
        self.assertEqual(code, 404)
        self.assertIn("not found", self.wait_status(d["transfer_id"], ("failed",))["error"])

    # D6 + D7 + D8
    def test_d6_d7_d8_mcp_direct_and_fallback(self):
        call, close = self.mcp()
        try:
            err, txt = call("send_file", {"to": "pa", "path": self.file("d6 mcp.txt", b"mcp direct")})
            self.assertFalse(err, txt)
            self.assertIn("straight to pa", txt)
            self.assertIn("status=delivered", txt)
            self.assertIn("d6 mcp.txt", self.inbox("pa"))
            with open(os.path.join(self.wd["pa"], "d6 back.txt"), "wb") as f:
                f.write(b"back direct")
            save = os.path.join(self.tmp, "got") + os.sep
            err, txt = call("fetch_file", {"pc": "pa", "path": "d6 back.txt", "save_to": save})
            self.assertFalse(err, txt)
            self.assertIn("(direct", txt)
            with open(os.path.join(save, "d6 back.txt"), "rb") as f:
                self.assertEqual(f.read(), b"back direct")
            err, txt = call("fetch_file", {"pc": "pa", "path": "missing.txt"})
            self.assertIn("did not come", txt)
            # D7: poff has no direct server: the hub relay delivers it
            err, txt = call("send_file", {"to": "poff", "path": self.file("d7.txt", b"relay"), "wait_seconds": 20})
            self.assertFalse(err, txt)
            self.assertIn("through the hub", txt)
            self.assertIn("status=delivered", txt)
            self.assertIn("d7.txt", self.inbox("poff"))
            # D8: pz claims a port where nothing listens: fall back, the file waits on the hub
            t0 = time.time()
            err, txt = call("send_file", {"to": "pz", "path": self.file("d8.txt", b"later"), "wait_seconds": 1})
            self.assertFalse(err, txt)
            self.assertIn("direct not possible", txt)
            self.assertRegex(txt, r"status=(stored|offered)")
            self.assertLess(time.time() - t0, 30)
        finally:
            close()

    # D9
    def test_d9_pc_to_pc_cli(self):
        src = self.file("d9 pc.txt", b"pa to pb, direct")
        env = {k: v for k, v in os.environ.items() if k != "A2A_RECEIVER_CONFIG"}
        r = subprocess.run([sys.executable, os.path.join(ROOT, "receiver", "receiver.py"),
                            "--config", self.rcfg["pa"], "send-file", "--to", "pb", src],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("straight", r.stderr)
        tid = r.stdout.strip().splitlines()[-1]
        t = self.wait_status(tid, ("delivered",))
        self.assertEqual((t["src"], t["dst"]), ("pc:pa", "pc:pb"))
        self.assertIn("d9 pc.txt", self.inbox("pb"))
        r = subprocess.run([sys.executable, os.path.join(ROOT, "receiver", "receiver.py"),
                            "--config", self.rcfg["pa"], "send-file", "--to", "poff", src],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("through the hub", r.stderr)

    # D10
    def test_d10_hub_refusals(self):
        data = b"x"
        self.api("POST", "/api/files/direct", {"op": "put", "to": "pp", "name": "x", "size": 1, "sha256": sha(data)},
                 expect=400)
        self.api("POST", "/api/files/direct", {"op": "put", "to": "nope", "name": "x", "size": 1,
                                               "sha256": sha(data)}, expect=400)
        self.api("POST", "/api/files/direct", {"op": "put", "to": "pa", "name": "../x", "size": 1,
                                               "sha256": sha(data)}, expect=400)
        self.api("POST", "/api/files/direct", {"op": "put", "to": "pa", "name": "x", "size": 2 * 1048576,
                                               "sha256": sha(data)}, expect=400)
        self.api("POST", "/api/files/direct", {"op": "put", "to": "pa", "name": "x", "size": 1, "sha256": "zz"},
                 expect=400)
        self.api("POST", "/api/files/direct", {"op": "put", "to": "poff", "name": "x", "size": 1,
                                               "sha256": sha(data)}, expect=409)
        self.api("POST", "/api/files/direct", {"op": "get", "pc": "poff", "path": "x"}, expect=409)
        self.api("POST", "/api/files/direct", {"op": "put", "to": "pa", "name": "x", "size": 1,
                                               "sha256": sha(data)}, token="tb", expect=401)
        self.api("POST", "/api/worker/files/direct", {"op": "get", "pc": "pa", "path": "x"}, token="tb",
                 expect=403)
        self.api("POST", "/api/worker/files/direct", {"op": "put", "to": "pa", "name": "x", "size": 1,
                                                      "sha256": sha(data)}, token="tpp", expect=403)
        self.api("POST", "/api/worker/files/direct", {"op": "put", "to": "pa", "name": "x", "size": 1,
                                                      "sha256": sha(data)}, token="ta", expect=400)  # to itself

    # D11
    def test_d11_sweep_fails_stale_direct(self):
        d = self.ticket_put("pa", "d11.txt", b"never sent")
        self.hub.store.x("UPDATE transfers SET created=? WHERE id=?", (time.time() - 3600, d["transfer_id"]))
        self.hub.sweep()
        t = self.transfer(d["transfer_id"])
        self.assertEqual(t["status"], "failed")
        self.assertIn("did not finish", t["error"])

    # D12
    def test_d12_health_and_ticket_unit(self):
        port = self.rcv["pa"].direct.port
        with OPENER.open(f"http://127.0.0.1:{port}/a2a/health", timeout=5) as r:
            self.assertEqual(json.loads(r.read())["pc"], "pa")
        t = a2a_direct.make_ticket("k", {"id": "1", "exp": time.time() + 60})
        self.assertEqual(a2a_direct.check_ticket("k", t)["id"], "1")
        for bad in ("", "a.b.c", t + "0", t.replace(".", "x")):
            with self.assertRaises(PermissionError):
                a2a_direct.check_ticket("k", bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
