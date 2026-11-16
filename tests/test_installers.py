"""Tests for the BUILT programs (run build\\build.py first).

Starts TeamA2A-Hub.exe on a test port, runs the client setup steps into a temp LOCALAPPDATA,
starts the installed TeamA2A-Agent.exe (with fake_claude), and talks through TeamA2A-MCP.exe.
It does NOT create scheduled tasks, registry entries, power settings or Claude MCP entries.
Run from the project folder:  python -m unittest tests.test_installers -v
"""
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "build", "work", "bin")
FAKE = os.path.join(ROOT, "tests", "fake_claude.py")
PORT = 8797
URL = f"http://127.0.0.1:{PORT}"
OWNER = "owner-test-token"
NOWIN = subprocess.CREATE_NO_WINDOW


def http(method, path, body=None, token=OWNER):
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-A2A-Token": token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


@unittest.skipUnless(os.path.exists(os.path.join(BIN, "TeamA2A-Hub.exe")), "run build\\build.py first")
class Installers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="a2a_inst_")
        cls.local = os.path.join(cls.tmp, "LocalAppData")
        os.makedirs(cls.local)
        hubdir = os.path.join(cls.tmp, "hub")
        os.makedirs(hubdir)
        cls.hub_cfg = os.path.join(hubdir, "hub.json")
        json.dump({"port": PORT, "bind": "127.0.0.1", "data_dir": os.path.join(hubdir, "data"),
                   "report_hour": 99, "join_code": "JOIN-TEST", "admins": {"owner": OWNER}, "pcs": {}},
                  open(cls.hub_cfg, "w"))
        cls.hub = subprocess.Popen([os.path.join(BIN, "TeamA2A-Hub.exe")],
                                   env=dict(os.environ, A2A_HUB_CONFIG=cls.hub_cfg))
        for _ in range(60):
            try:
                if http("GET", "/health")[0] == 200:
                    break
            except Exception:
                time.sleep(0.5)
        cls.agent = None

    @classmethod
    def tearDownClass(cls):
        for p in (cls.agent, cls.hub):
            if p:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
        time.sleep(1)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_1_hub_exe_serves_dashboard(self):
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(URL + "/") as r:
            self.assertIn(b"Team A2A Hub", r.read())
        self.assertEqual(http("GET", "/api/joincode")[1]["join_code"], "JOIN-TEST")

    def test_2_setup_agent_mcp(self):
        os.environ["LOCALAPPDATA"] = self.local  # setup and MCP use it for their folder
        sys.path.insert(0, os.path.join(ROOT, "client"))
        setup = importlib.import_module("setup")
        importlib.reload(setup)
        setup.BUNDLE = os.path.join(ROOT, "build", "work")  # has bin\\TeamA2A-Agent.exe, TeamA2A-MCP.exe
        self.assertTrue(setup.APP_DIR.startswith(self.local))
        lines = []
        inst = setup.Installer(lines.append)
        inst.step_copy()
        self.assertTrue(os.path.exists(os.path.join(setup.APP_DIR, "TeamA2A-Agent.exe")))
        with self.assertRaises(RuntimeError):
            inst.step_join(URL, "wrong", "x")
        hub_url, name, token, pubkey = inst.step_join("127.0.0.1:%d" % PORT, "JOIN-TEST", "test pc!")
        self.assertEqual((hub_url, name), (URL, "testpc"))
        self.assertRegex(pubkey or "", r"^[0-9a-f]{64}$")  # the hub's update key, pinned at enroll
        inst.step_config(hub_url, name, token, "claude", pubkey)
        with open(os.path.join(setup.APP_DIR, "receiver.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["update_pubkey"], pubkey)
        self.assertEqual(http("POST", "/api/send", {"to": "testpc", "message": "x"})[0], 400)  # pending
        self.assertEqual(http("POST", "/api/pcs/testpc/approve", {})[0], 200)
        # a second run keeps the same membership
        self.assertEqual(inst.step_join(URL, "JOIN-TEST", "other")[1], "testpc")

        # run the installed agent with fake_claude and no MCP syncing (keeps the real Claude config clean)
        cfgp = os.path.join(setup.APP_DIR, "receiver.json")
        cfg = json.load(open(cfgp))
        cfg.update({"claude_cmd": [sys.executable, FAKE], "mcp_cmd": None, "heartbeat_s": 1, "poll_wait_s": 3})
        json.dump(cfg, open(cfgp, "w"))
        env = dict(os.environ, LOCALAPPDATA=self.local, FAKE_CLAUDE_DIR=os.path.join(self.tmp, "fs"))
        type(self).agent = subprocess.Popen([os.path.join(setup.APP_DIR, "TeamA2A-Agent.exe")], env=env)
        for _ in range(40):
            pcs = http("GET", "/api/pcs")[1]["pcs"]
            if pcs and pcs[0]["online"]:
                break
            time.sleep(0.5)
        self.assertTrue(pcs[0]["online"], pcs)
        time.sleep(1)
        self.assertTrue(os.path.exists(os.path.join(setup.APP_DIR, "logs", "receiver.jsonl")), os.listdir(setup.APP_DIR))

        # the PC is a regular user: its own MCP cannot list PCs
        mcp = os.path.join(setup.APP_DIR, "TeamA2A-MCP.exe")
        menv = {k: v for k, v in env.items() if not k.startswith("A2A_")}

        def call_mcp(*msgs):
            body = "".join(json.dumps({"jsonrpc": "2.0", "id": i + 1, "method": m, "params": p}) + "\n"
                           for i, (m, p) in enumerate(msgs)).encode()
            out = subprocess.run([mcp], input=body, capture_output=True, env=menv, timeout=120,
                                 creationflags=NOWIN).stdout.decode("utf-8")
            return [json.loads(x) for x in out.splitlines()]

        r = call_mcp(("tools/call", {"name": "list_pcs", "arguments": {}}))
        self.assertTrue(r[0]["result"]["isError"])
        self.assertIn("401", r[0]["result"]["content"][0]["text"])

        # owner promotes it: the same MCP (token from receiver.json) now works and sends a task
        self.assertEqual(http("POST", "/api/pcs/testpc/role", {"role": "admin"})[0], 200)
        r = call_mcp(("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
                     ("tools/call", {"name": "list_pcs", "arguments": {}}),
                     ("tools/call", {"name": "send", "arguments": {"to": "testpc", "message": "hello exe",
                                                                   "wait_seconds": 30}}))
        self.assertIn("testpc: ONLINE", r[1]["result"]["content"][0]["text"])
        self.assertIn("ECHO: hello exe | turn=1 | sp=yes", r[2]["result"]["content"][0]["text"])

        # uninstall removes the folder (Claude MCP step disabled for the test)
        setup.find_claude = lambda: None
        setup.stop_agent = lambda: subprocess.run(["taskkill", "/PID", str(self.agent.pid), "/T", "/F"],
                                                  capture_output=True)
        time.sleep(0.5)
        orig_ps = setup.ps
        setup.ps = lambda script, timeout=180: (0, "")  # no Task Scheduler calls in the test
        try:
            setup.uninstall(lines.append)
        finally:
            setup.ps = orig_ps
        time.sleep(1)
        self.assertFalse(os.path.exists(setup.APP_DIR))


if __name__ == "__main__":
    unittest.main(verbosity=2)
