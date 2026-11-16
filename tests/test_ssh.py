"""Feature 4: SSH to the cmd shell of approved PCs (key only, forced command, audit).

The real sshd login, firewall rule and DefaultShell need admin rights and OpenSSH Server: they are
human checks. Everything else runs here against a real temp hub.
Run from the project folder:  python -m unittest discover -s tests -p "test_ssh.py" -v
"""
import ast
import base64
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "hub"), os.path.join(ROOT, "receiver"), os.path.join(ROOT, "admin_mcp")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import a2a_ssh  # noqa: E402
import admin_mcp  # noqa: E402
import hub as hubmod  # noqa: E402

ADMIN = "admin-ssh-token"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LAUNCHER = r"C:\ProgramData\TeamA2A\ssh\teama2a-ssh.cmd"


def _s(b):
    return struct.pack(">I", len(b)) + b


def make_key(ktype="ssh-ed25519", comment="ahmed@pc"):
    if ktype == "ssh-ed25519":
        blob = _s(b"ssh-ed25519") + _s(os.urandom(32))
    elif ktype.startswith("ecdsa-sha2-"):
        blob = _s(ktype.encode()) + _s(ktype.split("-")[-1].encode()) + _s(b"\x04" + os.urandom(64))
    elif ktype == "ssh-rsa":
        blob = _s(b"ssh-rsa") + _s(b"\x01\x00\x01") + _s(b"\x00" + os.urandom(256))
    else:
        blob = _s(ktype.encode()) + _s(os.urandom(32))
    return f"{ktype} {base64.b64encode(blob).decode()}" + (f" {comment}" if comment else "")


KEY_AHMED = make_key()
KEY_OMAR = make_key("ecdsa-sha2-nistp256", "omar")
KEY_GHOST = make_key()


