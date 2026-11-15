"""TeamA2A-Setup: one installer for every PC (regular users and admins).

The hub owner decides who is an admin; this installer is the same for everyone.
  TeamA2A-Setup.exe                         window
  TeamA2A-Setup.exe --silent [--hub URL] [--code CODE] [--name NAME] [--no-keep-awake]
  TeamA2A-Setup.exe --uninstall [--silent]
Installs per user (no admin rights): %LOCALAPPDATA%\\TeamA2A
"""
import argparse
import getpass
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

APP = "TeamA2A"
VERSION = "1.1"
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP)
AGENT_EXE = "TeamA2A-Agent.exe"
MCP_EXE = "TeamA2A-MCP.exe"
SETUP_EXE = "TeamA2A-Setup.exe"
TASK = "TeamA2A Agent"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\TeamA2A"
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
FROZEN = getattr(sys, "frozen", False)
BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------- helpers

def defaults():
    """Hub address and join code: built into the exe, or a TeamA2A-Setup.json beside it."""
    d = {"hub_url": "", "join_code": ""}
    for path in (os.path.join(BUNDLE, "setup_defaults.json"),
                 os.path.join(os.path.dirname(sys.executable if FROZEN else __file__), "TeamA2A-Setup.json")):
        try:
            with open(path, encoding="utf-8") as f:
                d.update({k: v for k, v in json.load(f).items() if v})
        except (OSError, ValueError):
            pass
    return d


def ps(script, timeout=180):
    """Run a PowerShell script; return (code, output)."""
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=NOWIN)
    return r.returncode, (r.stdout + r.stderr).strip()


def http(method, url, body=None, token=None, timeout=15):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-A2A-Token"] = token
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except ValueError:
            return e.code, {"error": str(e)}


def q(text):
    """Text for a single-quoted PowerShell string."""
    return str(text).replace("'", "''")


