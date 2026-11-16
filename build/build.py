"""Build the Team A2A installers with PyInstaller.

  build\\.venv\\Scripts\\python build\\build.py --hub-url http://192.168.1.157:8765 [--join-code CODE]

--join-code defaults to the code in hub\\hub.json. Both values are built into TeamA2A-Setup.exe;
a TeamA2A-Setup.json next to the exe overrides them (no rebuild after a new join code).
Output: dist\\TeamA2A-Setup.exe (every PC) and dist\\TeamA2A-Hub-Setup.exe (hub PC only).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "build", "work")
BIN = os.path.join(WORK, "bin")
DIST = os.path.join(ROOT, "dist")


def pyinstaller(script, name, console=False, data=(), paths=()):
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
           "--console" if console else "--noconsole", "--name", name,
           "--distpath", BIN, "--workpath", os.path.join(WORK, "tmp"), "--specpath", os.path.join(WORK, "spec")]
    for p in paths:  # extra import folders (the hub imports a2a_sign / a2a_util from receiver\)
        cmd += ["--paths", p]
    for src, dst in data:
        cmd += ["--add-data", f"{src}{os.pathsep}{dst}"]
    cmd.append(script)
    print(">", name, flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-3000:], r.stderr[-3000:])
        raise SystemExit(f"build of {name} failed")
    return os.path.join(BIN, name + ".exe")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub-url", required=True)
    ap.add_argument("--join-code")
    a = ap.parse_args()
    code = a.join_code
    if not code:
        with open(os.path.join(ROOT, "hub", "hub.json"), encoding="utf-8") as f:
            code = json.load(f).get("join_code", "")
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(BIN)
    os.makedirs(DIST, exist_ok=True)
    defaults = os.path.join(WORK, "setup_defaults.json")
    with open(defaults, "w", encoding="utf-8") as f:
        json.dump({"hub_url": a.hub_url, "join_code": code}, f)

    pyinstaller(os.path.join(ROOT, "receiver", "receiver.py"), "TeamA2A-Agent")
    pyinstaller(os.path.join(ROOT, "admin_mcp", "admin_mcp.py"), "TeamA2A-MCP", console=True)
    pyinstaller(os.path.join(ROOT, "hub", "hub.py"), "TeamA2A-Hub",
                data=[(os.path.join(ROOT, "hub", "dashboard.html"), ".")],
                paths=[os.path.join(ROOT, "receiver")])
    setup = pyinstaller(os.path.join(ROOT, "client", "setup.py"), "TeamA2A-Setup",
                        data=[(os.path.join(BIN, "TeamA2A-Agent.exe"), "bin"),
                              (os.path.join(BIN, "TeamA2A-MCP.exe"), "bin"), (defaults, ".")])
    # Console exe for SSH (setup_ssh.ps1 copies it to %ProgramData%\TeamA2A\ssh): sshd needs its output.
    ssh_exe = pyinstaller(os.path.join(ROOT, "receiver", "a2a_ssh.py"), "TeamA2A-SSH", console=True)
    shutil.copy2(os.path.join(ROOT, "receiver", "setup_ssh.ps1"), DIST)
    hub_setup = pyinstaller(os.path.join(ROOT, "client", "hub_setup.py"), "TeamA2A-Hub-Setup",
                            data=[(os.path.join(BIN, "TeamA2A-Hub.exe"), "bin")])
    for exe in (setup, hub_setup, ssh_exe):
        shutil.copy2(exe, DIST)
    print(f"\nDone: {DIST}\n  TeamA2A-Setup.exe      hub {a.hub_url}, join code built in\n"
          "  TeamA2A-Hub-Setup.exe  for the hub PC\n"
          "  TeamA2A-SSH.exe + setup_ssh.ps1  for IT, to turn on SSH on a PC")


if __name__ == "__main__":
    main()
