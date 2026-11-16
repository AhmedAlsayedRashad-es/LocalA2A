# Plan: four features (parallel sessions, file transfer, signed updates, SSH)

Branch: `feature/parallel-ftp-update-ssh` (from `main` 77594c3). Written 2026-11-15/16 by Claude Opus 5.5.
Two plans: **Plan 1A** (features 1-3) and **Plan 4.1** (feature 4). They were planned separately
because the first single planner run was cut off by a safety classifier.
Status of each step: see [HANDOFF.md](HANDOFF.md).

Rules for all work: Python 3.8+, standard library only. Never commit `hub/hub.json` or `receiver/receiver.json`
(they hold real tokens) or any private key. Tests use temp configs only. Every decision goes to
[DECISIONS.md](DECISIONS.md) (what / why / rejected alternative).

Test command on Windows (see HANDOFF.md for why):
`env -u A2A_IN_TASK python -m unittest discover -s tests -v` (bash), or one file with `-p "test_x.py"`.

---

## Plan 1A: features 1-3

### Assumptions
- A1 Local branch, commits only to `feature/parallel-ftp-update-ssh`. No release.
- A3 `test_14` (exact MCP tool set) = old 7 + `send_file`, `fetch_file`, `list_transfers` + `ssh_run`.
- A4 New keys read with `cfg.get(key, default)`. Receiver: `max_parallel=3`, `auto_update=true`, `update_check_s=3600`, `update_pubkey=''`. Hub: `max_transfer_mb=200`, `transfer_ttl_h=24`.
- A5 Backward-compatible poll: no `slots` → old behaviour (1 task, `task` field, any task still `running` fails as lost). With `slots` → `tasks` list + `task` (first); receiver also sends `running` ids; only running tasks missing from that list fail as lost.
- A6 Per-thread folder `<workdir>/a2a-sessions/<context_id>` (claude `--resume` is keyed to the cwd). Threads from before the upgrade fall back to the existing "could not be loaded" path.
- A7 Receiver VERSION 1.1 → 1.2. Releases are dotted integers. `hub.py release` refuses when the packaged `receiver.py` VERSION differs (prevents an update loop).
- A8 Update package = flat file set replacing files in the install dir. Script install needs `receiver.py`; frozen exe install needs its exe. Frozen updates untested → human check.
- A9 After an update the receiver starts a detached copy of itself and exits 0. Tests replace the restart callable.
- A10 No `update_pubkey` → never installs updates. Pubkey written at enroll by `client/setup.py` and printed by `hub.py add-pc` (trust on first use) → decision + human check.
- A11 `fetch_file` may read any path on the PC (admins already run full-permission Claude tasks).
- A12 Hub imports `receiver/a2a_sign.py` and `receiver/a2a_util.py` via `../receiver` on `sys.path`; `build.py` gets `--paths receiver` for the hub exe (human check).

