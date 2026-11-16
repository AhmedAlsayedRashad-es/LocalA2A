# Summary

All four features are done on branch `feature/parallel-ftp-update-ssh` (2026-11-16). Full suite: 79 tests OK
(`python -m unittest discover -s tests`, with `A2A_IN_TASK` unset), including `test_installers` against freshly built exes.

| Feature | Tests | State |
|---|---|---|
| 1 Parallel sessions | `test_parallel` 7/7, `test_e2e` 20/20 | works |
| 2 File transfer (direct first, hub relay as fallback) | `test_direct` 9/9, `test_transfer` 11/11 | works on one PC; a real LAN is a human check |
| 3 Signed receiver updates | `test_sign` 3/3, `test_update` 12/12 | works for script installs; exe swap is a human check |
| 4 SSH to cmd.exe | `test_ssh` 15/15 | works up to sshd; real sshd login is a human check |

Human checks (cannot be tested on this PC without admin rights or a second PC):
- Feature 2: direct mode between two real PCs, with the real firewall rule (tests ran on 127.0.0.1 only).
- Feature 3: trust on first use of the update key; one real exe update on an installed PC.
- Feature 4: run `setup_ssh.ps1` on one PC (elevated), then one `ssh_run` from an admin PC.

## Run log

- Branch: `feature/parallel-ftp-update-ssh` (from `main` 77594c391a2021b3bf9ffac9e49318841747f7c3).
- Baseline on main: `test_e2e` 20 tests OK in about 14 s, with `A2A_IN_TASK` unset. With `A2A_IN_TASK=1` (this session runs inside a Team A2A task), test_14 failed. Fixed: test_14 now removes `A2A_IN_TASK` from the child env.
- README test command `python -m unittest tests.test_e2e` fails on this PC: a `tests` package in site-packages shadows the repo folder. README now says `python -m unittest discover -s tests`.
- Models: all Claude Opus 5.5 (`claude-opus-5-5`). First run (2026-11-15/16): planners high, executor medium; stopped after feature 1. Second run (2026-11-16): one Opus 5.5 session finished steps 1A-9 to 1A-16 and Plan 4.1, no sub-agents.
- Planning: one planner run was cut off by a safety classifier, so features 1-3 and feature 4 were planned separately (PLAN-FEATURES.md).
- Exes rebuilt with `build\build.py --hub-url http://192.168.1.157:8765` (same hub and join code as before). The old installers are kept in `dist\previous-2026-11-12\`. `test_installers` ran against the new build: 2/2 OK. That also proves the hub exe finds `a2a_sign` / `a2a_util` / `a2a_ssh` (`--paths receiver`).
- `TeamA2A-SSH.exe` smoke test: `render-config` and `shell` (runs `echo`, exit 0) work from the built exe.

## Feature 1: parallel sessions per PC

- What: per-thread folder `<workdir>/a2a-sessions/<context_id>`. Why: `claude --resume` finds a session by its working folder, so every turn of a thread must run in the same folder, and different threads must not share one. Rejected: a folder per task, which would break `--resume`.
- What: hub never runs two tasks of the same thread at once, and later turns stay queued in order. Why: two turns would write to the same Claude session at once. Rejected: free parallelism per task.
- What: old receivers (no `slots` in the poll) still get one task at a time. Why: PCs update at different times. Rejected: a protocol break that forces all PCs to update together.
- What: default `max_parallel` 3, and 1 gives the old behaviour. Why: several tasks at once without overloading a colleague PC or their Claude seat limits. Rejected: no limit.

## Feature 2: file transfer

- What (first version): transfers go through the hub (upload, store, receiver downloads on its poll). Why: PCs only connect out to the hub, with no firewall change on colleague PCs.
- What (2026-11-16, the admin asked for direct mode): **direct first, hub relay as fallback.** Each PC with `"direct": true` runs a file server on port 8766. The hub gives the sender a ticket and the bytes go straight between the PCs. Why: the admin wants the files not to pass through the hub. Rejected: direct only, because a PC without the firewall rule, an offline PC, or a PC on another network could then get no files at all.
- What: the ticket is HMAC-SHA256 over its claims (id, op, src, dst, name, size, sha256 or path, expiry 10 min), with the token of the PC that serves or takes the file. The PC checks it alone, takes each ticket once, and reports the result to the hub (audit). Why: only the hub and that PC know the token, so nobody else can make a ticket, and the PC needs no extra call to the hub. Rejected: the PC asking the hub for every request (one more round trip, and it fails when the hub is slow).
- What: the firewall rule is for the agent exe only, port 8766, profiles Domain and Private, remote address LocalSubnet. `TeamA2A-Setup.exe` asks for admin rights once (UAC) and turns `direct` on only when the rule exists. Silent installs never show a UAC prompt. Why: without a rule, Windows shows a firewall pop-up on the colleague's screen when the agent opens the port; the plan says no pop-ups. Rejected: opening the port for all programs.
- What: a PC cannot fetch files from another PC directly (`op: get` is for admins only); PCs can only send (`op: put`). Why: the same rule as the relay (only admins can request files from a PC).
- What: the sender falls back to the relay only when there is no route (hub 409) or the PC does not answer in 5 s. A refusal by the PC (bad sha256, bad ticket) is an error, not a fallback. Why: a refusal means something is wrong, and a silent second try through the hub would hide it.
- What: size limit `max_transfer_mb` (default 200), checked from Content-Length before the body is read. sha256 checked by the hub and again by the receiver. Sender and receiver must be approved PCs, or the sender is an admin. Rejected: checks on the receiver only.
- What: admin MCP tools `send_file`, `fetch_file`, `list_transfers`. `fetch_file` saves to `~\a2a-inbox` by default and checks the sha256 again before it keeps the file. Why: the admin side must refuse a damaged file too. Rejected: saving first and checking after (a damaged file would stay on disk).
- What: `fetch_file` may read any path the colleague's Windows user can read (A11). Why: admins already run full-permission Claude tasks on that PC, so a path limit adds no real protection. Rejected: only the work folder.

## Feature 3: signed updates

- What: Ed25519 written in pure Python (RFC 8032 reference). Why: the project rule is standard library only. Rejected: the `cryptography` package, and HMAC (every PC would hold the secret and could forge releases). Checked against RFC 8032 §7.1 TEST 1-3.
- What: PCs pin the hub public key (`update_pubkey`) at enroll. `TeamA2A-Setup.exe` writes it; `hub.py add-pc` prints it. Why: no other trusted channel exists today. Rejected: fetching the key on every check, which a fake hub could replace. Needs a human check (trust on first use).
- What: `hub.py release` also packs `*.exe` from the source folder (never `*.json`). Why: every real PC runs `TeamA2A-Agent.exe`, and a `.py`-only package is `wrong_kind` for it, so without this no installed PC could ever update. Rejected: a separate exe release command. The exe swap itself (rename the running exe, start the new one) is not tested on a real install → human check.
- What: a member name with a path (`..\evil.py`) in a signed manifest is refused like a bad signature. Why: even a release signed with the right key must not write outside the install folder.

## Feature 4: SSH

- What: Windows' built-in OpenSSH Server. Rejected: an SSH server in pure Python (much more code, much more risk).
- What: one keys file, `%ProgramData%\ssh\administrators_authorized_keys`, set as the global `AuthorizedKeysFile`, writable only by SYSTEM and Administrators. Why: the colleague cannot add keys or remove the forced command. Rejected: `~\.ssh\authorized_keys` (the colleague owns it).
- What: every key line has `from="<hub CIDR>"`, `command="<launcher> --requester NAME"` and `no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty`. `sshd_config` also turns off passwords, keyboard-interactive, forwarding, tunnels, TTY and the SFTP subsystem. Why: SSH can only run one logged cmd command, from the office network, with a key.
- What: the forced command is `%ProgramData%\TeamA2A\ssh\teama2a-ssh.cmd`, a path with no spaces or quotes. It calls `TeamA2A-SSH.exe ... shell`. Why: sshd hands the command to `cmd.exe /c`, which mangles commands that start with a quoted path; a plain path needs no quotes. Rejected: quoting the agent path inside `command="..."`.
- What: a separate console exe `TeamA2A-SSH.exe` (built from `receiver/a2a_ssh.py`). Why: `TeamA2A-Agent.exe` is a windowed exe, which gives no reliable stdout/stderr to sshd. Rejected: reusing the agent exe. Deviation from the plan: the forced command lives in `a2a_ssh.py` (`shell`) instead of a separate `a2a_ssh_shell.py`, so one exe holds it all. `receiver.py ssh ...` gives the same commands for script installs.
- What: the SSH exe, `ssh.json` (PC name, hub URL, PC token) and the launcher are copied to `%ProgramData%\TeamA2A\ssh` (SYSTEM + Administrators write, Users read). The SYSTEM task "TeamA2A SSH key sync" runs that copy every 15 minutes. Why: a SYSTEM task must never run a file the colleague can change (that would give them SYSTEM rights), and must not read a hub URL the colleague can change. Rejected: running the agent from `%LOCALAPPDATA%` as SYSTEM.
- What: key sync fails closed. The hub says 400/401/403/404 → empty keys file. Network error → keep the file. The file is rewritten in place to keep its ACL. Only keys of current admins (admin tokens and PCs with role admin) are served. Why: an admin who is removed, or a PC that is removed, loses SSH at the next sync.
- What: the hub stores the PC's last IP (`pcs_seen.ip`, from the poll) and `POST /api/ssh/target` gives it to the admin MCP, with the audit event `ssh_request`. The PC sends `ssh_start` / `ssh_end` / `ssh_refused` to the hub (`ssh_command` audit event) and logs them in `%LOCALAPPDATA%\TeamA2A\logs\ssh_commands.jsonl`. Why: two independent records of every command.
- What: `ssh_run` uses `-T`, `BatchMode=yes`, publickey only, `StrictHostKeyChecking=accept-new`, `IdentitiesOnly=yes`, `ClearAllForwardings=yes`, and `--` before the host. A host or user that starts with `-` is refused. Why: no password prompt can hang the MCP, and no value can turn into an ssh option.
- Not tested here (no admin rights, no sshd): `setup_ssh.ps1` end to end, the firewall rule, DefaultShell, and a real login. The script parses without errors and has every part (test T16).
