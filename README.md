# A2ALocal — Team A2A

Admins' Claude sends requests to the Claude Code agent on colleague PCs over the office LAN.
The work runs on the colleague's PC, in their Windows session, on their Claude seat, with their MCPs (Revit, AutoCAD, Civil 3D).
Python 3.8+ only, no packages. Plan and decisions: [PLAN.md](PLAN.md).

```
admin Claude ──MCP──► admin_mcp.py ──HTTP──► hub.py (your PC, :8765) ◄──poll── receiver.py ──► claude -p   (each colleague PC)
browser ───────────── dashboard ─────────────┘
```

## Installers (recommended)

Build: `build\.venv\Scripts\python build\build.py --hub-url http://HUB-IP:8765` → `dist\`.

| File | Who runs it | What it does |
|---|---|---|
| `TeamA2A-Setup.exe` | every PC (users and admins) | Per user, no admin rights. Installs Claude Code CLI if missing and waits for sign-in, joins the hub with the built-in join code, starts the agent at sign-in, turns off sleep on AC, adds "Team A2A" to Apps (uninstall). `--silent` for IT. |
| `TeamA2A-Hub-Setup.exe` | the hub PC only | Installs the hub in `%LOCALAPPDATA%\TeamA2A-Hub`, starts it at sign-in, opens the firewall port (UAC prompt), writes `OWNER-TOKEN.txt` and `share\TeamA2A-Setup.json`. |

- New PCs join as **pending**: they get no tasks until the hub owner clicks **Approve** in the dashboard (or **Reject**; CLI: `python hub.py approve NAME`). Approved PCs are regular users. The owner makes a PC an admin in the PCs tab (Make admin / Make user / Remove, groups, join code).
- An admin PC gets the `team-a2a` MCP in its Claude by itself, and loses it when made a user again.
- New join code: dashboard "New code". Put the new `TeamA2A-Setup.json` (`hub_url`, `join_code`) next to `TeamA2A-Setup.exe`, or build again.
- Silent: `TeamA2A-Setup.exe --silent [--hub 192.168.1.157] [--code CODE] [--name PC-NAME] [--no-keep-awake]`, `TeamA2A-Setup.exe --uninstall --silent`. Log: `%TEMP%\TeamA2A-Setup.log`.
- Some antivirus tools flag new PyInstaller programs. Sign the exes or add an exception if that happens.

## 1. Hub (your PC, by hand)

```
cd hub
python hub.py add-admin ahmed                      # prints your admin token
python hub.py add-pc ENG-PC-07 --groups revit,cad --hub-url http://YOUR-IP:8765
python hub.py                                      # start the hub
```

- `add-pc` prints the `receiver.json` for that PC. The running hub loads new PCs and admins by itself.
- Open the port once (PowerShell as admin): `New-NetFirewallRule -DisplayName TeamA2A -Direction Inbound -LocalPort 8765 -Protocol TCP -Action Allow`
- Dashboard: `http://YOUR-IP:8765/` (sign in with the admin token).
- Data: `hub/data/hub.db`, audit log `hub/data/audit.jsonl`, daily report `hub/data/reports/` (at `report_hour`, default 7).
- Keep your PC on at night; the hub runs on it. Start it at login: `powershell -ExecutionPolicy Bypass -File .\install_hub.ps1`.
- Put `data_dir` in `hub.json` on a local C: folder (this PC: `%LOCALAPPDATA%\TeamA2A\hub`). On drive D: something held `hub.db` for many seconds and the hub stopped answering.
- `python hub.py list` / `python hub.py remove NAME` (the token stops working at once).

## 2. Receiver (each colleague PC)

Needs: Python 3.8+, Claude Code CLI installed and signed in to the colleague's seat (`claude`, then `/login`).

1. Copy the `receiver` folder to the PC.
2. Save the JSON from `add-pc` as `receiver\receiver.json`.
3. As the colleague's Windows user: `powershell -ExecutionPolicy Bypass -File .\install_receiver.ps1`
   (add `-NoSleep` in an elevated PowerShell to turn off sleep on AC power).
4. At night: lock the PC (Win+L). Do not log off and do not let it sleep.

The receiver runs one task at a time. Logs: `receiver\logs\receiver.jsonl`. Remove: `uninstall_receiver.ps1`.
Optional `receiver.json` keys: `workdir` (default: user home), `extra_system_prompt`, `claude_cmd`.

## 3. Admin MCP (each admin's Claude)

Add to the Claude desktop app / Claude Code MCP config:

```json
"team-a2a": {
  "command": "python",
  "args": ["H:\\A2ALocal\\admin_mcp\\admin_mcp.py"],
  "env": { "A2A_HUB_URL": "http://127.0.0.1:8765", "A2A_ADMIN_TOKEN": "your-admin-token" }
}
```

Tools: `list_pcs`, `send` (to a PC, `group:NAME`, `all`, or continue a thread with `context_id`), `get_thread`,
`get_broadcast`, `list_tasks`, `morning_report`, `cancel`.
Ask your Claude, for example: "Ask group:revit which Revit versions are installed" or "Show the morning report".

## How a task works

- Each send makes a thread per PC. A reply in the thread continues the same Claude session (`--resume`).
- Task status: `queued` → `running` → `done` / `failed` / `canceled` / `needs_input`.
  `needs_input` = the remote Claude asked a question (reply starts with `QUESTION:`); answer with `send` + `context_id`.
- Offline PCs keep their tasks queued and run them when they come online.

## Tests

```
python -m unittest tests.test_e2e -v
```

The tests start a real hub, real receivers and the admin MCP, with `tests/fake_claude.py` in place of `claude`.

## Old: Codex to Codex (2 PCs)

`a2a_server.py`, `a2a_send.py`, `a2a_relay.py`, `config.json` are the first two-PC Codex version. They still work and do not touch the team version.