class SshUnit(unittest.TestCase):
    # T2
    def test_t2_sshd_config(self):
        c = a2a_ssh.render_sshd_config("alice", "192.168.1.0/24")
        lines = c.splitlines()
        for want in ("PubkeyAuthentication yes", "PasswordAuthentication no", "KbdInteractiveAuthentication no",
                     "ChallengeResponseAuthentication no", "PermitEmptyPasswords no",
                     "AuthenticationMethods publickey", "AllowUsers alice", "AllowTcpForwarding no",
                     "AllowAgentForwarding no", "X11Forwarding no", "PermitTunnel no", "GatewayPorts no",
                     "PermitTTY no", "PermitUserEnvironment no", "StrictModes yes", "MaxAuthTries 3",
                     "LogLevel VERBOSE",
                     "AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys"):
            self.assertIn(want, lines)
        self.assertNotIn("PasswordAuthentication yes", c)
        self.assertFalse(any(ln.startswith("Subsystem") for ln in lines))
        for bad_user in ("al ice", "-oX", "a\nb", "", "x" * 65):
            with self.assertRaises(ValueError):
                a2a_ssh.render_sshd_config(bad_user)
        with self.assertRaises(ValueError):
            a2a_ssh.render_sshd_config("alice", "not-a-net")

    # T3
    def test_t3_authorized_keys_line(self):
        line = a2a_ssh.authorized_keys_line(LAUNCHER, "ahmed", "192.168.1.7/24", KEY_AHMED)
        typ, b64 = KEY_AHMED.split()[:2]
        self.assertEqual(line, f'from="192.168.1.0/24",command="{LAUNCHER} --requester ahmed",'
                               f"no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty "
                               f"{typ} {b64} a2a:ahmed")
        for bad in (r"C:\Program Files\x.cmd", 'C:\\x".cmd', "relative.cmd", r"C:\x.cmd & calc"):
            with self.assertRaises(ValueError):
                a2a_ssh.authorized_keys_line(bad, "ahmed", "10.0.0.0/8", KEY_AHMED)
        text, skipped = a2a_ssh.render_authorized_keys(
            LAUNCHER, "10.0.0.0/8", [{"name": "ahmed", "key": KEY_AHMED}, {"name": "bad name", "key": KEY_AHMED},
                                     {"name": "x", "key": "ssh-dss AAAA"}])
        self.assertEqual(text.count("\n"), 1)
        self.assertEqual(skipped, ["bad name", "x"])

    # T4
    def test_t4_validate_pubkey(self):
        typ, b64 = KEY_AHMED.split()[:2]
        bad = [f'command="calc" {typ} {b64}', f"from=\"x\",{typ} {b64}", f"{typ} {b64}\n{typ} {b64}",
               f"{typ} {b64}\rX", f"{typ} {b64} c\x00", make_key("ssh-dss"), f"{typ} !!notbase64", f"ssh-rsa {b64}",
               f"{typ} {b64} comment,with,commas", f"{typ} {b64} \"q\"", "", "ssh-ed25519",
               f"{typ} {base64.b64encode(b'xx').decode()}"]
        for k in bad:
            with self.assertRaises(ValueError, msg=repr(k)):
                a2a_ssh.validate_pubkey(k)
        for k in (KEY_AHMED, KEY_OMAR, make_key("ssh-rsa"), make_key("ecdsa-sha2-nistp384", "")):
            self.assertEqual(a2a_ssh.validate_pubkey(k), " ".join(k.split()[:2]))
        for name in ("a b", "a;b", "", "x" * 65, "ä", "-oX", "a\n"):
            with self.assertRaises(ValueError):
                a2a_ssh.validate_name(name)

    # T14
    def test_t14_ssh_args(self):
        a = admin_mcp.ssh_args(r"C:\k\id", "alice", "192.168.1.9", "ipconfig /all", 10)
        for opt in ("BatchMode=yes", "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
                    "PreferredAuthentications=publickey", "StrictHostKeyChecking=accept-new",
                    "IdentitiesOnly=yes", "ConnectTimeout=10"):
            self.assertIn(opt, a)
            self.assertEqual(a[a.index(opt) - 1], "-o")
        self.assertEqual(a[0], "-T")
        self.assertEqual(a[a.index("-i") + 1], r"C:\k\id")
        self.assertEqual(a[a.index("-l") + 1], "alice")
        self.assertEqual(a[-3:], ["--", "192.168.1.9", "ipconfig /all"])
        for host in ("-oProxyCommand=x", "", "a b"):
            with self.assertRaises(ValueError):
                admin_mcp.ssh_args("k", "alice", host, "x")
        with self.assertRaises(ValueError):
            admin_mcp.ssh_args("k", "-oX", "h", "x")

    # T16
    def test_t16_setup_script(self):
        ps1 = os.path.join(ROOT, "receiver", "setup_ssh.ps1")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
                            f"'{ps1}', [ref]$null, [ref]$e); $e.Count"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.stdout.strip(), "0", r.stdout + r.stderr)
        with open(ps1, encoding="utf-8") as f:
            text = f.read()
        for part in ("OpenSSH.Server", "administrators_authorized_keys", "DefaultShell", "cmd.exe",
                     "TeamA2A-SSH", "-RemoteAddress $cidr", "OpenSSH-Server-In-TCP", "icacls",
                     "/inheritance:r", "S-1-5-18", "S-1-5-32-544", "& $sshd -t", "render-config",
                     "TeamA2A SSH key sync", "-User 'SYSTEM'", "sync", "IsInRole"):
            self.assertIn(part, text)
        self.assertNotIn(chr(8), text)

    # T18
    def test_t18_stdlib_only(self):
        for fn in ("receiver/a2a_ssh.py", "admin_mcp/admin_mcp.py", "hub/hub.py", "receiver/receiver.py"):
            with open(os.path.join(ROOT, fn), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                    [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                for m in mods:
                    top = m.split(".")[0]
                    if top.startswith("a2a_"):
                        continue
                    self.assertIn(top, sys.stdlib_module_names, f"{fn} imports {m}")


class SshHub(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="a2a_ssh_")
        cls.cfg_path = os.path.join(cls.tmp, "hub.json")
        cfg = {"port": 0, "bind": "127.0.0.1", "data_dir": "data", "report_hour": 99, "task_timeout_s": 60,
               "admins": {"ahmed": ADMIN, "omar": "t-omar"},
               "ssh_keys": {"ahmed": KEY_AHMED, "ghost": KEY_GHOST}, "ssh_allowed_cidr": "127.0.0.0/8",
               "pcs": {"pa": {"token": "ta"}, "pnever": {"token": "tnever"},
                       "pp": {"token": "tpp", "approved": False}}}
        with open(cls.cfg_path, "w") as f:
            json.dump(cfg, f)
        cls.old_cfg = hubmod.CONFIG_PATH
        hubmod.CONFIG_PATH = cls.cfg_path
        cls.srv, cls.hub, cls.stop = hubmod.serve(hubmod.load_config(cls.cfg_path))
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        # pa polls once: the hub learns its address and Windows user
        cls.api("POST", "/api/worker/next", {"wait": 0, "slots": 0, "running": [], "user": "alice",
                                             "version": "1.2"}, token="ta")
        for name, tok in (("pa", "ta"), ("pp", "tpp")):
            with open(os.path.join(cls.tmp, f"{name}.json"), "w") as f:
                json.dump({"name": name, "hub_url": cls.url, "token": tok}, f)

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.srv.shutdown()
        cls.srv.server_close()
        time.sleep(0.3)
        cls.hub.store.db.close()
        hubmod.CONFIG_PATH = cls.old_cfg
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
        assert expect is None or code == expect, f"{method} {path}: HTTP {code} {out}"
        return out

    def audit(self, event):
        return [a for a in self.api("GET", "/api/audit?limit=2000")["audit"] if a["event"] == event]

    def shell(self, command, requester="ahmed"):
        log_dir = os.path.join(self.tmp, "shell_logs")
        env = dict(os.environ, SSH_CLIENT="127.0.0.1 50022 22")
        env.pop("SSH_ORIGINAL_COMMAND", None)
        if command is not None:
            env["SSH_ORIGINAL_COMMAND"] = command
        r = subprocess.run([sys.executable, os.path.join(ROOT, "receiver", "receiver.py"), "ssh",
                            "--config", os.path.join(self.tmp, "pa.json"), "shell",
                            "--requester", requester, "--log-dir", log_dir],
                           capture_output=True, text=True, env=env, timeout=60)
        with open(os.path.join(log_dir, "ssh_commands.jsonl"), encoding="utf-8") as f:
            entries = [json.loads(ln) for ln in f]
        return r, entries

    # T5 + T6
    def test_t5_t6_shell_runs_and_logs(self):
        r, log = self.shell("echo hi")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "hi")
        start, end = [e for e in log if e["command"] == "echo hi"][-2:]
        self.assertEqual((start["event"], end["event"]), ("ssh_start", "ssh_end"))
        for k in ("id", "pc", "time", "requester", "command", "client_ip"):
            self.assertIn(k, start)
        self.assertEqual((end["pc"], end["requester"], end["client_ip"], end["exit_code"]),
                         ("pa", "ahmed", "127.0.0.1", 0))
        self.assertIsInstance(end["duration_s"], float)
        ev = [a for a in self.audit("ssh_command") if a["detail"].get("id") == end["id"]]
        self.assertEqual(sorted(a["detail"]["phase"] for a in ev), ["end", "start"])
        self.assertTrue(all(a["actor"] == "pa" and a["detail"]["pc"] == "pa" for a in ev))

    # T7
    def test_t7_exit_code_passes_through(self):
        r, log = self.shell("exit /b 3")
        self.assertEqual(r.returncode, 3)
        self.assertEqual([e for e in log if e["command"] == "exit /b 3"][-1]["exit_code"], 3)

    # T8
    def test_t8_no_command_refused(self):
        before = len(self.audit("ssh_command"))
        r, log = self.shell(None)
        self.assertEqual(r.returncode, 1)
        self.assertIn("interactive", r.stderr)
        self.assertEqual(log[-1]["event"], "ssh_refused")
        ev = self.audit("ssh_command")
        self.assertEqual(len(ev), before + 1)
        self.assertEqual(ev[0]["detail"]["phase"], "refused")

    # T9
    def test_t9_worker_ssh_keys(self):
        self.api("POST", "/api/worker/ssh_keys", {}, token="tpp", expect=403)
        r = self.api("POST", "/api/worker/ssh_keys", {}, token="ta")
        self.assertEqual(r["cidr"], "127.0.0.0/8")
        self.assertEqual([k["name"] for k in r["keys"]], ["ahmed"])  # ghost is not an admin
        self.assertEqual(r["keys"][0]["key"], " ".join(KEY_AHMED.split()[:2]))
        self.api("POST", "/api/worker/ssh_keys", {}, token="bad", expect=401)

    # T10
    def test_t10_sync_writes_and_fails_closed(self):
        keys = os.path.join(self.tmp, "keys_pa")
        cfg = a2a_ssh.load_cfg(os.path.join(self.tmp, "pa.json"))
        r = a2a_ssh.sync_authorized_keys(cfg, keys, LAUNCHER)
        self.assertEqual(r["result"], "written")
        with open(keys, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f'from="127.0.0.0/8",command="{LAUNCHER} --requester ahmed"'))
        keys_pp = os.path.join(self.tmp, "keys_pp")
        with open(keys_pp, "w") as f:
            f.write("old line that must go\n")
        r = a2a_ssh.sync_authorized_keys(a2a_ssh.load_cfg(os.path.join(self.tmp, "pp.json")), keys_pp, LAUNCHER)
        self.assertEqual(r["result"], "emptied")
        self.assertEqual(os.path.getsize(keys_pp), 0)
        down = dict(cfg, hub_url="http://127.0.0.1:9")  # nothing listens: keep the file
        r = a2a_ssh.sync_authorized_keys(down, keys, LAUNCHER)
        self.assertEqual(r["result"], "kept")
        with open(keys, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 1)

    # T11 (+ T9 for a second admin key)
    def test_t11_cli_ssh_add_key(self):
        env = dict(os.environ, A2A_HUB_CONFIG=self.cfg_path)
        hubpy = os.path.join(ROOT, "hub", "hub.py")

        def cli(*a):
            return subprocess.run([sys.executable, hubpy] + list(a), capture_output=True, text=True,
                                  env=env, timeout=60)
        with open(self.cfg_path, "rb") as f:
            before = f.read()
        self.assertNotEqual(cli("ssh-add-key", "omar", 'command="x" ' + KEY_OMAR).returncode, 0)
        self.assertNotEqual(cli("ssh-add-key", "nobody", KEY_OMAR).returncode, 0)
        self.assertNotEqual(cli("ssh-cidr", "nonsense").returncode, 0)
        with open(self.cfg_path, "rb") as f:
            self.assertEqual(f.read(), before)
        r = cli("ssh-add-key", "omar", KEY_OMAR)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.cfg_path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["ssh_keys"]["omar"], " ".join(KEY_OMAR.split()[:2]))
        time.sleep(0.05)
        names = [k["name"] for k in self.api("POST", "/api/worker/ssh_keys", {}, token="ta")["keys"]]
        self.assertEqual(names, ["ahmed", "omar"])
        self.assertEqual(cli("ssh-remove-key", "omar").returncode, 0)
        names = [k["name"] for k in self.api("POST", "/api/worker/ssh_keys", {}, token="ta")["keys"]]
        self.assertEqual(names, ["ahmed"])

    # T12
    def test_t12_ssh_target(self):
        r = self.api("POST", "/api/ssh/target", {"pc": "pa", "command": "ver"})
        self.assertEqual((r["ip"], r["user"]), ("127.0.0.1", "alice"))
        ev = [a for a in self.audit("ssh_request") if a["detail"].get("command") == "ver"]
        self.assertEqual(ev[0]["actor"], "ahmed")
        self.assertEqual(ev[0]["detail"]["pc"], "pa")

    # T13
    def test_t13_ssh_target_refusals(self):
        self.api("POST", "/api/ssh/target", {"pc": "pp"}, expect=403)
        self.api("POST", "/api/ssh/target", {"pc": "nope"}, expect=400)
        self.api("POST", "/api/ssh/target", {"pc": "pa"}, token="t-omar", expect=403)  # no key
        self.api("POST", "/api/ssh/target", {"pc": "pnever"}, expect=409)
        self.api("POST", "/api/ssh/target", {"pc": "pa"}, token="ta", expect=401)

    # T15
    def test_t15_mcp_ssh_run(self):
        fake = os.path.join(self.tmp, "fake_ssh.py")
        with open(fake, "w") as f:
            f.write("import json, sys\nprint(json.dumps(sys.argv[1:]))\nsys.exit(0)\n")
        fake_cmd = os.path.join(self.tmp, "fake_ssh.cmd")
        with open(fake_cmd, "w") as f:
            f.write(f'@"{sys.executable}" "{fake}" %*\r\n')
        key = os.path.join(self.tmp, "id_test")
        with open(key, "w") as f:
            f.write("not a real key")
        env = dict(os.environ, A2A_HUB_URL=self.url, A2A_ADMIN_TOKEN=ADMIN, A2A_SSH_EXE=fake_cmd,
                   A2A_SSH_KEY=key)
        env.pop("A2A_IN_TASK", None)

        def session(env):
            p = subprocess.Popen([sys.executable, os.path.join(ROOT, "admin_mcp", "admin_mcp.py")],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)

            def rpc(i, method, params=None):
                p.stdin.write((json.dumps({"jsonrpc": "2.0", "id": i, "method": method,
                                           "params": params or {}}) + "\n").encode())
                p.stdin.flush()
                return json.loads(p.stdout.readline())
            return p, rpc

        p, rpc = session(env)
        try:
            rpc(1, "initialize", {})
            names = {t["name"] for t in rpc(2, "tools/list")["result"]["tools"]}
            self.assertIn("ssh_run", names)

            def call(i, args):
                res = rpc(i, "tools/call", {"name": "ssh_run", "arguments": args})["result"]
                return res.get("isError", False), res["content"][0]["text"]
            err, txt = call(3, {"pc": "pa", "command": "hostname"})
            self.assertFalse(err, txt)
            self.assertIn("exit=0", txt)
            argv = json.loads(txt.splitlines()[1])
            self.assertEqual(argv[-3:], ["--", "127.0.0.1", "hostname"])
            self.assertEqual(argv[argv.index("-l") + 1], "alice")
            self.assertEqual(argv[argv.index("-i") + 1], key)
            for args, want in (({"pc": "nope", "command": "x"}, "unknown PC"),
                               ({"pc": "pp", "command": "x"}, "approval"),
                               ({"pc": "pnever", "command": "x"}, "address"),
                               ({"pc": "pa", "command": "  "}, "give 'pc' and 'command'")):
                err, txt = call(10, args)
                self.assertTrue(err, txt)
                self.assertIn(want, txt)
        finally:
            p.stdin.close()
            p.wait(10)
            p.stdout.close()
        p, rpc = session(dict(env, A2A_ADMIN_TOKEN="t-omar"))  # admin without a key on the hub
        try:
            res = rpc(1, "tools/call", {"name": "ssh_run", "arguments": {"pc": "pa", "command": "x"}})["result"]
            self.assertTrue(res["isError"])
            self.assertIn("no SSH key", res["content"][0]["text"])
        finally:
            p.stdin.close()
            p.wait(10)
            p.stdout.close()
        p, rpc = session(dict(env, A2A_IN_TASK="1"))  # inside a remote task: no tools at all
        try:
            self.assertEqual(rpc(1, "tools/list")["result"]["tools"], [])
            self.assertIn("error", rpc(2, "tools/call", {"name": "ssh_run", "arguments": {"pc": "pa"}}))
        finally:
            p.stdin.close()
            p.wait(10)
            p.stdout.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
