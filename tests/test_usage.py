"""Plan limits (5-hour / weekly) of each PC: receiver reads them, hub stores them, admin MCP shows them.

Run from the A2ALocal folder:  python -m unittest tests.test_usage -v
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "hub"), os.path.join(ROOT, "receiver"), os.path.join(ROOT, "admin_mcp")]
import hub as hubmod  # noqa: E402
import receiver as rcvmod  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fake_claude.py")
ADMIN_TOKEN = "admin-token-usage"

# A real line from Claude Code 2.1.325 (`claude -p --output-format stream-json --verbose`).
REAL_LINE = ('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1794831000,'
             '"rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":"org_level_disabled",'
             '"isUsingOverage":false,"unifiedWindows":{"five_hour":{"utilization":0.1,"resetsAt":1794831000,'
             '"status":"allowed"},"seven_day":{"utilization":0.01,"resetsAt":1795388400,"status":"allowed"}}},'
             '"uuid":"fe68a6c2","session_id":"41de283a"}')


class Parse(unittest.TestCase):
    def test_parse_real_line(self):
        lim = rcvmod.parse_limits("noise\n" + REAL_LINE + '\n{"type":"result","result":"OK"}', "task")
        self.assertEqual(lim["windows"]["five_hour"], {"pct": 10, "resets_at": 1794831000, "status": "allowed"})
        self.assertEqual(lim["windows"]["seven_day"]["pct"], 1)
        self.assertEqual(lim["source"], "task")

    def test_parse_last_event_wins_and_none(self):
        newer = REAL_LINE.replace('"utilization":0.1,', '"utilization":0.2,')
        self.assertEqual(rcvmod.parse_limits(REAL_LINE + "\n" + newer, "probe")["windows"]["five_hour"]["pct"], 20)
        self.assertIsNone(rcvmod.parse_limits('{"type":"result","result":"OK"}', "task"))
        self.assertIsNone(rcvmod.parse_limits("", "task"))
        self.assertIsNone(rcvmod.parse_limits('{"type":"rate_limit_event","rate_limit_info":{}}', "task"))

    def test_parse_old_cli_single_window(self):
        line = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "rateLimitType": "seven_day", "utilization": 0.81, "resetsAt": 5}})
        lim = rcvmod.parse_limits(line, "task")
        self.assertEqual(lim["windows"], {"seven_day": {"pct": 81, "resets_at": 5, "status": "allowed_warning"}})

    def test_stream_cmd(self):
        self.assertEqual(rcvmod.stream_cmd(rcvmod.DEFAULT_CMD),
                         ["claude", "-p", "--output-format", "stream-json", "--dangerously-skip-permissions",
                          "--verbose"])
        self.assertEqual(rcvmod.DEFAULT_CMD[3], "json")  # the default list itself is not changed
        custom = ["python", "x.py"]
        self.assertEqual(rcvmod.stream_cmd(custom), custom)
        twice = rcvmod.stream_cmd(["claude", "-p", "--verbose", "--output-format", "json"])
        self.assertEqual(twice.count("--verbose"), 1)

    def test_fmt_limits(self):
        os.environ.setdefault("A2A_ADMIN_TOKEN", "x")
        import admin_mcp
        t = 1000000.0
        lim = {"windows": {"seven_day": {"pct": 2, "resets_at": t + 6 * 86400 + 13 * 3600, "status": "allowed"},
                           "five_hour": {"pct": 10, "resets_at": t + 2 * 3600 + 19 * 60, "status": "allowed"}},
               "at": t - 12 * 60, "source": "probe"}
        self.assertEqual(admin_mcp.fmt_limits(lim, t),
                         "5h 10% (resets in 2h 19m) | week 2% (resets in 6d 13h) | data 12m old, from probe")
        lim["at"] = t - 7200
        self.assertTrue(admin_mcp.fmt_limits(lim, t).endswith("(OLD)"))
        self.assertIn("no data yet", admin_mcp.fmt_limits(None, t))


class Live(unittest.TestCase):
    """Hub + two receivers on fake_claude: pa reads limits from tasks only, pb probes when idle."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="a2a_usage_")
        os.environ["FAKE_CLAUDE_DIR"] = os.path.join(cls.tmp, "sessions")
        rcvmod.LOG_DIR = os.path.join(cls.tmp, "receiver_logs")
        cfg_path = os.path.join(cls.tmp, "hub.json")
        json.dump({"port": 0, "bind": "127.0.0.1", "data_dir": "data", "report_hour": 99,
                   "admins": {"ahmed": ADMIN_TOKEN},
                   "pcs": {"pa": {"token": "ta"}, "pb": {"token": "tb"}}}, open(cfg_path, "w"))
        hubmod.CONFIG_PATH = cfg_path
        cls.srv, cls.hub, cls.stop = hubmod.serve(hubmod.load_config(cfg_path))
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.receivers = []
        for name, tok, probe in (("pa", "ta", 0), ("pb", "tb", 1)):
            wd = os.path.join(cls.tmp, "wd_" + name)
            os.makedirs(wd)
            p = os.path.join(cls.tmp, name + ".json")
            json.dump({"name": name, "hub_url": cls.url, "token": tok, "workdir": wd,
                       "claude_cmd": [sys.executable, FAKE], "heartbeat_s": 1, "poll_wait_s": 1,
                       "usage_probe_s": probe, "auto_update": False}, open(p, "w"))
            r = rcvmod.Receiver(rcvmod.load_config(p))
            threading.Thread(target=r.loop, daemon=True).start()
            cls.receivers.append(r)

    @classmethod
    def tearDownClass(cls):
        for r in cls.receivers:
            r.stop.set()
        cls.stop.set()
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.hub.store.db.close()
        time.sleep(0.5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def api(cls, method, path, body=None, token=ADMIN_TOKEN):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(cls.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-A2A-Token": token})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())

    def pcs(self):
        return {p["name"]: p for p in self.api("GET", "/api/pcs")["pcs"]}

    def wait_limits(self, pc, source, t=15):
        end = time.time() + t
        while time.time() < end:
            lim = self.pcs()[pc]["limits"]
            if lim and lim.get("source") == source:
                return lim
            time.sleep(0.2)
        self.fail(f"no limits from {source} for {pc}")

    def test_1_limits_from_task(self):
        self.assertIsNone(self.pcs()["pa"]["limits"])  # no probe on pa, no task yet
        self.api("POST", "/api/send", {"to": "pa", "message": "hello"})
        lim = self.wait_limits("pa", "task")
        self.assertEqual(lim["windows"]["five_hour"]["pct"], 37)
        self.assertEqual(lim["windows"]["seven_day"]["pct"], 5)

    def test_2_limits_from_idle_probe(self):
        lim = self.wait_limits("pb", "probe")
        self.assertEqual(lim["windows"]["five_hour"]["pct"], 37)

    def test_3_bad_limits_keep_old_value(self):
        self.wait_limits("pb", "probe")
        for bad in ("text", {"windows": "x"}, {"windows": {"a": "b" * 5000}}, [1, 2]):
            self.api("POST", "/api/worker/next", {"wait": 0, "slots": 0, "running": [], "limits": bad},
                     token="tb")
            self.assertEqual(self.pcs()["pb"]["limits"]["windows"]["five_hour"]["pct"], 37)

    def test_4_team_usage_tool(self):
        self.wait_limits("pb", "probe")
        os.environ["A2A_ADMIN_TOKEN"] = ADMIN_TOKEN
        import admin_mcp
        admin_mcp.HUB, admin_mcp.TOKEN = self.url, ADMIN_TOKEN
        txt = admin_mcp.t_team_usage({})
        self.assertIn("- pb (", txt)
        self.assertIn("5h 37%", txt)
        self.assertIn("week 5%", txt)


if __name__ == "__main__":
    unittest.main()
