# Handoff: four features, done

Date: 2026-11-16. Branch: `feature/parallel-ftp-update-ssh` (from `main` 77594c3). Not merged into `main`.
Plan: [PLAN-FEATURES.md](PLAN-FEATURES.md). Decisions and test evidence: [DECISIONS.md](DECISIONS.md).

## Status

| Plan step | State | Evidence |
|---|---|---|
| 1A-0 to 1A-8 | done | first run; feature 1 tested then |
| 1A-9 admin MCP `send_file`, `fetch_file`, `list_transfers` | done | `test_transfer` X9, `test_e2e` test_14 |
| 1A-10 `tests/test_transfer.py` | done | 11/11 OK |
| 1A-11/12 F3 hub + receiver | done, tested | `test_update` 12/12 OK |
| 1A-13 `client/setup.py` pins `update_pubkey`; `build.py --paths receiver` | done | `test_installers` 2/2 OK on a fresh build |
| 1A-14 `tests/test_sign.py`, `tests/test_update.py` | done | 3/3 and 12/12 OK |
| 1A-15 README | done | sections for all four features |
| Plan 4.1 feature 4 (SSH) | done | `test_ssh` 15/15 OK |
| 1A-16 full run + DECISIONS summary | done | 70 tests OK |

## Run the tests

`env -u A2A_IN_TASK python -m unittest discover -s tests -v` (bash), about 4 minutes.
`test_installers` uses the exes in `build\work\bin`; build first with `build\build.py`, else it is skipped.

## Human checks before you merge

1. File transfer goes through the hub, not straight from PC to PC. Approve this, or ask for a direct mode.
2. Updates: PCs trust the hub's update key the first time they join. Then make one real release
   (`hub.py release`, with `TeamA2A-Agent.exe` in the source folder) and watch one installed PC update and restart.
   PCs installed before this branch have no `update_pubkey`; run the new `TeamA2A-Setup.exe` on them once
   (it keeps the membership and pins the key from `GET /api/update/key`).
3. SSH: on one PC, run `setup_ssh.ps1` elevated (README, section SSH), set `hub.py ssh-cidr` and
   `hub.py ssh-add-key`, then try `ssh_run` from an admin PC. Check the firewall rule and the log file.
4. The hub on this PC still runs the old version. Install the new `dist\TeamA2A-Hub-Setup.exe` (or restart
   `hub.py` from this branch) to get the new endpoints.

## Things to watch

- `hub/hub.json`, `receiver/receiver.json` and `config.json` are tracked and hold real tokens. Tests use temp configs.
- The hub signing key lives in `<data_dir>/update_signing_key.json`. Never commit it or serve it.
- `dist\` and `build\work\` are not in git. The old installers are in `dist\previous-2026-11-12\`.
