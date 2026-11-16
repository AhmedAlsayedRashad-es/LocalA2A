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

The receiver runs up to `max_parallel` tasks at once (default 3). Logs: `receiver\logs\receiver.jsonl`. Remove: `uninstall_receiver.ps1`.
Optional `receiver.json` keys: `workdir` (default: user home), `extra_system_prompt`, `claude_cmd`,
`max_parallel` (default 3; 1 = one task at a time), `auto_update` (default true), `update_check_s` (default 3600),
`update_pubkey` (the hub's update key; written by `TeamA2A-Setup.exe`, or copy it from `hub.py add-pc`).

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
`get_broadcast`, `list_tasks`, `morning_report`, `cancel`, `send_file`, `fetch_file`, `list_transfers`, `ssh_run`.
For `ssh_run`: optional env `A2A_SSH_KEY` (default `~\.ssh\id_ed25519`) and `A2A_SSH_EXE` (default: `ssh` on PATH).
Ask your Claude, for example: "Ask group:revit which Revit versions are installed" or "Show the morning report".

## How a task works

- Each send makes a thread per PC. A reply in the thread continues the same Claude session (`--resume`).
- Task status: `queued` → `running` → `done` / `failed` / `canceled` / `needs_input`.
  `needs_input` = the remote Claude asked a question (reply starts with `QUESTION:`); answer with `send` + `context_id`.
- Offline PCs keep their tasks queued and run them when they come online.

## Parallel tasks

- A PC runs up to `max_parallel` tasks at once (receiver.json, default 3). The hub never runs two turns of one
  thread at the same time; later turns of a thread wait for the earlier one.
- Each thread runs in its own folder `<workdir>\a2a-sessions\<thread id>`, so `--resume` finds its session.
- Old receivers (1.1) keep working: they get one task at a time.

## File transfer

Files go through the hub (PCs only connect out to the hub; no port opens on colleague PCs).

- Admin to PC: MCP `send_file {to, path}`. The file arrives in `<workdir>\a2a-inbox` on that PC
  (`name (2).ext` when the name exists).
- PC to admin: MCP `fetch_file {pc, path, save_to}`. A relative path is read from the PC's work folder.
  Default save folder: `~\a2a-inbox`.
- PC to PC: the remote Claude runs `receiver.py send-file --to PC-NAME FILE` (the system prompt tells it how).
- Limits and checks: `max_transfer_mb` in hub.json (default 200, checked before the upload is read), sha256 checked by
  the hub and again by the receiver, plain file names only, sender and receiver must be approved. Files nobody
  fetched expire after `transfer_ttl_h` (default 24) and are deleted. Every step is in the audit log
  (`transfer_upload`, `transfer_delivered`, `transfer_rejected`, `transfer_failed`, `transfer_expired`).
- MCP `list_transfers` shows the status: `stored` / `offered` (waits for the PC), `delivered`, `failed`, `expired`.

## Signed updates of the receiver

1. Set `VERSION = "1.3"` in `receiver\receiver.py` (it must match the release version).
2. For PCs installed with `TeamA2A-Setup.exe`: build again and put `TeamA2A-Agent.exe` (from `build\work\bin`) in the
   source folder too. Script installs only need the `.py` files.
3. On the hub PC: `python hub.py release --version 1.3 [--source ..\receiver]`. The hub signs the package with its
   update key (`<data_dir>\update_signing_key.json`, never served, never in git). `*.json` files never go in.
4. Each receiver with `update_pubkey` checks every `update_check_s` (and when the hub says a newer version exists),
   checks the signature, the version (only newer), the size and every file's sha256, waits until its tasks end,
   backs up the old files to `update_backup\<old version>\`, swaps the files and starts again.

- `python hub.py update-key` prints the public key. `add-pc` prints it in the receiver.json; `TeamA2A-Setup.exe`
  writes it when the PC joins. A receiver without `update_pubkey` never installs updates. PCs that joined before
  this version: run the new `TeamA2A-Setup.exe` once; it keeps the membership and pins the key.
- Turn off on one PC: `"auto_update": false` in its receiver.json.

## SSH to a colleague PC (cmd.exe)

For quick checks without Claude: `ssh_run {pc, command}` runs one cmd.exe command over SSH and returns output
and exit code. Key only, no password, no interactive shell, no forwarding. The PC logs every command in
`%LOCALAPPDATA%\TeamA2A\logs\ssh_commands.jsonl` and the hub audits it (`ssh_request`, `ssh_command`).

1. Hub owner, once:
   `python hub.py ssh-cidr 192.168.1.0/24` (the network PCs accept SSH from) and
   `python hub.py ssh-add-key ahmed "ssh-ed25519 AAAA... ahmed"` per admin (`ssh-remove-key NAME` to remove).
   Make a key with `ssh-keygen -t ed25519` (it goes to `~\.ssh\id_ed25519`).
2. IT, once per PC, elevated, with `TeamA2A-SSH.exe` and `setup_ssh.ps1` from `dist\`:
   `powershell -ExecutionPolicy Bypass -File setup_ssh.ps1 -User colleague -ReceiverConfig C:\Users\colleague\AppData\Local\TeamA2A\receiver.json`
   It installs OpenSSH Server, writes a key-only `sshd_config` (`AllowUsers` that user), opens port 22 only for the
   hub network, sets cmd.exe as the SSH shell and adds the SYSTEM task "TeamA2A SSH key sync" (every 15 min).
3. Keys of admins who are removed from the hub disappear from the PCs at the next sync. A PC that is removed or
   not approved gets an empty keys file.

## Tests

```
python -m unittest discover -s tests -v
```

The tests start a real hub, real receivers and the admin MCP, with `tests/fake_claude.py` in place of `claude`.
Use `discover -s tests` (a `tests` package in site-packages can hide this folder). Inside a Team A2A task, unset
`A2A_IN_TASK` first. `test_installers` needs a PyInstaller build and is skipped without one.

## Old: Codex to Codex (2 PCs)

`a2a_server.py`, `a2a_send.py`, `a2a_relay.py`, `config.json` are the first two-PC Codex version. They still work and do not touch the team version.
