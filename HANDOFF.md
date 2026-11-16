# Handoff: four features, work in progress

Date: 2026-11-16. Branch: `feature/parallel-ftp-update-ssh` (from `main` 77594c3). Not merged.
Stopped on the admin's request before the work was complete. Plan: [PLAN-FEATURES.md](PLAN-FEATURES.md).
Decisions so far: [DECISIONS.md](DECISIONS.md).

Models: everything ran on Claude Opus 5.5 (`claude-opus-5-5`). Planners at high effort. The executor
(the agent that wrote the code) ran at **medium** effort, per this PC's CLAUDE.md team table, while the
admin asked for high. The main session's effort level could not be confirmed from inside the session.

## Status per step

| Plan step | State | Evidence |
|---|---|---|
| 1A-0 baseline + branch | done | on `main`: `test_e2e` 20/20 OK (with `A2A_IN_TASK` unset) |
| 1A-1 DECISIONS.md | started (skeleton + this run's entries) | |
| 1A-2 `a2a_sign.py`, `a2a_util.py` | written | RFC 8032 TEST 1 pubkey + signature match, verify of a changed message is False (checked by hand). No test file yet (`test_sign.py` missing) |
| 1A-3/4/5 F1 parallel sessions (hub, receiver, extras) | **done, tested** | `tests/test_parallel.py` 7 tests OK (P1-P8); `test_e2e` 20/20 OK on this branch |
| 1A-6 `tests/test_parallel.py` | done | 7 tests OK |
| 1A-7 F2 hub (transfers table, streaming routes, ack, request, sweep) | written, **untested** | compiles |
| 1A-8 F2 receiver (inbox download, requests, `send-file` CLI) | written, **untested** | compiles |
| 1A-9 F2 admin MCP tools `send_file`, `fetch_file`, `list_transfers` | **not done** | |
| 1A-10 `tests/test_transfer.py` | **not done** | |
| 1A-11 F3 hub (signing key, `release`, manifest/package endpoints, `update` in poll) | written, **untested** | compiles. Check that `update-key` and `update_pubkey` in `add-pc`/enroll exist |
| 1A-12 F3 receiver (`check_update`, install, rollback, restart) | written, **untested** | compiles |
| 1A-13 `client/setup.py` writes `update_pubkey`; `build.py --paths receiver` | **not done** | |
| 1A-14 `tests/test_sign.py`, `tests/test_update.py` | **not done** | |
| 1A-15 README | **not done** | |
| 1A-16 full run + DECISIONS summary | **not done** | |
| Plan 4.1 (feature 4, SSH): all steps | **not started** | |
| 1A-17 commit | this commit = work in progress | |

So: feature 1 works (tests pass). Features 2 and 3 are coded on the hub and receiver but have no
tests and no admin MCP tools. Do not treat them as working. Feature 4 has a plan only.

## How to continue

1. `git checkout feature/parallel-ftp-update-ssh`
2. Run the tests (bash):
   `env -u A2A_IN_TASK python -m unittest discover -s tests -v`
   - `python -m unittest tests.test_e2e` (the README command) fails on this PC: a `tests` package in
     Python's site-packages shadows the repo folder. Use `discover -s tests`.
   - If you run inside a Team A2A task, `A2A_IN_TASK=1` leaks into `test_14` and makes it fail (the admin MCP
     gives no tools inside a task). Unset it, or make `test_14` drop it from the child env.
3. Continue with Plan 1A step 9 (admin MCP F2 tools; also update the `test_14` expected tool set),
   then 10, 13, 14 (tests for F2 and F3; fix what they find), 15, then Plan 4.1, then 16 and the summary.
4. Write every decision to DECISIONS.md (what / why / rejected alternative). Claim a feature works only when its
   tests pass.

## Things to watch

- `hub/hub.json` and `receiver/receiver.json` are tracked and hold real tokens. Never run the hub or receiver
  against them in tests and never commit changes to them. Tests use temp configs (`hubmod.CONFIG_PATH`).
- The hub signing key lives in `<data_dir>/update_signing_key.json`. It must never be committed or served.
- `tests/test_installers.py` is skipped here (no PyInstaller build).
- Literal peer-to-peer file transfer was rejected (needs inbound ports on every PC). Transfers go through the hub.
  The admin asked for "directly", so this needs the admin's OK.
- Release trust: PCs pin the hub's public key at enroll (trust on first use) → human check.