### Steps
0. Baseline + branch: record git state, SHA-256 of `hub/hub.json` and `receiver/receiver.json`, baseline tests; `git checkout -b feature/parallel-ftp-update-ssh`.
1. `DECISIONS.md` skeleton: Summary (filled at the end), Run log, one section per feature.
2. Shared modules. `receiver/a2a_sign.py`: Ed25519 (RFC 8032 §6 reference, `hashlib.sha512`): `generate_seed()`, `public_key(seed)`, `sign(seed,msg)`, `verify(pub,msg,sig)` (False on bad input, never raises). `receiver/a2a_util.py`: `safe_name()` (rejects empty, `.`, `..`, `/ \ :`, control chars, >200 chars, trailing dot/space, CON/PRN/AUX/NUL/COM1-9/LPT1-9), `sha256_file()`, `version_tuple()`, `canonical()` (sorted compact JSON bytes).
3. F1 hub: `next_task(pc, wait, slots=None, running=None)` / `_claim()`: up to `slots` tasks, never two of one context at once, thread turns in order, audit `start` each. Long-poll also wakes for files / file requests. `/api/worker/next` returns `task`, `tasks` (only when slots given), `role`, `files`, `file_requests`, `update`. `pcs()` adds `running` and `running_tasks`; `busy_task` = first running id.
4. F1 receiver: `max_parallel`, `self.running` under a lock, poll with free slots, one thread per task, cwd per context, per-task system-prompt file `logs/run/<task_id>.sp.txt`, thread-safe `log()`, VERSION 1.2.
5. F1 extras: `fake_claude.py` `CWD` mode; dashboard shows `(+N)`; `list_pcs` shows `running=N`.
6. `tests/test_parallel.py` P1-P8.
7. F2 hub: table `transfers` (src/dst `pc:NAME` / `admin:NAME`, status requested|offered_req|stored|offered|delivered|failed|expired). Files in `<data_dir>/transfers/<id>.bin`. Streaming routes: `POST /api/files/send?to=&name=` (admin), `POST /api/worker/files/send?to=&name=` (PC), `POST /api/worker/files/<id>/upload` (PC answers a request). Checks in order: auth; source approved (403); destination exists and approved (400); `safe_name` (400); `X-A2A-SHA256` present; no Content-Length 411; Content-Length > limit 413 without reading the body. Stream in 64 KiB chunks while hashing; mismatch → 400, delete, audit `transfer_rejected`. Poll offers stored files and requests (re-offer after 300 s). `GET /api/worker/files/<id>` (destination PC only, headers `X-A2A-SHA256`, `X-A2A-Name`); `POST /api/worker/files/<id>/ack`. Admin: `POST /api/files/request {pc,path}`, `GET /api/files/<id>/download`, `GET /api/files`, `GET /api/files/<id>`. `sweep()` expires transfers older than `transfer_ttl_h`.
8. F2 receiver + CLI: download into `<workdir>/a2a-inbox/<name>.part`, check sha256 + size, rename to a unique name (`name (2).ext`), ack. Answer file requests by uploading the path. CLI `receiver.py send-file --to PC PATH`. One system-prompt sentence about `send-file`.
9. F2 admin MCP: tools `send_file{to,path,wait_seconds}`, `fetch_file{pc,path,save_to?,wait_seconds}`, `list_transfers{limit}`; update `test_14` set.
10. `tests/test_transfer.py` X1-X11 (hub with `max_transfer_mb=1`; PCs pa, pb approved with receivers, poff approved without receiver, pp pending).
11. F3 hub: key `<data_dir>/update_signing_key.json` (never served). `hub.py update-key` prints the pubkey; `add-pc` output and enroll response include `update_pubkey`. `hub.py release --version X [--source DIR]`: zip the `*.py` files of DIR (never `*.json`), check VERSION, manifest `{version, sha256, size, files{name:sha256}, created}`, signature over `canonical(manifest)`, store `<data_dir>/releases/X/{package.zip, release.json}`, audit. `GET /api/update/manifest`, `GET /api/update/package?version=X` (approved PC or admin token; none 401; pending 403). Poll response `update={version}` when newer than the PC's version.
12. F3 receiver: `check_update()` → `no_key|none|not_newer|bad_signature|sha256_mismatch|wrong_kind|installed|error:...`. Pinned key, verify signature, version strictly greater, stop taking tasks and wait for running ones, download to `update_staging`, check size/sha256 and every zip member, back up to `update_backup/<old>/`, swap files (`.old` + `os.replace`, roll back on error), restart. Triggered at start, every `update_check_s`, and when a poll carries `update`. `auto_update=false` turns it off.
13. `client/setup.py` writes `update_pubkey` from the enroll response; `build/build.py` adds `--paths receiver` for the hub exe.
14. `tests/test_sign.py` (S1-S3) and `tests/test_update.py` (U1-U10).
15. README sections for the three features and the new config keys.
16. Run all tests, fix, fill the DECISIONS.md summary (claim only what tests prove).
17. Commit (after Plan 4.1). Explicit paths only.

