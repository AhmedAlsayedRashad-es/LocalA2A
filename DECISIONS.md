# Summary

Work in progress, stopped on the admin's request (2026-11-16). See HANDOFF.md for the status of each step.

- Works (tests pass): feature 1, parallel sessions. `tests/test_parallel.py` 7/7 OK, and `tests/test_e2e.py` is still 20/20 OK.
- Coded but not tested, so NOT claimed as working: feature 2 (file transfer: hub and receiver parts; the admin MCP tools are missing) and feature 3 (signed updates: hub and receiver parts).
- Not started: feature 4 (SSH). The plan is in PLAN-FEATURES.md.
- Needs a human check: hub-relayed transfer instead of literal peer-to-peer; trust on first use for the update key; executor effort was medium, not high.

## Run log

- Branch: `feature/parallel-ftp-update-ssh` (from `main` 77594c391a2021b3bf9ffac9e49318841747f7c3).
- Baseline on main: `test_e2e` 20 tests OK in about 14 s, with `A2A_IN_TASK` unset. With `A2A_IN_TASK=1` (this session runs inside a Team A2A task), test_14 fails. That is an environment artefact, not a code bug.
- README test command `python -m unittest tests.test_e2e` fails on this PC: a `tests` package in site-packages shadows the repo folder. Use `python -m unittest discover -s tests`.
- Models: all Claude Opus 5.5 (`claude-opus-5-5`). Orchestrator = main session (effort level not visible from inside the session). Planners: Opus 5.5, high. Executor: Opus 5.5, medium (from the CLAUDE.md team table; the admin asked for high). Verifiers: not run yet.
- Planning: one planner run was cut off by a safety classifier, so features 1-3 and feature 4 were planned separately (PLAN-FEATURES.md).

## Feature 1: parallel sessions per PC

- What: per-thread folder `<workdir>/a2a-sessions/<context_id>`. Why: `claude --resume` finds a session by its working folder, so every turn of a thread must run in the same folder, and different threads must not share one. Rejected: a folder per task, which would break `--resume`.
- What: hub never runs two tasks of the same thread at once, and later turns stay queued in order. Why: two turns would write to the same Claude session at once. Rejected: free parallelism per task.
- What: old receivers (no `slots` in the poll) still get one task at a time. Why: PCs update at different times. Rejected: a protocol break that forces all PCs to update together.
- What: default `max_parallel` 3, and 1 gives the old behaviour. Why: several tasks at once without overloading a colleague PC or their Claude seat limits. Rejected: no limit.

## Feature 2: file transfer

- What: transfers go through the hub (upload, store, receiver downloads on its poll). Why: PCs only connect out to the hub today, with no firewall change on colleague PCs. Rejected: literal peer-to-peer, which needs an inbound port on every PC. Needs the admin's OK.
- What: size limit `max_transfer_mb` (default 200), checked from Content-Length before the body is read. sha256 checked by the hub and again by the receiver. Sender and receiver must be approved PCs, or the sender is an admin. Rejected: checks on the receiver only.

## Feature 3: signed updates

- What: Ed25519 written in pure Python (RFC 8032 reference). Why: the project rule is standard library only. Rejected: the `cryptography` package, and HMAC (every PC would hold the secret and could forge releases).
- What: PCs pin the hub public key (`update_pubkey`) at enroll. Why: no other trusted channel exists today. Rejected: fetching the key on every check, which a fake hub could replace. Needs a human check (trust on first use).

## Feature 4: SSH
