"""Feature 3: signed receiver updates (hub release + receiver check_update).

Run from the project folder:  python -m unittest discover -s tests -p "test_update.py" -v
"""
import hashlib
import io
import json
import os
import py_compile
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(ROOT, "hub"), os.path.join(ROOT, "receiver")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import a2a_sign  # noqa: E402
import hub as hubmod  # noqa: E402
import receiver as rcvmod  # noqa: E402
from a2a_util import canonical  # noqa: E402

ADMIN = "admin-upd-token"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
RECEIVER_FILES = ("receiver.py", "a2a_sign.py", "a2a_util.py")


def source_with_version(dst, version):
    """A copy of receiver/*.py whose VERSION is `version`."""
    os.makedirs(dst, exist_ok=True)
    for n in RECEIVER_FILES:
        shutil.copy2(os.path.join(ROOT, "receiver", n), os.path.join(dst, n))
    p = os.path.join(dst, "receiver.py")
    with open(p, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r'^VERSION = "[^"]*"', f'VERSION = "{version}"', text, count=1, flags=re.M)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return dst


def build_release(seed, version, files):
    """(release dict, package bytes) signed with `seed`; files = {name: bytes}."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, data in files.items():
            z.writestr(n, data)
    pkg = buf.getvalue()
    man = {"version": version, "sha256": hashlib.sha256(pkg).hexdigest(), "size": len(pkg),
           "files": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}, "created": "now"}
    return {"manifest": man, "signature": a2a_sign.sign(seed, canonical(man)).hex()}, pkg


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Update(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="a2a_upd_")
        cls.old_log_dir = rcvmod.LOG_DIR
        rcvmod.LOG_DIR = os.path.join(cls.tmp, "receiver_logs")
        cls.cfg_path = os.path.join(cls.tmp, "hub.json")
        cfg = {"port": 0, "bind": "127.0.0.1", "data_dir": "data", "report_hour": 99,
               "task_timeout_s": 120, "admins": {"ahmed": ADMIN},
               "pcs": {"pa": {"token": "ta"}, "pp": {"token": "tpp", "approved": False}}}
        with open(cls.cfg_path, "w") as f:
            json.dump(cfg, f)
        cls.old_cfg = hubmod.CONFIG_PATH
        hubmod.CONFIG_PATH = cls.cfg_path
        cls.cfg = hubmod.load_config(cls.cfg_path)
        cls.srv, cls.hub, cls.stop = hubmod.serve(cls.cfg)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.data_dir = hubmod.data_dir_of(cls.cfg)
        cls.seed, cls.pub = hubmod.signing_key(cls.data_dir)
        # U1: one good release, 1.9.0
        cls.rel = hubmod.make_release(cls.cfg, "1.9.0", source_with_version(os.path.join(cls.tmp, "src190"), "1.9.0"))

    @classmethod
    def tearDownClass(cls):
        cls.stop.set()
        cls.srv.shutdown()
        cls.srv.server_close()
        time.sleep(0.3)
        cls.hub.store.db.close()
        hubmod.CONFIG_PATH = cls.old_cfg
        rcvmod.LOG_DIR = cls.old_log_dir
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def raw(self, method, path, body=None, token=ADMIN):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-A2A-Token": token})
        try:
            with OPENER.open(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def receiver(self, install_dir, pubkey=None, **extra):
        path = os.path.join(self.tmp, f"rcv_{os.path.basename(install_dir)}.json")
        with open(path, "w") as f:
            json.dump({"name": "pa", "hub_url": self.url, "token": "ta", "workdir": self.tmp,
                       "update_pubkey": self.pub.hex() if pubkey is None else pubkey, **extra}, f)
        r = rcvmod.Receiver(rcvmod.load_config(path))
        r.install_dir = install_dir
        r.restarts = 0

        def fake_restart():
            r.restarts += 1
        r.restart = fake_restart
        return r

    def install_dir(self, name):
        d = source_with_version(os.path.join(self.tmp, name), rcvmod.VERSION)
        return d

    def snapshot(self, d):
        out = {}
        for n in RECEIVER_FILES:
            with open(os.path.join(d, n), "rb") as f:
                out[n] = f.read()
        return out

    # U1
    def test_u1_release_written_and_signed(self):
        d = os.path.join(self.data_dir, "releases", "1.9.0")
        self.assertTrue(os.path.isfile(os.path.join(d, "package.zip")))
        with open(os.path.join(d, "release.json"), encoding="utf-8") as f:
            rel = json.load(f)
        self.assertTrue(a2a_sign.verify(self.pub, canonical(rel["manifest"]), bytes.fromhex(rel["signature"])))
        with zipfile.ZipFile(os.path.join(d, "package.zip")) as z:
            names = z.namelist()
        self.assertIn("receiver.py", names)
        self.assertFalse(any(n.endswith(".json") for n in names), names)
        with open(os.path.join(d, "package.zip"), "rb") as f:
            self.assertEqual(hashlib.sha256(f.read()).hexdigest(), rel["manifest"]["sha256"])

    # U1b: a source folder with receiver.json and an exe: the json never goes in, the exe does
    def test_u1b_release_skips_json_takes_exe(self):
        src = source_with_version(os.path.join(self.tmp, "src191"), "1.9.1")
        with open(os.path.join(src, "receiver.json"), "w") as f:
            f.write('{"token": "secret"}')
        with open(os.path.join(src, "TeamA2A-Agent.exe"), "wb") as f:
            f.write(b"MZ fake exe")
        rel = hubmod.make_release(self.cfg, "1.9.1", src)
        self.assertNotIn("receiver.json", rel["manifest"]["files"])
        self.assertIn("TeamA2A-Agent.exe", rel["manifest"]["files"])

    # U2
    def test_u2_bad_versions_refused(self):
        src = source_with_version(os.path.join(self.tmp, "src_mis"), "2.0")
        with self.assertRaises(SystemExit):
            hubmod.make_release(self.cfg, "2.1", src)  # VERSION in receiver.py differs
        with self.assertRaises(ValueError):
            hubmod.make_release(self.cfg, "abc", src)
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "releases", "2.1")))
        self.assertFalse(os.path.exists(os.path.join(self.data_dir, "releases", "abc")))

    # U3
    def test_u3_seed_never_served(self):
        self.assertTrue(os.path.isfile(os.path.join(self.data_dir, "update_signing_key.json")))
        seed_hex = self.seed.hex()
        bodies = []
        for method, path, body, tok in (
                ("GET", "/api/update/manifest", None, "ta"), ("GET", "/api/update/manifest", None, ADMIN),
                ("GET", "/api/pcs", None, ADMIN), ("GET", "/api/audit?limit=500", None, ADMIN),
                ("POST", "/api/worker/next", {"wait": 0, "slots": 0, "running": [], "version": "1.2"}, "ta"),
                ("POST", "/api/enroll", {"join_code": self.hub.cfg["join_code"], "name": "u3pc"}, "")):
            bodies.append(self.raw(method, path, body, tok)[1])
        bodies.append(self.raw("GET", "/api/update/package?version=1.9.0", None, "ta")[1])
        for b in bodies:
            self.assertNotIn(seed_hex.encode(), b)
            self.assertNotIn(self.seed, b)

    # U4
    def test_u4_manifest_and_package_auth(self):
        code, body = self.raw("GET", "/api/update/manifest", token="ta")
        self.assertEqual(code, 200)
        man = json.loads(body)["release"]["manifest"]
        self.assertEqual(self.raw("GET", "/api/update/manifest", token="")[0], 401)
        self.assertEqual(self.raw("GET", "/api/update/manifest", token="tpp")[0], 403)
        code, pkg = self.raw("GET", f"/api/update/package?version={man['version']}", token="ta")
        self.assertEqual(code, 200)
        self.assertEqual(hashlib.sha256(pkg).hexdigest(), man["sha256"])
        self.assertEqual(self.raw("GET", "/api/update/package?version=1.9.0", token="")[0], 401)
        self.assertEqual(self.raw("GET", "/api/update/package?version=1.9.0", token="tpp")[0], 403)
        self.assertEqual(self.raw("GET", "/api/update/package?version=..%2F..%2Fx", token="ta")[0], 400)
        self.assertEqual(self.raw("GET", "/api/update/package?version=7.7", token="ta")[0], 404)

    # U5
    def test_u5_poll_offers_newer_only(self):
        newest = self.hub.newest_release()["manifest"]["version"]
        r = json.loads(self.raw("POST", "/api/worker/next",
                                {"wait": 0, "slots": 0, "running": [], "version": "1.2"}, "ta")[1])
        self.assertEqual(r["update"], {"version": newest})
        r = json.loads(self.raw("POST", "/api/worker/next",
                                {"wait": 0, "slots": 0, "running": [], "version": newest}, "ta")[1])
        self.assertIsNone(r["update"])

    # U6
    def test_u6_good_install(self):
        d = self.install_dir("inst_good")
        before = self.snapshot(d)
        rel, pkg = build_release(self.seed, "1.9.0", {
            n: source_bytes for n, source_bytes in
            self.snapshot(source_with_version(os.path.join(self.tmp, "src_u6"), "1.9.0")).items()})
        r = self.receiver(d)
        self.serve_fake(r, rel, pkg)
        self.assertEqual(r.check_update(), "installed")
        with open(os.path.join(d, "receiver.py"), encoding="utf-8") as f:
            self.assertIn('VERSION = "1.9.0"', f.read())
        backup = os.path.join(d, "update_backup", rcvmod.VERSION, "receiver.py")
        with open(backup, "rb") as f:
            self.assertEqual(f.read(), before["receiver.py"])
        self.assertEqual(r.restarts, 1)
        self.assertFalse(os.path.exists(os.path.join(d, "update_staging")))
        # through the real hub too: manifest + package from HTTP
        d2 = self.install_dir("inst_hub")
        r2 = self.receiver(d2)
        self.assertEqual(r2.check_update(), "installed")
        self.assertEqual(r2.restarts, 1)
        r2.clean_old_files()
        self.assertFalse([n for n in os.listdir(d2) if n.endswith(".old")])

    def serve_fake(self, r, rel, pkg):
        def fake_get(path, timeout=60):
            if path.startswith("/api/update/manifest"):
                return FakeResp(json.dumps({"ok": True, "release": rel}).encode())
            if path.startswith("/api/update/package"):
                return FakeResp(pkg)
            raise AssertionError(path)
        r.get = fake_get

    # U7
    def test_u7_tampered_updates_refused(self):
        good = self.snapshot(source_with_version(os.path.join(self.tmp, "src_u7"), "1.9.0"))
        other_seed = a2a_sign.generate_seed()
        cases = []
        rel, pkg = build_release(self.seed, "1.9.0", good)
        bad_sig = dict(rel, signature=("00" if rel["signature"][:2] != "00" else "11") + rel["signature"][2:])
        cases.append(("altered signature", bad_sig, pkg, None, "bad_signature"))
        changed_man = {"manifest": dict(rel["manifest"], version="1.9.9"), "signature": rel["signature"]}
        cases.append(("manifest changed after signing", changed_man, pkg, None, "bad_signature"))
        rel_o, pkg_o = build_release(other_seed, "1.9.0", good)
        cases.append(("other key", rel_o, pkg_o, None, "bad_signature"))
        flipped = bytearray(pkg)
        flipped[len(flipped) // 2] ^= 1
        cases.append(("flipped package byte", rel, bytes(flipped), None, "sha256_mismatch"))
        rel_d, pkg_d = build_release(self.seed, "1.0", good)
        cases.append(("downgrade", rel_d, pkg_d, None, "not_newer"))
        cases.append(("no key", rel, pkg, "", "no_key"))
        rel_w, pkg_w = build_release(self.seed, "1.9.0", {"other.py": b"print(1)"})
        cases.append(("no receiver.py", rel_w, pkg_w, None, "wrong_kind"))
        rel_t, pkg_t = build_release(self.seed, "1.9.0", {"receiver.py": good["receiver.py"],
                                                          "..\\evil.py": b"x"})
        cases.append(("path in file name", rel_t, pkg_t, None, "bad_signature"))
        for i, (label, rel_x, pkg_x, key, want) in enumerate(cases):
            d = self.install_dir(f"inst_t{i}")
            before = self.snapshot(d)
            r = self.receiver(d, pubkey=key)
            self.serve_fake(r, rel_x, pkg_x)
            self.assertEqual(r.check_update(), want, label)
            self.assertEqual(self.snapshot(d), before, label)
            self.assertEqual(r.restarts, 0, label)
            self.assertFalse(os.path.exists(os.path.join(d, "update_staging")), label)
            self.assertFalse(os.path.exists(os.path.join(os.path.dirname(d), "evil.py")), label)

    # U7b: auto_update off
    def test_u7b_auto_update_off(self):
        d = self.install_dir("inst_off")
        r = self.receiver(d, auto_update=False)
        self.assertEqual(r.check_update(), "disabled")

    # U8
    def test_u8_running_task_finishes_first(self):
        d = self.install_dir("inst_busy")
        good = self.snapshot(source_with_version(os.path.join(self.tmp, "src_u8"), "1.9.0"))
        rel, pkg = build_release(self.seed, "1.9.0", good)
        r = self.receiver(d)
        self.serve_fake(r, rel, pkg)
        with r.run_lock:
            r.running["busy-task"] = None
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("res", r.check_update()), daemon=True)
        th.start()
        time.sleep(1.5)
        self.assertTrue(r.updating.is_set(), "receiver must stop taking tasks while it waits")
        self.assertNotIn("res", out)
        with open(os.path.join(d, "receiver.py"), encoding="utf-8") as f:
            self.assertNotIn('VERSION = "1.9.0"', f.read())
        with r.run_lock:
            r.running.pop("busy-task")
        r.slot_free.set()
        th.join(15)
        self.assertEqual(out.get("res"), "installed")
        self.assertFalse(r.updating.is_set())

    # U9
    def test_u9_pubkey_at_enroll_and_add_pc(self):
        code, body = self.raw("POST", "/api/enroll", {"join_code": self.hub.cfg["join_code"], "name": "u9"}, "")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["update_pubkey"], self.pub.hex())
        # members that joined before updates existed fetch the key once (setup.py re-run)
        for tok in ("ta", "tpp", ADMIN):
            code, body = self.raw("GET", "/api/update/key", token=tok)
            self.assertEqual((code, json.loads(body)["update_pubkey"]), (200, self.pub.hex()))
        self.assertEqual(self.raw("GET", "/api/update/key", token="")[0], 401)
        env = dict(os.environ, A2A_HUB_CONFIG=self.cfg_path)
        p = subprocess.run([sys.executable, os.path.join(ROOT, "hub", "hub.py"), "add-pc", "u9cli"],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["update_pubkey"], self.pub.hex())
        p = subprocess.run([sys.executable, os.path.join(ROOT, "hub", "hub.py"), "update-key"],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.stdout.strip(), self.pub.hex())

    # U10
    def test_u10_setup_and_build_compile(self):
        for f in ("client/setup.py", "build/build.py"):
            py_compile.compile(os.path.join(ROOT, f), doraise=True,
                               cfile=os.path.join(self.tmp, os.path.basename(f) + "c"))
        with open(os.path.join(ROOT, "client", "setup.py"), encoding="utf-8") as f:
            self.assertIn('cfg["update_pubkey"] = update_pubkey', f.read())
        with open(os.path.join(ROOT, "build", "build.py"), encoding="utf-8") as f:
            self.assertIn('"--paths"', f.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