### Tests (written before the work)
- T0 DECISIONS.md run log: baseline, branch, models. T1 branch name. T2 full suite OK (only `test_installers` skipped). T3 `test_e2e` 20/20 with updated `test_14`.
- P1 two `SLEEP 3` tasks on a max-3 PC finish in < 5 s and overlap. P2 same thread: turn 2 starts after turn 1 ends, output `turn=2`. P3 max-1 PC: no overlap, ≥ 4 s. P4 `CWD`: path is `<wd>/a2a-sessions/<context_id>`, differs per thread, same for a follow-up. P5 old protocol (no slots): one task, no `tasks` key, lost task failed on next poll. P6 4 tasks: running never > 3, all done. P7 `/api/pcs` has `running`, `running_tasks`, `busy_task`. P8 cancel one of two parallel tasks; the other keeps running.
- X1 100 KB admin→pa delivered with the same sha256, stored copy removed. X2 Content-Length over limit → 413 without the body; no length → 411. X3 wrong sha256 → 400 + audit `transfer_rejected`. X4 corrupted stored file → receiver acks failure, `sha256` in error, nothing in inbox. X5 pending PC sends → 403; send to pending → 400; unknown PC → 400; PC token on admin route → 401. X6 bad names (`../x.txt`, `..\x`, `CON`, empty) → 400; duplicate name → `name (2).ext`. X7 `receiver.py send-file --to pb` → in pb inbox, audit src `pc:pa` dst `pc:pb`. X8 admin requests a file from pa → bytes and sha256 equal; missing path → failed. X9 MCP `send_file` / `fetch_file` over stdio. X10 `sweep()` expires a 25 h old transfer and deletes the file. X11 audit has `transfer_upload`, `transfer_delivered`, `transfer_rejected`.
- S1 RFC 8032 §7.1 TEST 1-3 exact. S2 verify False (no exception) for changed message, flipped bit, wrong key, 63-byte signature. S3 stdlib-only imports.
- U1 `release --version 1.9.0` writes a zip + signed release.json without `receiver.json`. U2 VERSION mismatch / `abc` → refused, no release dir. U3 key only in data_dir, seed in no HTTP response. U4 manifest/package auth (PC 200, none 401, pending 403), package sha256 = manifest. U5 poll with 1.2 → `update.version=1.9.0`; with 1.9.0 → null. U6 good install into a temp dir: VERSION changed, backup made, restart called once. U7 tamper: altered signature, other key, flipped package byte, downgrade, no key, package without `receiver.py` → `bad_signature, bad_signature, sha256_mismatch, not_newer, no_key, wrong_kind`; files unchanged each time. U8 running task finishes before the update installs. U9 enroll response and `add-pc` output contain `update_pubkey`. U10 `py_compile client/setup.py build/build.py`.
- D1 DECISIONS.md structure. D2 README mentions `max_parallel`, `max_transfer_mb`, `send-file`, `hub.py release`, `update_pubkey`. C1 commit message ends with the Co-Authored-By line; `main` unchanged.

---

## Plan 4.1: feature 4 (SSH to the cmd shell of approved PCs)

### Assumptions
- Windows' built-in **OpenSSH Server** (rejected: an SSH implementation in pure Python).
- One central keys file `%ProgramData%\ssh\administrators_authorized_keys` (SYSTEM + Administrators only); `sshd_config` sets a global `AuthorizedKeysFile` to it, so the colleague cannot add keys or change the forced command (rejected: `~/.ssh/authorized_keys`).
- The receiver runs as a limited user, so `setup_ssh.ps1` registers a SYSTEM scheduled task "TeamA2A SSH key sync" (`a2a_ssh.py sync` at startup and every 15 min).
- Sync fails closed: hub 400/401/403/404 → empty keys file; network error → keep the current file; write in place to keep the ACL.
- Key validation on hub and on PC: `ssh-ed25519`, `ecdsa-sha2-nistp256/384/521`, `ssh-rsa`; line `^TYPE BASE64( COMMENT)?$`; base64 decodes and its first string equals TYPE; options, quotes, commas, newlines rejected. Names `[A-Za-z0-9._-]{1,64}`.
- Wrapper runs `cmd.exe /d /s /c "<command>"`; output and exit code pass through. Log `<dir of receiver.json>/logs/ssh_commands.jsonl`.
- Hub audit events: `ssh_command` (PC, start/end), `ssh_request` (admin, from `ssh_run`), `ssh_keys` (PC sync). PC's last IP stored in a new `pcs_seen.ip` column.
- `ssh_run` key: env `A2A_SSH_KEY`, else `~/.ssh/id_ed25519`; ssh: env `A2A_SSH_EXE`, `which ssh`, `%WINDIR%\System32\OpenSSH\ssh.exe`; timeout 60 (1..3600). Hub refuses (403) admins without a key.
- Real sshd login, firewall rule and DefaultShell cannot be tested here (no sshd, no admin rights) → human checks.

