"""Feature 1: parallel sessions on one PC (real hub + real receivers + fake_claude.py).

Run from the project folder:  python -m unittest discover -s tests -p "test_parallel.py" -v
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
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "hub"), os.path.join(ROOT, "receiver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import hub as hubmod  # noqa: E402
import receiver as rcvmod  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fake_claude.py")
ADMIN = "admin-par-token"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp() if s else None


class Parallel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="a2a_par_")
        os.environ["FAKE_CLAUDE_DIR"] = os.path.join(cls.tmp, "sessions")
        cls.old_log_dir = rcvmod.LOG_DIR
        rcvmod.LOG_DIR = os.path.join(cls.tmp, "receiver_logs")
        cls.cfg_path = os.path.join(cls.tmp, "hub.json")
        cfg = {"port": 0, "bind": "127.0.0.1", "data_dir": "data", "report_hour": 99,
               "task_timeout_s": 120, "admins": {"ahmed": ADMIN},
               "pcs": {"pA": {"token": "tA"}, "pB": {"token": "tB"}, "pOld": {"token": "tOld"}}}
        with open(cls.cfg_path, "w") as f:
            json.dump(cfg, f)
        cls.old_cfg = hubmod.CONFIG_PATH
        hubmod.CONFIG_PATH = cls.cfg_path
        cls.srv, cls.hub, cls.stop = hubmod.serve(hubmod.load_config(cls.cfg_path))
        cls.port = cls.srv.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.wd = {}
        cls.receivers = []
        for name, tok, mp in (("pA", "tA", 3), ("pB", "tB", 1)):
            wd = os.path.join(cls.tmp, "wd_" + name)
            os.makedirs(wd)
            cls.wd[name] = wd
            path = os.path.join(cls.tmp, f"{name}.json")
            with open(path, "w") as f:
                json.dump({"name": name, "hub_url": cls.url, "token": tok, "workdir": wd,
                           "claude_cmd": [sys.executable, FAKE], "heartbeat_s": 1, "poll_wait_s": 2,
                           "max_parallel": mp, "auto_update": False}, f)
            r = rcvmod.Receiver(rcvmod.load_config(path))
            threading.Thread(target=r.loop, daemon=True).start()
            cls.receivers.append(r)
        end = time.time() + 10
        while time.time() < end:
            on = {p["name"] for p in cls.api("GET", "/api/pcs")["pcs"] if p["online"]}
            if {"pA", "pB"} <= on:
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

    def send(self, **body):
        return self.api("POST", "/api/send", body)["tasks"][0]

    def wait(self, t, statuses=("done", "failed", "canceled", "needs_input"), timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            for turn in self.api("GET", f"/api/threads/{t['context_id']}")["turns"]:
                if turn["task_id"] == t["task_id"] and turn["status"] in statuses:
                    return turn
            time.sleep(0.1)
        self.fail(f"task {t['task_id']} did not reach {statuses}")

    # P1
    def test_p1_two_threads_run_at_once(self):
        t0 = time.time()
        a = self.send(to="pA", message="SLEEP 3")
        b = self.send(to="pA", message="SLEEP 3")
        ra, rb = self.wait(a), self.wait(b)
        took = time.time() - t0
        self.assertEqual((ra["status"], rb["status"]), ("done", "done"))
        self.assertLess(took, 5, f"two SLEEP 3 took {took:.1f} s: not parallel")
        self.assertLess(ts(ra["started"]), ts(rb["finished"]))
        self.assertLess(ts(rb["started"]), ts(ra["finished"]))

    # P2
    def test_p2_same_thread_stays_in_order(self):
        a = self.send(to="pA", message="SLEEP 2")
        b = self.send(context_id=a["context_id"], message="SLEEP 2 again")
        ra, rb = self.wait(a), self.wait(b)
        self.assertGreaterEqual(ts(rb["started"]) + 1, ts(ra["finished"]))
        self.assertIn("turn=2", rb["output"])

    # P3
    def test_p3_max_parallel_one_runs_one_at_a_time(self):
        t0 = time.time()
        a = self.send(to="pB", message="SLEEP 2")
        b = self.send(to="pB", message="SLEEP 2")
        ra, rb = self.wait(a), self.wait(b)
        took = time.time() - t0
        self.assertGreaterEqual(took, 4 - 1)
        first, second = sorted((ra, rb), key=lambda r: ts(r["started"]))
        self.assertGreaterEqual(ts(second["started"]) + 1, ts(first["finished"]))
        self.assertGreaterEqual(ts(second["finished"]) - ts(first["started"]), 4 - 1)

    # P4
    def test_p4_each_thread_has_its_own_folder(self):
        a = self.send(to="pA", message="CWD one")
        b = self.send(to="pA", message="CWD two")
        ra, rb = self.wait(a), self.wait(b)
        c = self.send(context_id=a["context_id"], message="CWD again")
        rc = self.wait(c)
        norm = lambda s: os.path.normcase(os.path.normpath(s.split("CWD: ", 1)[1].strip()))  # noqa: E731
        want = os.path.normcase(os.path.normpath(os.path.join(self.wd["pA"], "a2a-sessions", a["context_id"])))
        self.assertEqual(norm(ra["output"]), want)
        self.assertNotEqual(norm(ra["output"]), norm(rb["output"]))
        self.assertEqual(norm(rb["output"]), os.path.normcase(os.path.normpath(
            os.path.join(self.wd["pA"], "a2a-sessions", b["context_id"]))))
        self.assertEqual(norm(rc["output"]), want)

    # P5
    def test_p5_old_receiver_protocol(self):
        a = self.send(to="pOld", message="old one")
        b = self.send(to="pOld", message="old two")
        r1 = self.api("POST", "/api/worker/next", {"wait": 0, "version": "1.1"}, token="tOld")
        self.assertIsNotNone(r1["task"])
        self.assertNotIn("tasks", r1)
        self.assertEqual(r1["task"]["task_id"], a["task_id"])
        r2 = self.api("POST", "/api/worker/next", {"wait": 0, "version": "1.1"}, token="tOld")
        self.assertEqual(r2["task"]["task_id"], b["task_id"])
        first = self.wait(a, statuses=("failed",), timeout=5)
        self.assertEqual(first["error"], "receiver restarted while running")
        self.api("POST", "/api/tasks/" + b["task_id"] + "/cancel", {})

    # P6 + P7
    def test_p6_p7_never_more_than_max_parallel(self):
        ts_ = [self.send(to="pA", message="SLEEP 2") for _ in range(4)]
        peak, seen_busy = 0, None
        end = time.time() + 20
        while time.time() < end:
            pa = {p["name"]: p for p in self.api("GET", "/api/pcs")["pcs"]}["pA"]
            peak = max(peak, pa["running"])
            if pa["running"] >= 2 and seen_busy is None:
                seen_busy = pa
            states = [self.wait(t, statuses=("queued", "running", "done", "failed", "canceled"))["status"]
                      for t in ts_]
            if all(s == "done" for s in states):
                break
            time.sleep(0.2)
        self.assertLessEqual(peak, 3)
        self.assertGreaterEqual(peak, 2)
        for t in ts_:
            self.assertEqual(self.wait(t)["status"], "done")
        # P7: the /api/pcs entry of a busy PC
        self.assertIsNotNone(seen_busy)
        self.assertIsInstance(seen_busy["running"], int)
        self.assertIsInstance(seen_busy["running_tasks"], list)
        self.assertEqual(len(seen_busy["running_tasks"]), seen_busy["running"])
        self.assertIn(seen_busy["busy_task"], seen_busy["running_tasks"])

    # P8
    def test_p8_cancel_one_of_two(self):
        a = self.send(to="pA", message="SLEEP 20")
        b = self.send(to="pA", message="SLEEP 20")
        self.wait(a, statuses=("running",))
        self.wait(b, statuses=("running",))
        t0 = time.time()
        try:
            self.api("POST", f"/api/tasks/{a['task_id']}/cancel", {})
            self.assertEqual(self.wait(a, statuses=("canceled",), timeout=15)["status"], "canceled")
            self.assertLess(time.time() - t0, 10)
            self.assertEqual(self.wait(b, statuses=("running", "done", "failed", "canceled"))["status"],
                             "running")
        finally:
            self.api("POST", f"/api/tasks/{b['task_id']}/cancel", {})
            self.wait(b, statuses=("canceled", "done", "failed"), timeout=15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