def ours_mcp(claude):
    """True when the team-a2a MCP in Claude is the one this installer registered."""
    try:
        r = subprocess.run([claude, "mcp", "get", "team-a2a"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=90, creationflags=NOWIN)
    except Exception:
        return False
    mine = os.path.normcase(os.path.join(APP_DIR, MCP_EXE))
    return r.returncode == 0 and mine in os.path.normcase(r.stdout) and "A2A_ADMIN_TOKEN" not in r.stdout


def find_claude():
    for c in (shutil.which("claude"),
              os.path.join(os.path.expanduser("~"), ".local", "bin", "claude.exe")):
        if c and os.path.exists(c):
            return c
    return None


def claude_logged_in(claude):
    try:
        r = subprocess.run([claude, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=60, creationflags=NOWIN)
        return bool(json.loads(r.stdout).get("loggedIn"))
    except Exception:
        return False


def stop_agent():
    """Stop this user's agent (and its claude children)."""
    ps(f"Stop-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue")
    subprocess.run(["taskkill", "/FI", f"USERNAME eq {os.environ.get('USERNAME', '')}", "/IM", AGENT_EXE,
                    "/T", "/F"], capture_output=True, creationflags=NOWIN)
    time.sleep(1)


def replace_file(src, dst):
    """Copy over an exe that may still run (an open Claude keeps TeamA2A-MCP.exe loaded)."""
    if os.path.exists(dst):
        old = dst + ".old"
        try:
            if os.path.exists(old):
                os.remove(old)
        except OSError:
            old = dst + f".{int(time.time())}.old"
        try:
            os.replace(dst, old)  # Windows allows renaming a running exe
        except OSError:
            pass
    shutil.copy2(src, dst)


# ---------------------------------------------------------------- install

class Installer:
    def __init__(self, log):
        self.log = log

    def step_copy(self):
        self.log("Copying program files ...")
        os.makedirs(APP_DIR, exist_ok=True)
        stop_agent()
        src_dir = os.path.join(BUNDLE, "bin")
        for exe in (AGENT_EXE, MCP_EXE):
            replace_file(os.path.join(src_dir, exe), os.path.join(APP_DIR, exe))
        if FROZEN and os.path.normcase(sys.executable) != os.path.normcase(os.path.join(APP_DIR, SETUP_EXE)):
            replace_file(sys.executable, os.path.join(APP_DIR, SETUP_EXE))  # kept for uninstall

    def step_claude(self, install_if_missing=True):
        claude = find_claude()
        if not claude and install_if_missing:
            self.log("Claude Code CLI is missing. Installing it (official installer) ...")
            code, out = ps("irm https://claude.ai/install.ps1 | iex", timeout=900)
            self.log(out[-800:] if out else f"installer exit {code}")
            claude = find_claude()
        if not claude:
            raise RuntimeError("Claude Code CLI is not installed. Install it from "
                               "https://claude.com/claude-code and run this setup again.")
        self.log(f"Claude Code CLI: {claude}")
        if not claude_logged_in(claude):
            self.log("Claude Code is not signed in. A window opens: sign in with your company account.")
            subprocess.Popen([claude, "auth", "login"], creationflags=subprocess.CREATE_NEW_CONSOLE)
            for _ in range(180):  # wait up to 15 minutes
                time.sleep(5)
                if claude_logged_in(claude):
                    break
            else:
                raise RuntimeError("Claude Code is still not signed in. Sign in (claude auth login), "
                                   "then run this setup again.")
        self.log("Claude Code is signed in.")
        return claude

    def step_join(self, hub_url, code, name):
        hub_url = hub_url.strip().rstrip("/")
        if not hub_url.startswith("http"):
            hub_url = "http://" + hub_url
        if ":" not in hub_url.split("//", 1)[1]:
            hub_url += ":8765"
        cfg_path = os.path.join(APP_DIR, "receiver.json")
        old = {}
        try:
            with open(cfg_path, encoding="utf-8") as f:
                old = json.load(f)
        except (OSError, ValueError):
            pass
        self.log(f"Connecting to the hub {hub_url} ...")
        try:
            st, _ = http("GET", hub_url + "/health", timeout=10)
        except Exception as e:
            raise RuntimeError(f"Cannot reach the hub at {hub_url}: {e}")
        if st != 200:
            raise RuntimeError(f"The hub at {hub_url} did not answer (HTTP {st}).")
        if old.get("token") and old.get("hub_url", "").rstrip("/") == hub_url:
            st, _ = http("POST", hub_url + "/api/worker/heartbeat", {}, token=old["token"])
            if st == 200:
                self.log(f"This PC is already a member of the hub as '{old['name']}'.")
                return hub_url, old["name"], old["token"]
        st, r = http("POST", hub_url + "/api/enroll",
                     {"join_code": code.strip(), "name": name.strip() or socket.gethostname(),
                      "host": socket.gethostname(), "user": getpass.getuser()})
        if st != 200:
            raise RuntimeError(f"The hub refused this PC: {r.get('error', st)}")
        self.log(f"Joined the hub as '{r['name']}'. The hub owner must approve this PC "
                 "before it gets tasks.")
        return hub_url, r["name"], r["token"]

    def step_config(self, hub_url, name, token, claude):
        path = os.path.join(APP_DIR, "receiver.json")
        cfg = {}
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            pass
        cfg.update({"name": name, "hub_url": hub_url, "token": token,
                    "claude_cmd": [claude, "-p", "--output-format", "json", "--dangerously-skip-permissions"],
                    "mcp_cmd": [os.path.join(APP_DIR, MCP_EXE)]})
        cfg.setdefault("workdir", "")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, path)

    def step_autostart(self):
        self.log("Setting the agent to start at Windows sign-in ...")
        agent = os.path.join(APP_DIR, AGENT_EXE)
        script = f"""
$ErrorActionPreference = 'Stop'
$user = "$env:USERDOMAIN\\$env:USERNAME"
$a = New-ScheduledTaskAction -Execute '{q(agent)}' -WorkingDirectory '{q(APP_DIR)}'
$t = New-ScheduledTaskTrigger -AtLogOn -User $user
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
     -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
     -MultipleInstances IgnoreNew
$p = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $t -Settings $s -Principal $p `
     -Description 'Team A2A agent: runs Claude Code tasks from the office hub' -Force | Out-Null
Start-ScheduledTask -TaskName '{TASK}'
"""
        code, out = ps(script)
        if code == 0:
            self.log("Autostart: Task Scheduler.")
            return
        # Fallback without Task Scheduler: the user's Run key, and start it now.
        self.log(f"Task Scheduler refused ({out[-200:]}). Using the Run key.")
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, f'"{agent}"')
        subprocess.Popen([agent], cwd=APP_DIR, creationflags=subprocess.DETACHED_PROCESS | NOWIN)

    def step_keep_awake(self):
        code, out = ps("powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0")
        self.log("Sleep on AC power: off." if code == 0 else
                 "Could not turn off sleep (ask IT to set 'Sleep: Never' on AC power).")

    def step_register(self):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            for name, val in (("DisplayName", "Team A2A"), ("DisplayVersion", VERSION),
                              ("Publisher", "FirstOption"), ("InstallLocation", APP_DIR),
                              ("DisplayIcon", os.path.join(APP_DIR, AGENT_EXE)),
                              ("UninstallString", f'"{os.path.join(APP_DIR, SETUP_EXE)}" --uninstall')):
                winreg.SetValueEx(k, name, 0, winreg.REG_SZ, val)
            winreg.SetValueEx(k, "NoModify", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(k, "NoRepair", 0, winreg.REG_DWORD, 1)

    def step_check(self, hub_url, token):
        self.log("Checking that the agent is online ...")
        started = time.time() - 5
        for _ in range(20):
            time.sleep(2)
            st, r = http("POST", hub_url + "/api/worker/heartbeat", {}, token=token)
            if st == 200:
                break
        logp = os.path.join(APP_DIR, "logs", "receiver.jsonl")
        for _ in range(15):
            if os.path.exists(logp) and os.path.getmtime(logp) >= started:
                self.log("The agent is running.")
                return
            time.sleep(2)
        self.log("The agent did not write its log yet. Check Task Scheduler > 'TeamA2A Agent'.")

    def install(self, hub_url, code, name, keep_awake=True):
        if not hub_url:
            raise RuntimeError("Enter the hub address (for example 192.168.1.157).")
        self.step_copy()
        claude = self.step_claude()
        hub_url, name, token = self.step_join(hub_url, code, name)
        self.step_config(hub_url, name, token, claude)
        self.step_autostart()
        if keep_awake:
            self.step_keep_awake()
        self.step_register()
        self.step_check(hub_url, token)
        self.log("Done. Keep this PC signed in at night: lock it (Win+L), do not sign out.")
        return name


def uninstall(log):
    import winreg
    log("Stopping the agent ...")
    stop_agent()
    ps(f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false -ErrorAction SilentlyContinue")
    for key, value in ((r"Software\Microsoft\Windows\CurrentVersion\Run", APP),):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, value)
        except OSError:
            pass
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass
    claude = find_claude()
    if claude and ours_mcp(claude):  # never remove a team-a2a MCP someone made by hand
        subprocess.run([claude, "mcp", "remove", "team-a2a", "-s", "user"], capture_output=True,
                       creationflags=NOWIN, timeout=60)
    log("Removing files ...")
    if FROZEN and os.path.normcase(os.path.dirname(sys.executable)) == os.path.normcase(APP_DIR):
        # This program runs from the folder: delete it after this process ends.
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
                          f"Wait-Process -Id {os.getpid()} -Timeout 600 -ErrorAction SilentlyContinue; "
                          f"Start-Sleep 1; Remove-Item -Recurse -Force -LiteralPath '{q(APP_DIR)}'"],
                         creationflags=NOWIN | subprocess.DETACHED_PROCESS)
    else:
        shutil.rmtree(APP_DIR, ignore_errors=True)
    log("Team A2A is removed. Ask the hub owner to remove this PC from the hub.")


