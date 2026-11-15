"""TeamA2A-Hub-Setup: installs the hub on the PC (or server) that will host it.

  TeamA2A-Hub-Setup.exe               window
  TeamA2A-Hub-Setup.exe --silent [--owner NAME] [--port 8765]
Installs per user in %LOCALAPPDATA%\\TeamA2A-Hub, starts at sign-in, opens the firewall port (asks for admin).
"""
import argparse
import getpass
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "TeamA2A-Hub")
HUB_EXE = "TeamA2A-Hub.exe"
TASK = "TeamA2A Hub"
NOWIN = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
BUNDLE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def ps(script, timeout=120):
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=NOWIN)
    return r.returncode, (r.stdout + r.stderr).strip()


def q(text):
    return str(text).replace("'", "''")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no traffic is sent; picks the LAN interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return socket.gethostbyname(socket.gethostname())


def install(owner, port, log):
    os.makedirs(APP_DIR, exist_ok=True)
    log("Stopping an old hub ...")
    ps(f"Stop-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue")
    subprocess.run(["taskkill", "/IM", HUB_EXE, "/F"], capture_output=True, creationflags=NOWIN)
    time.sleep(1)
    shutil.copy2(os.path.join(BUNDLE, "bin", HUB_EXE), os.path.join(APP_DIR, HUB_EXE))

    cfg_path = os.path.join(APP_DIR, "hub.json")
    cfg = {}
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        log("Kept the existing hub.json (PCs, tokens, join code).")
    cfg.setdefault("bind", "0.0.0.0")
    cfg["port"] = int(port)
    cfg.setdefault("data_dir", os.path.join(APP_DIR, "data"))
    cfg.setdefault("report_hour", 7)
    cfg.setdefault("task_timeout_s", 3600)
    cfg.setdefault("pcs", {})
    cfg.setdefault("admins", {})
    cfg.setdefault("join_code", secrets.token_urlsafe(9))
    if owner not in cfg["admins"]:
        cfg["admins"][owner] = secrets.token_urlsafe(24)
    with open(cfg_path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(cfg_path + ".tmp", cfg_path)

    log("Setting the hub to start at Windows sign-in ...")
    exe = os.path.join(APP_DIR, HUB_EXE)
    code, out = ps(f"""
$ErrorActionPreference = 'Stop'
$user = "$env:USERDOMAIN\\$env:USERNAME"
$a = New-ScheduledTaskAction -Execute '{q(exe)}' -WorkingDirectory '{q(APP_DIR)}'
$t = New-ScheduledTaskTrigger -AtLogOn -User $user
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
     -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
     -MultipleInstances IgnoreNew
$p = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $t -Settings $s -Principal $p `
     -Description 'Team A2A hub' -Force | Out-Null
Start-ScheduledTask -TaskName '{TASK}'
""")
    if code != 0:
        raise RuntimeError(f"Task Scheduler refused: {out[-300:]}")

    log("Opening the firewall port (Windows asks for admin rights) ...")
    rule = (f"Remove-NetFirewallRule -DisplayName 'TeamA2A Hub' -ErrorAction SilentlyContinue; "
            f"New-NetFirewallRule -DisplayName 'TeamA2A Hub' -Direction Inbound -LocalPort {port} "
            f"-Protocol TCP -Action Allow -Profile Domain,Private | Out-Null")
    code, out = ps(f"Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
                   f"-ArgumentList '-NoProfile','-Command',\"{rule}\"", timeout=300)
    code, _ = ps("if (Get-NetFirewallRule -DisplayName 'TeamA2A Hub' -ErrorAction SilentlyContinue) "
                 "{ exit 0 } else { exit 1 }")
    log("Firewall: port open." if code == 0 else
        f"Firewall: not changed. Other PCs cannot connect until port {port} is open.")

    url = f"http://{lan_ip()}:{port}"
    for _ in range(30):
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
                    f"http://127.0.0.1:{port}/health", timeout=3) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("The hub did not start. Check Task Scheduler > 'TeamA2A Hub'.")

    share = os.path.join(APP_DIR, "share")
    os.makedirs(share, exist_ok=True)
    with open(os.path.join(share, "TeamA2A-Setup.json"), "w", encoding="utf-8") as f:
        json.dump({"hub_url": url, "join_code": cfg["join_code"]}, f, indent=2)
    with open(os.path.join(APP_DIR, "OWNER-TOKEN.txt"), "w", encoding="utf-8") as f:
        f.write(f"Hub owner: {owner}\nDashboard: {url}/\nOwner token: {cfg['admins'][owner]}\n")
    log(f"Hub is running: {url}/")
    log(f"Owner token and dashboard link: {os.path.join(APP_DIR, 'OWNER-TOKEN.txt')}")
    log(f"For the client installer: put {os.path.join(share, 'TeamA2A-Setup.json')} "
        "next to TeamA2A-Setup.exe on the share.")
    return url


def gui():
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = tk.Tk()
    root.title("Team A2A Hub Setup")
    root.geometry("600x420")
    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="Team A2A Hub", font=("Segoe UI", 15, "bold")).pack(anchor="w")
    ttk.Label(frm, wraplength=560, justify="left",
              text="Install the hub on the PC that stays on at night. All PCs connect to it.").pack(anchor="w")
    form = ttk.Frame(frm)
    form.pack(fill="x", pady=8)
    v_owner = tk.StringVar(value=getpass.getuser().lower())
    v_port = tk.StringVar(value="8765")
    for i, (lab, var) in enumerate((("Owner name", v_owner), ("Port", v_port))):
        ttk.Label(form, text=lab).grid(row=i, column=0, sticky="w", pady=3)
        ttk.Entry(form, textvariable=var, width=30).grid(row=i, column=1, sticky="w", padx=8)
    out = tk.Text(frm, height=12, wrap="word", state="disabled", font=("Consolas", 9))

    def log(msg):
        def put():
            out.configure(state="normal")
            out.insert("end", msg + "\n")
            out.see("end")
            out.configure(state="disabled")
        root.after(0, put)

    def go():
        b.configure(state="disabled")

        def work():
            try:
                url = install(v_owner.get().strip() or "owner", int(v_port.get()), log)
                root.after(0, lambda: messagebox.showinfo("Team A2A Hub", f"The hub is running.\n{url}/"))
                os.startfile(APP_DIR)
            except Exception as e:
                msg = str(e)
                log(f"ERROR: {msg}")
                root.after(0, lambda m=msg: messagebox.showerror("Team A2A Hub", m))
            finally:
                root.after(0, lambda: b.configure(state="normal"))
        threading.Thread(target=work, daemon=True).start()

    b = ttk.Button(frm, text="Install hub", command=go)
    b.pack(anchor="w")
    out.pack(fill="both", expand=True, pady=(8, 0))
    root.mainloop()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--silent", action="store_true")
    ap.add_argument("--owner", default=getpass.getuser().lower())
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args(argv)
    if a.silent:
        try:
            install(a.owner, a.port, lambda m: print(m, flush=True) if sys.stdout else None)
            return 0
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr) if sys.stderr else None
            return 1
    gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
