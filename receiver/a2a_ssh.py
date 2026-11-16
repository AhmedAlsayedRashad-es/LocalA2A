"""Team A2A SSH: key-only OpenSSH access from hub admins to the cmd shell of an approved PC.

Every command runs through a forced command (the `ssh-shell` of the agent), which logs it on the PC
and in the hub audit log. Keys live in ONE file that only SYSTEM and Administrators can write:
%ProgramData%\\ssh\\administrators_authorized_keys. A SYSTEM task fills it from the hub (`ssh-sync`).

CLI (TeamA2A-SSH.exe is this file built as a console exe; `receiver.py ssh ...` also works):
  python a2a_ssh.py render-config --user NAME
  python a2a_ssh.py sync --config ssh.json --keys-file FILE --launcher C:\\ProgramData\\TeamA2A\\ssh\\teama2a-ssh.cmd
  python a2a_ssh.py hub-cidr --config ssh.json
  python a2a_ssh.py shell --config ssh.json --requester NAME [--log-dir DIR]
Python 3.8+, standard library only.
"""
import argparse
import base64
import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa")
# No leading '-' (it would read as an option); \Z (not $) so a trailing newline cannot slip in.
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}\Z")
USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}\Z")
# The forced-command launcher: a path with no spaces or quotes, so cmd.exe and the
# authorized_keys parser both take it as one plain word.
LAUNCHER_RE = re.compile(r"^[A-Za-z]:\\[A-Za-z0-9_.\\-]+\Z")
KEYS_FILE = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "ssh",
                         "administrators_authorized_keys")
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- checks

def validate_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError(f"bad name '{name}' (use letters, digits, . _ -; at most 64)")
    return name


def validate_user(user):
    if not isinstance(user, str) or not USER_RE.match(user):
        raise ValueError(f"bad Windows user name '{user}'")
    return user


def validate_cidr(cidr):
    """'192.168.1.0/24' or one address. Returns the normalized network text."""
    try:
        net = ipaddress.ip_network(str(cidr).strip(), strict=False)
    except ValueError:
        raise ValueError(f"bad CIDR '{cidr}' (example: 192.168.1.0/24)")
    return str(net)


def validate_pubkey(line):
    """Return 'TYPE BASE64' for one public key line, else raise ValueError.
    Options (from=, command=, ...), quotes, commas, a second line and unknown types are refused.
    A comment after the key is allowed and dropped."""
    if not isinstance(line, str):
        raise ValueError("public key must be text")
    line = line.strip()
    if not line or "\n" in line or "\r" in line or "\0" in line:
        raise ValueError("public key must be one line")
    if any(c in line for c in "\"',"):
        raise ValueError("public key must not contain quotes or commas (no options)")
    m = re.match(r"^(\S+) ([A-Za-z0-9+/]+={0,3})(?: [ -~]*)?\Z", line)
    if not m:
        raise ValueError("public key must look like 'ssh-ed25519 AAAA... comment'")
    ktype, b64 = m.group(1), m.group(2)
    if ktype not in KEY_TYPES:
        raise ValueError(f"key type '{ktype}' is not allowed (use one of {', '.join(KEY_TYPES)})")
    try:
        blob = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        raise ValueError("public key base64 does not decode")
    if len(blob) < 4:
        raise ValueError("public key is too short")
    n = struct.unpack(">I", blob[:4])[0]
    if blob[4:4 + n] != ktype.encode("ascii"):
        raise ValueError("public key data does not match its type")
    return f"{ktype} {b64}"


# ---------------------------------------------------------------- files