# ---------------------------------------------------------------- window

def gui(d):
    import tkinter as tk
    from tkinter import ttk, messagebox

    root = tk.Tk()
    root.title("Team A2A Setup")
    root.geometry("560x470")
    root.resizable(False, True)
    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="Team A2A", font=("Segoe UI", 15, "bold")).pack(anchor="w")
    ttk.Label(frm, wraplength=520, justify="left",
              text="Lets the office admins send tasks to Claude Code on this PC. Tasks run automatically, "
                   "also at night, with your Claude account. The hub owner decides who is an admin.").pack(
        anchor="w", pady=(2, 10))
    form = ttk.Frame(frm)
    form.pack(fill="x")
    v_hub = tk.StringVar(value=d.get("hub_url", ""))
    v_code = tk.StringVar(value=d.get("join_code", ""))
    v_name = tk.StringVar(value=socket.gethostname())
    v_awake = tk.BooleanVar(value=True)
    rows = [("Hub address", v_hub, ""), ("Join code", v_code, "*"), ("PC name", v_name, "")]
    for i, (label, var, show) in enumerate(rows):
        ttk.Label(form, text=label).grid(row=i, column=0, sticky="w", pady=3)
        e = ttk.Entry(form, textvariable=var, width=48, show=show)
        e.grid(row=i, column=1, sticky="we", pady=3, padx=(8, 0))
        if label == "Join code" and d.get("join_code"):
            e.configure(state="disabled")  # built into this installer
    ttk.Checkbutton(frm, text="Keep this PC awake on AC power (needed for night tasks)",
                    variable=v_awake).pack(anchor="w", pady=(6, 6))
    btns = ttk.Frame(frm)
    btns.pack(fill="x")
    out = tk.Text(frm, height=12, wrap="word", state="disabled", font=("Consolas", 9))
    out.pack(fill="both", expand=True, pady=(8, 0))

    def log(msg):
        def put():
            out.configure(state="normal")
            out.insert("end", msg + "\n")
            out.see("end")
            out.configure(state="disabled")
        root.after(0, put)

    def run(job, done_msg):
        b_inst.configure(state="disabled")
        b_un.configure(state="disabled")

        def work():
            try:
                job()
                root.after(0, lambda: messagebox.showinfo("Team A2A", done_msg))
            except Exception as e:
                msg = str(e)
                log(f"ERROR: {msg}")
                root.after(0, lambda m=msg: messagebox.showerror("Team A2A", m))
            finally:
                root.after(0, lambda: (b_inst.configure(state="normal"), b_un.configure(state="normal")))
        threading.Thread(target=work, daemon=True).start()

    def do_install():
        run(lambda: Installer(log).install(v_hub.get(), v_code.get(), v_name.get(), v_awake.get()),
            "Team A2A is installed and running.")

    def do_uninstall():
        if messagebox.askyesno("Team A2A", "Remove Team A2A from this PC?"):
            run(lambda: uninstall(log), "Team A2A is removed.")

    b_inst = ttk.Button(btns, text="Install", command=do_install)
    b_inst.pack(side="left")
    b_un = ttk.Button(btns, text="Uninstall", command=do_uninstall)
    b_un.pack(side="left", padx=8)
    if not os.path.exists(os.path.join(APP_DIR, "receiver.json")):
        b_un.configure(state="disabled")
    root.mainloop()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Team A2A setup")
    ap.add_argument("--silent", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--hub")
    ap.add_argument("--code")
    ap.add_argument("--name", default="")
    ap.add_argument("--no-keep-awake", action="store_true")
    a = ap.parse_args(argv)
    d = defaults()
    if a.hub:
        d["hub_url"] = a.hub
    if a.code:
        d["join_code"] = a.code
    logf = os.path.join(os.environ.get("TEMP", APP_DIR), "TeamA2A-Setup.log")

    def log(msg):
        with open(logf, "a", encoding="utf-8") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
        if sys.stdout:
            print(msg, flush=True)

    if a.uninstall and a.silent:
        uninstall(log)
        return 0
    if a.silent:
        try:
            Installer(log).install(d["hub_url"], d["join_code"], a.name, not a.no_keep_awake)
            return 0
        except Exception as e:
            log(f"ERROR: {e}")
            return 1
    if a.uninstall:
        import tkinter.messagebox as mb
        import tkinter as tk
        tk.Tk().withdraw()
        if mb.askyesno("Team A2A", "Remove Team A2A from this PC?"):
            uninstall(log)
            mb.showinfo("Team A2A", "Team A2A is removed.")
        return 0
    gui(d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