### Steps
1. Record hashes of files that must stay unchanged.
2. `receiver/a2a_ssh.py`: `validate_pubkey`, `render_sshd_config(user, cidr)` (PubkeyAuthentication yes, PasswordAuthentication no, KbdInteractiveAuthentication no, ChallengeResponseAuthentication no, PermitEmptyPasswords no, AuthenticationMethods publickey, AllowUsers, AllowTcpForwarding/AgentForwarding/X11Forwarding/PermitTunnel/GatewayPorts/PermitTTY/PermitUserEnvironment no, StrictModes yes, MaxAuthTries 3, LogLevel VERBOSE, AuthorizedKeysFile `__PROGRAMDATA__/ssh/administrators_authorized_keys`, no Subsystem), `authorized_keys_line()` (`from="<cidr>",command="\"<python>\" \"<shell>\" --requester <name>",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty <key>`), `render_authorized_keys()`, `sync_authorized_keys()` (POST `/api/worker/ssh_keys`). CLI `render-config`, `sync`, `hub-cidr`.
3. `receiver/a2a_ssh_shell.py` forced command: reads `SSH_ORIGINAL_COMMAND`, `SSH_CLIENT`; logs `ssh_start` / `ssh_end` (id, pc, time, requester, command, client_ip, exit_code, duration_s) locally and to the hub (best effort); empty command → refuse (`ssh_refused`), exit 1; else run via cmd and exit with its code.
4. Hub config: `ssh_keys {}`, `ssh_allowed_cidr ''`; reload/mutate copy them; CLI `ssh-add-key NAME "pubkey"`, `ssh-remove-key NAME`, `ssh-cidr CIDR`.
5. Hub: `pcs_seen.ip` (guarded ALTER TABLE); `POST /api/worker/ssh_keys` (403 pending, 400 no CIDR, else keys of current admins); `POST /api/worker/ssh_log` (audit `ssh_command`); admin `POST /api/ssh/target {pc, command}` (400 unknown, 403 pending, 403 no key, 409 no IP; else audit `ssh_request`, return ip + user).
6. Admin MCP: pure `ssh_args()` (`-T`, `BatchMode=yes`, `PasswordAuthentication=no`, `KbdInteractiveAuthentication=no`, `PreferredAuthentications=publickey`, `StrictHostKeyChecking=accept-new`, `IdentitiesOnly=yes`, `ConnectTimeout`, `-i key`, `-l user`, `--`, host, command; ValueError on host starting with `-`); tool `ssh_run{pc, command, timeout}` (refused inside a remote task).
7. `receiver/setup_ssh.ps1` (run elevated once by IT): install OpenSSH.Server, back up and write `sshd_config`, `sshd -t`, DefaultShell = cmd.exe, disable the default firewall rule and add `TeamA2A-SSH` (TCP 22, `-RemoteAddress` hub CIDR), first sync, `icacls` the keys file, SYSTEM sync task, start sshd.
8. `test_14` set += `ssh_run`.
9. `tests/test_ssh.py` T2-T16 against a real temp hub.
10. Run tests. 11. DECISIONS.md feature 4 section + summary. 12. Check unchanged files.

### Tests
- T2 sshd_config has the key-only lines, `AllowUsers alice`, no `PasswordAuthentication yes`; bad user / CIDR → ValueError. T3 authorized_keys line format. T4 `validate_pubkey` rejects option injection, a second line, `ssh-dss`, bad base64, wrong type string, bad requester; accepts ed25519 / ecdsa-p256 / rsa. T5 wrapper runs `echo hi`, exit 0, log start+end with pc/time/requester/command/client_ip/exit_code/duration. T6 hub audit `ssh_command` both phases. T7 `exit /b 3` → exit 3 logged. T8 no command → refused, `ssh_refused`, no `ssh_start`. T9 `/api/worker/ssh_keys` pending 403; approved gets exactly the admins with keys. T10 sync writes one line for an approved PC, empty file for a pending one. T11 `hub.py ssh-add-key` valid → stored; bad → non-zero, file unchanged; unknown admin → non-zero. T12 `/api/ssh/target` → ip 127.0.0.1, user, audit `ssh_request`. T13 pending 403, unknown 400, admin without key 403. T14 `ssh_args` options; `-oProxyCommand=x` host → ValueError. T15 MCP `ssh_run` refusals; inside a task no tools. T16 `setup_ssh.ps1` parses, has the key parts. T17 `test_14`. T18 stdlib only. T19 DECISIONS.md feature 4 entries.