def render_sshd_config(user, cidr=None):
    """sshd_config for key-only, command-only access by one Windows user."""
    validate_user(user)
    if cidr is not None:
        validate_cidr(cidr)
    lines = [
        "# Written by Team A2A setup_ssh.ps1. Key-only access; every command goes through the",
        "# Team A2A forced command (it logs on the PC and on the hub).",
        "Port 22",
        "PubkeyAuthentication yes",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "ChallengeResponseAuthentication no",
        "PermitEmptyPasswords no",
        "AuthenticationMethods publickey",
        f"AllowUsers {user}",
        "AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys",
        "AllowTcpForwarding no",
        "AllowAgentForwarding no",
        "AllowStreamLocalForwarding no",
        "X11Forwarding no",
        "PermitTunnel no",
        "GatewayPorts no",
        "PermitTTY no",
        "PermitUserEnvironment no",
        "StrictModes yes",
        "MaxAuthTries 3",
        "LoginGraceTime 30",
        "LogLevel VERBOSE",
        "SyslogFacility LOCAL0",
        "",
    ]
    return "\n".join(lines)


def authorized_keys_line(launcher, requester, cidr, key):
    """One authorized_keys line: only from the hub's network, only the forced command."""
    if not LAUNCHER_RE.match(str(launcher)):
        raise ValueError(f"launcher path '{launcher}' must be like C:\\ProgramData\\TeamA2A\\ssh\\x.cmd "
                         "(no spaces or quotes)")
    validate_name(requester)
    cidr = validate_cidr(cidr)
    key = validate_pubkey(key)
    opts = (f'from="{cidr}",command="{launcher} --requester {requester}",'
            "no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty")
    return f"{opts} {key} a2a:{requester}"


def render_authorized_keys(launcher, cidr, keys):
    """keys = [{'name':..., 'key':...}]; bad entries are skipped. Returns (text, skipped names)."""
    out, skipped = [], []
    for k in keys or []:
        try:
            out.append(authorized_keys_line(launcher, k.get("name"), cidr, k.get("key")))
        except (ValueError, AttributeError):
            skipped.append(str(k.get("name") if isinstance(k, dict) else k))
    return ("\r\n".join(out) + ("\r\n" if out else "")), skipped


