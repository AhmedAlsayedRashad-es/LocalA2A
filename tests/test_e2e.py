"""End-to-end tests: real hub + real receivers (with fake_claude.py) + real admin MCP over stdio.

Run from the A2ALocal folder:  python -m unittest tests.test_e2e -v
"""
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "hub"), os.path.join(ROOT, "receiver")]
import hub as hubmod  # noqa: E402
import receiver as rcvmod  # noqa: E402

FAKE = os.path.join(ROOT, "tests", "fake_claude.py")
ADMIN_TOKEN = "admin-token-test"


class E2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="a2a_test_")
        os.environ["FAKE_CLAUDE_DIR"] = os.path.join(cls.tmp, "sessions")
        rcvmod.LOG_DIR = os.path.join(cls.tmp, "receiver_logs")  # keep test logs out of the project
        cls.cfg_path = os.path.join(cls.tmp, "hub.json")
        cfg = {"port": 0, "bind": "127.0.0.1", "data_dir": "data", "report_hour": 99,
               "task_timeout_s": 60,
               "admins": {"ahmed": ADMIN_TOKEN, "omar": "admin2-token"},
               "pcs": {"pc1": {"token": "t1", "groups": ["revit"]},
                       "pc2": {"token": "t2", "groups": ["revit", "cad"]},
                       "pc3": {"token": "t3", "groups": ["cad"]}}}  # pc3 never runs: offline
        json.dump(cfg, open(cls.cfg_path, "w"))
        hubmod.CONFIG_PATH = cls.cfg_path
        cls.srv, cls.hub, cls.stop = hubmod.serve(hubmod.load_config(cls.cfg_path))
        cls.port = cls.srv.server_address[1]
        cls.url = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

        cls.receivers = []
        for name, tok in (("pc1", "t1"), ("pc2", "t2")):
            wd = os.path.join(cls.tmp, "wd_" + name)
            os.makedirs(wd)
            rcfg_path = os.path.join(cls.tmp, f"{name}.json")
            json.dump({"name": name, "hub_url": cls.url, "token": tok, "workdir": wd,
                       "claude_cmd": [sys.executable, FAKE], "heartbeat_s": 1, "poll_wait_s": 2},
                      open(rcfg_path, "w"))
            r = rcvmod.Receiver(rcvmod.load_config(rcfg_path))
            threading.Thread(target=r.loop, daemon=True).start()
            cls.receivers.append(r)
        # wait until both PCs are online
        end = time.time() + 10
        while time.time() < end:
            if sum(p["online"] for p in cls.api("GET", "/api/pcs")["pcs"]) == 2:
                break
            time.sleep(0.2)

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

    # ---- helpers
    @classmethod
    def api(cls, method, path, body=None, token=ADMIN_TOKEN, expect=200):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(cls.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-A2A-Token": token})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                code, out = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, out = e.code, json.loads(e.read())
        if expect is not None:
            assert code == expect, f"{method} {path}: HTTP {code} {out}"
        return out

    def wait(self, task_id, context_id, statuses=("done", "failed", "canceled", "needs_input"), t=20):
        end = time.time() + t
        while time.time() < end:
            for turn in self.api("GET", f"/api/threads/{context_id}")["turns"]:
                if turn["task_id"] == task_id and turn["status"] in statuses:
                    return turn
            time.sleep(0.2)
        self.fail(f"task {task_id} did not reach {statuses}")

    def send(self, **body):
        return self.api("POST", "/api/send", body)

    # ---- tests
    def test_01_auth(self):
        self.api("GET", "/api/pcs", token="nope", expect=401)
        self.api("GET", "/api/pcs", token="t1", expect=401)  # a PC token is not an admin token
        self.api("POST", "/api/worker/next", {"wait": 0}, token=ADMIN_TOKEN, expect=401)
        self.assertEqual(self.api("GET", "/api/whoami")["admin"], "ahmed")
        self.assertTrue(self.api("GET", "/health", token="")["ok"])

    def test_02_single_send_and_system_prompt(self):
        r = self.send(to="pc1", message="hello there")
        t = r["tasks"][0]
        turn = self.wait(t["task_id"], t["context_id"])
        self.assertEqual(turn["status"], "done")
        self.assertIn("ECHO: hello there | turn=1 | sp=yes | admin_in_sp=yes", turn["output"])
        self.assertIsNone(r["broadcast_id"])

    def test_03_conversation_keeps_session(self):
        t = self.send(to="pc2", message="first")["tasks"][0]
        self.wait(t["task_id"], t["context_id"])
        t2 = self.send(context_id=t["context_id"], message="second")["tasks"][0]
        self.assertEqual(t2["context_id"], t["context_id"])
        turn = self.wait(t2["task_id"], t2["context_id"])
        self.assertIn("ECHO: second | turn=2", turn["output"])  # same Claude session, turn 2
        th = self.api("GET", f"/api/threads/{t['context_id']}")
        self.assertEqual(len(th["turns"]), 2)
        self.assertTrue(th["has_session"])

    def test_04_group_broadcast(self):
        r = self.send(to="group:revit", message="what version?")
        self.assertEqual(sorted(x["pc"] for x in r["tasks"]), ["pc1", "pc2"])
        self.assertTrue(r["broadcast_id"])
        for t in r["tasks"]:
            self.wait(t["task_id"], t["context_id"])
        b = self.api("GET", f"/api/broadcasts/{r['broadcast_id']}")
        self.assertEqual([x["status"] for x in b["results"]], ["done", "done"])

    def test_05_needs_input_then_answer(self):
        t = self.send(to="pc1", message="ASK about cleanup")["tasks"][0]
        turn = self.wait(t["task_id"], t["context_id"])
        self.assertEqual(turn["status"], "needs_input")
        self.assertTrue(turn["output"].startswith("QUESTION:"))
        rep = self.api("GET", "/api/report?since_hours=1")["markdown"]
        self.assertIn(f"task {t['task_id']}", rep.split("## Waiting for your answer")[1].split("##")[0])
        t2 = self.send(context_id=t["context_id"], message="drive D")["tasks"][0]
        self.assertIn("turn=2", self.wait(t2["task_id"], t2["context_id"])["output"])
        rep = self.api("GET", "/api/report?since_hours=1")["markdown"]
        waiting = rep.split("## Waiting for your answer")[1].split("##")[0] if "## Waiting" in rep else ""
        self.assertNotIn(f"task {t['task_id']}", waiting)  # answered: no longer waiting

    def test_06_failures(self):
        t = self.send(to="pc1", message="FAIL now")["tasks"][0]
        turn = self.wait(t["task_id"], t["context_id"])
        self.assertEqual(turn["status"], "failed")
        self.assertIn("API Error", turn["error"])
        t = self.send(to="pc1", message="EXIT now")["tasks"][0]
        turn = self.wait(t["task_id"], t["context_id"])
        self.assertEqual(turn["status"], "failed")
        self.assertIn("exit 3", turn["error"])

    def test_07_cancel_running(self):
        t = self.send(to="pc2", message="SLEEP 30")["tasks"][0]
        self.wait(t["task_id"], t["context_id"], statuses=("running",))
        t0 = time.time()
        self.assertTrue(self.api("POST", f"/api/tasks/{t['task_id']}/cancel", {})["ok"])
        turn = self.wait(t["task_id"], t["context_id"], statuses=("canceled",), t=15)
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(turn["status"], "canceled")
        # the PC is free again
        t2 = self.send(to="pc2", message="after cancel")["tasks"][0]
        self.assertEqual(self.wait(t2["task_id"], t2["context_id"])["status"], "done")

    def test_08_offline_pc_queues_and_cancel_queued(self):
        t = self.send(to="pc3", message="for the offline PC")["tasks"][0]
        pcs = {p["name"]: p for p in self.api("GET", "/api/pcs")["pcs"]}
        self.assertFalse(pcs["pc3"]["online"])
        self.assertEqual(pcs["pc3"]["queued"], 1)
        rep = self.api("GET", "/api/report?since_hours=1")["markdown"]
        self.assertIn("Offline now: pc3", rep)
        self.assertIn("Still queued", rep)
        self.assertEqual(self.api("POST", f"/api/tasks/{t['task_id']}/cancel", {})["was"], "queued")
        self.assertEqual(self.wait(t["task_id"], t["context_id"], ("canceled",))["status"], "canceled")
        # a finished task cannot be canceled
        self.assertFalse(self.api("POST", f"/api/tasks/{t['task_id']}/cancel", {})["ok"])

    def test_09_offline_pc_runs_when_it_comes_online(self):
        t = self.send(to="pc3", message="queued overnight")["tasks"][0]
        time.sleep(1)
        wd = os.path.join(self.tmp, "wd_pc3")
        os.makedirs(wd, exist_ok=True)
        r = rcvmod.Receiver({"name": "pc3", "hub_url": self.url, "token": "t3", "workdir": wd,
                             "claude_cmd": [sys.executable, FAKE], "heartbeat_s": 1, "poll_wait_s": 2,
                             "extra_system_prompt": ""})
        th = threading.Thread(target=r.loop, daemon=True)
        th.start()
        try:
            self.assertEqual(self.wait(t["task_id"], t["context_id"])["status"], "done")
        finally:
            r.stop.set()

    def test_10_resume_failure_falls_back_to_new_session(self):
        t = self.send(to="pc1", message="start")["tasks"][0]
        self.wait(t["task_id"], t["context_id"])
        # lose the session on the PC (e.g. Claude data cleared)
        shutil.rmtree(os.environ["FAKE_CLAUDE_DIR"])
        t2 = self.send(context_id=t["context_id"], message="continue")["tasks"][0]
        turn = self.wait(t2["task_id"], t2["context_id"])
        self.assertEqual(turn["status"], "done")
        self.assertIn("could not be loaded", turn["output"])
        self.assertIn("turn=1", turn["output"])

    def test_11_validation(self):
        self.api("POST", "/api/send", {"to": "nobody", "message": "x"}, expect=400)
        self.api("POST", "/api/send", {"to": "group:nogroup", "message": "x"}, expect=400)
        self.api("POST", "/api/send", {"to": "pc1", "message": "  "}, expect=400)
        self.api("POST", "/api/send", {"context_id": "bad", "message": "x"}, expect=400)
        self.api("POST", "/api/send", {"message": "x"}, expect=400)
        self.api("GET", "/api/threads/nope", expect=400)

    def test_12_report_audit_dashboard(self):
        rep = self.api("GET", "/api/report?since_hours=1")
        self.assertIn("# Team A2A report", rep["markdown"])
        self.assertIn("## Done", rep["markdown"])
        events = {a["event"] for a in self.api("GET", "/api/audit?limit=500")["audit"]}
        self.assertTrue({"send", "start", "result"} <= events)
        with open(os.path.join(self.tmp, "data", "audit.jsonl"), encoding="utf-8") as f:
            self.assertGreater(len(f.readlines()), 5)
        with urllib.request.urlopen(self.url + "/") as r:
            self.assertIn(b"Team A2A Hub", r.read())
        path = self.hub.write_daily_report()
        self.assertTrue(os.path.exists(path))

    def test_13_add_pc_without_restart(self):
        old = sys.stdout
        import io
        sys.stdout = buf = io.StringIO()
        try:
            hubmod.main(["add-pc", "pc4", "--groups", "cad", "--hub-url", self.url])
        finally:
            sys.stdout = old
        tok = json.loads(buf.getvalue())["token"]
        time.sleep(0.05)
        out = self.api("POST", "/api/worker/next", {"wait": 0}, token=tok)
        self.assertIsNone(out["task"])
        self.assertIn("pc4", [p["name"] for p in self.api("GET", "/api/pcs")["pcs"]])

    def test_15_keepalive_after_body_requests(self):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Content-Type": "application/json", "X-A2A-Token": ADMIN_TOKEN}
        t = self.send(to="pc3", message="to cancel")["tasks"][0]
        c.request("POST", f"/api/tasks/{t['task_id']}/cancel", body="{}", headers=h)
        self.assertEqual(c.getresponse().read() and 200, 200)
        c.request("POST", "/api/nothing-here", body='{"a": 1}', headers=h)  # 404 path with a body
        r = c.getresponse()
        r.read()
        self.assertEqual(r.status, 404)
        c.request("GET", "/api/whoami", headers=h)  # same connection must still work
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(r.read())["admin"], "ahmed")
        c.close()

    def test_16_error_on_resumed_turn_is_not_run_again(self):
        t = self.send(to="pc1", message="one")["tasks"][0]
        self.wait(t["task_id"], t["context_id"])
        t2 = self.send(context_id=t["context_id"], message="EXIT please")["tasks"][0]
        turn = self.wait(t2["task_id"], t2["context_id"])
        self.assertEqual(turn["status"], "failed")
        self.assertIn("exit 3", turn["error"])

    def test_17_big_unicode_output_is_capped(self):
        t = self.send(to="pc2", message="BIG 400000")["tasks"][0]
        turn = self.wait(t["task_id"], t["context_id"], t=30)
        self.assertEqual(turn["status"], "done")
        self.assertTrue(turn["output"].startswith("م" * 100))
        self.assertIn("[truncated: 400000 characters in total]", turn["output"])
        t = self.send(to="pc2", message="مرحبا hello")["tasks"][0]
        self.assertIn("مرحبا", self.wait(t["task_id"], t["context_id"])["output"])

    def test_18_bad_hub_json_keeps_old_config(self):
        good = open(self.cfg_path, encoding="utf-8").read()
        try:
            with open(self.cfg_path, "w") as f:
                f.write("{ not json")
            os.utime(self.cfg_path, (time.time() + 5, time.time() + 5))
            self.assertEqual(self.api("GET", "/api/whoami")["admin"], "ahmed")
            t = self.send(to="pc1", message="still works")["tasks"][0]
            self.assertEqual(self.wait(t["task_id"], t["context_id"])["status"], "done")
        finally:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                f.write(good)
            os.utime(self.cfg_path, (time.time() + 10, time.time() + 10))
        self.assertEqual(self.api("GET", "/api/whoami")["admin"], "ahmed")

    def test_19_enroll_and_roles(self):
        jc = self.api("GET", "/api/joincode")["join_code"]
        self.api("POST", "/api/enroll", {"join_code": "wrong", "name": "x"}, token="", expect=403)
        a = self.api("POST", "/api/enroll", {"join_code": jc, "name": "newpc", "host": "H1", "user": "u1"}, token="")
        b = self.api("POST", "/api/enroll", {"join_code": jc, "name": "newpc"}, token="")
        self.assertEqual((a["name"], a["role"]), ("newpc", "pending"))
        self.assertEqual(b["name"], "newpc-2")
        # pending: no tasks, not in 'all', direct send refused, promotion has no effect yet
        self.assertEqual(self.api("POST", "/api/worker/next", {"wait": 0}, token=a["token"])["role"], "pending")
        self.api("POST", "/api/send", {"to": "newpc", "message": "x"}, expect=400)
        self.assertNotIn("newpc", [t["pc"] for t in self.send(to="all", message="ping all")["tasks"]])
        self.api("POST", "/api/pcs/newpc/role", {"role": "admin"})
        self.api("GET", "/api/pcs", token=a["token"], expect=401)
        self.api("POST", "/api/pcs/newpc/role", {"role": "user"})
        self.assertFalse({p["name"]: p for p in self.api("GET", "/api/pcs")["pcs"]}["newpc"]["approved"])
        self.api("POST", "/api/pcs/newpc/approve", {})
        self.api("POST", "/api/pcs/newpc/approve", {}, token=a["token"], expect=401)
        # approved regular PC token: worker yes, admin no; the poll reports its role
        r = self.api("POST", "/api/worker/next", {"wait": 0}, token=a["token"])
        self.assertEqual(r["role"], "user")
        self.api("GET", "/api/pcs", token=a["token"], expect=401)
        # owner promotes it: it can send, but cannot manage members
        self.api("POST", "/api/pcs/newpc/role", {"role": "admin"})
        self.assertEqual(self.api("POST", "/api/worker/next", {"wait": 0}, token=a["token"])["role"], "admin")
        w = self.api("GET", "/api/whoami", token=a["token"])
        self.assertEqual((w["admin"], w["owner"]), ("newpc", False))
        t = self.api("POST", "/api/send", {"to": "pc1", "message": "from promoted admin"}, token=a["token"])
        self.assertEqual(self.wait(t["tasks"][0]["task_id"], t["tasks"][0]["context_id"])["admin"], "newpc")
        self.api("POST", "/api/pcs/pc1/role", {"role": "admin"}, token=a["token"], expect=403)
        self.api("GET", "/api/joincode", token=a["token"], expect=403)
        # demote: admin access ends at once
        self.api("POST", "/api/pcs/newpc/role", {"role": "user"})
        self.api("GET", "/api/pcs", token=a["token"], expect=401)
        self.api("POST", "/api/pcs/newpc/role", {"role": "boss"}, expect=400)
        # groups, remove, new join code
        self.assertEqual(self.api("POST", "/api/pcs/newpc-2/groups", {"groups": "cad, revit,"})["groups"],
                         ["cad", "revit"])
        self.api("POST", "/api/pcs/newpc-2/remove", {})
        self.api("POST", "/api/worker/next", {"wait": 0}, token=b["token"], expect=401)
        new = self.api("POST", "/api/joincode", {})["join_code"]
        self.assertNotEqual(new, jc)
        self.api("POST", "/api/enroll", {"join_code": jc, "name": "late"}, token="", expect=403)
        # roles survive a reload of hub.json from disk
        saved = json.load(open(self.cfg_path, encoding="utf-8"))
        self.assertEqual(saved["pcs"]["newpc"]["role"], "user")
        self.assertNotIn("newpc-2", saved["pcs"])
        self.assertEqual(saved["join_code"], new)

    def test_20_mcp_gives_no_tools_inside_a_task(self):
        env = dict(os.environ, A2A_HUB_URL=self.url, A2A_ADMIN_TOKEN=ADMIN_TOKEN, A2A_IN_TASK="1")
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "admin_mcp", "admin_mcp.py")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
        out, _ = p.communicate(b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}\n'
                               b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"send","arguments":{}}}\n'
                               b'not json\n', timeout=20)
        lines = [json.loads(x) for x in out.decode().splitlines()]
        self.assertEqual(lines[0]["result"]["tools"], [])
        self.assertIn("error", lines[1])
        self.assertEqual(lines[2]["error"]["code"], -32700)

    def test_14_admin_mcp_stdio(self):
        env = dict(os.environ, A2A_HUB_URL=self.url, A2A_ADMIN_TOKEN=ADMIN_TOKEN)
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "admin_mcp", "admin_mcp.py")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)

        def rpc(i, method, params=None):
            p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                       "params": params or {}}) + "\n").encode())
            p.stdin.flush()
            return json.loads(p.stdout.readline())

        try:
            init = rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "1"}})
            self.assertEqual(init["result"]["serverInfo"]["name"], "team-a2a")
            p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
            names = {t["name"] for t in rpc(2, "tools/list")["result"]["tools"]}
            self.assertEqual(names, {"list_pcs", "send", "get_thread", "get_broadcast", "list_tasks",
                                     "morning_report", "cancel"})
            txt = rpc(3, "tools/call", {"name": "list_pcs", "arguments": {}})["result"]["content"][0]["text"]
            self.assertIn("pc1: ONLINE", txt)
            res = rpc(4, "tools/call", {"name": "send", "arguments": {
                "to": ["pc1", "pc2"], "message": "via mcp", "wait_seconds": 20}})["result"]
            txt = res["content"][0]["text"]
            self.assertEqual(txt.count("status=done"), 2, txt)
            self.assertIn("ECHO: via mcp", txt)
            cid = txt.split("thread=")[1].split()[0]
            txt = rpc(5, "tools/call", {"name": "get_thread", "arguments": {"context_id": cid}})
            self.assertIn("via mcp", txt["result"]["content"][0]["text"])
            txt = rpc(6, "tools/call", {"name": "morning_report", "arguments": {}})
            self.assertIn("Team A2A report", txt["result"]["content"][0]["text"])
            bad = rpc(7, "tools/call", {"name": "send", "arguments": {"to": "ghost", "message": "x"}})
            self.assertTrue(bad["result"]["isError"])
            self.assertIn("unknown PC", bad["result"]["content"][0]["text"])
        finally:
            p.stdin.close()
            p.wait(5)
            p.stdout.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