def write_in_place(path, text):
    """Rewrite the file without replacing it, so its ACL (SYSTEM + Administrators) stays."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a+b") as f:
        f.seek(0)
        f.truncate()
        f.write(text.encode("utf-8"))


# ---------------------------------------------------------------- hub calls

class HubRefused(Exception):
    def __init__(self, code, msg):
        super().__init__(f"HTTP {code}: {msg}")
        self.code = code


def load_cfg(path):
    with open(path, encoding="utf-8-sig") as f:
        cfg = json.load(f)
    for k in ("name", "hub_url", "token"):
        if not cfg.get(k):
            raise SystemExit(f"{path}: '{k}' is missing")
    cfg["hub_url"] = cfg["hub_url"].rstrip("/")
    return cfg


def hub_post(cfg, path, body, timeout=15):
    req = urllib.request.Request(cfg["hub_url"] + path, data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers={"Content-Type": "application/json",
                                                         "X-A2A-Token": cfg["token"]})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error")
        except Exception:
            msg = str(e)
        raise HubRefused(e.code, msg)


def sync_authorized_keys(cfg, keys_file=KEYS_FILE, launcher=None):
    """Fetch the admins' keys from the hub and write keys_file. Fails closed: when the hub refuses
    (400/401/403/404) the file is emptied; on a network error it is kept. Returns a short result."""
    launcher = launcher or os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                                        "TeamA2A", "ssh", "teama2a-ssh.cmd")
    try:
        r = hub_post(cfg, "/api/worker/ssh_keys", {})
    except HubRefused as e:
        if e.code in (400, 401, 403, 404):
            write_in_place(keys_file, "")
            return {"result": "emptied", "error": str(e), "keys": 0}
        return {"result": "kept", "error": str(e)}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"result": "kept", "error": f"{type(e).__name__}: {e}"}
    text, skipped = render_authorized_keys(launcher, r.get("cidr"), r.get("keys"))
    write_in_place(keys_file, text)
    return {"result": "written", "keys": text.count("\n"), "skipped": skipped, "cidr": r.get("cidr")}


# ---------------------------------------------------------------- forced command

def _log(log_dir, entry):
    try:
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "ssh_commands.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _hub_log(cfg, entry):
    try:
        hub_post(cfg, "/api/worker/ssh_log", entry, timeout=5)
    except Exception:
        pass  # best effort: the PC log has it


def run_shell(cfg, requester, log_dir=None, env=None, stdin=None, stdout=None, stderr=None):
    """The forced command: log, run SSH_ORIGINAL_COMMAND in cmd.exe, log the exit code. Returns it."""
    env = os.environ if env is None else env
    log_dir = log_dir or os.path.join(os.path.dirname(os.path.abspath(cfg.get("_path", "."))), "logs")
    command = env.get("SSH_ORIGINAL_COMMAND", "")
    client_ip = (env.get("SSH_CLIENT") or "").split(" ")[0] or None
    try:
        requester = validate_name(requester)
    except ValueError:
        requester = "?"
    base = {"id": uuid.uuid4().hex[:12], "pc": cfg.get("name") or socket.gethostname(),
            "requester": requester, "command": command, "client_ip": client_ip}
    t0 = time.time()
    if not command.strip():
        entry = {**base, "event": "ssh_refused", "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "error": "no command (interactive shells are not allowed)"}
        _log(log_dir, entry)
        _hub_log(cfg, entry)
        (stderr or sys.stderr).write("Team A2A: give a command; interactive shells are not allowed.\n")
        return 1
    start = {**base, "event": "ssh_start", "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    _log(log_dir, start)
    _hub_log(cfg, start)
    if os.name == "nt":
        p = subprocess.run(f'cmd.exe /d /s /c "{command}"', stdin=stdin, stdout=stdout, stderr=stderr)
    else:  # tests on other systems only
        p = subprocess.run(["/bin/sh", "-c", command], stdin=stdin, stdout=stdout, stderr=stderr)
    end = {**base, "event": "ssh_end", "time": time.strftime("%Y-%m-%d %H:%M:%S"),
           "exit_code": p.returncode, "duration_s": round(time.time() - t0, 3)}
    _log(log_dir, end)
    _hub_log(cfg, end)
    return p.returncode


# ---------------------------------------------------------------- CLI

def main(argv=None, default_config=None):
    ap = argparse.ArgumentParser(description="Team A2A SSH helper")
    ap.add_argument("--config", default=default_config, help="JSON with name, hub_url, token")
    sub = ap.add_subparsers(dest="cmd", required=True)
    rc = sub.add_parser("render-config", help="print the sshd_config")
    rc.add_argument("--user", required=True)
    sy = sub.add_parser("sync", help="write the authorized keys from the hub")
    sy.add_argument("--keys-file", default=KEYS_FILE)
    sy.add_argument("--launcher")
    sub.add_parser("hub-cidr", help="print the network the hub allows SSH from")
    sh = sub.add_parser("shell", help="forced command (sshd runs it)")
    sh.add_argument("--requester", required=True)
    sh.add_argument("--log-dir")
    a = ap.parse_args(argv)
    if a.cmd == "render-config":
        sys.stdout.write(render_sshd_config(a.user))
        return 0
    if not a.config:
        raise SystemExit("--config is required")
    cfg = load_cfg(a.config)
    cfg["_path"] = os.path.abspath(a.config)
    if a.cmd == "sync":
        r = sync_authorized_keys(cfg, a.keys_file, a.launcher)
        print(json.dumps(r))
        return 0 if r["result"] == "written" else 2
    if a.cmd == "hub-cidr":
        try:
            print(validate_cidr(hub_post(cfg, "/api/worker/ssh_keys", {}).get("cidr")))
        except (HubRefused, ValueError, urllib.error.URLError, OSError) as e:
            print(f"hub-cidr failed: {e}", file=sys.stderr)
            return 1
        return 0
    if a.cmd == "shell":
        return run_shell(cfg, a.requester, a.log_dir)
    return 1


if __name__ == "__main__":
    sys.exit(main())
