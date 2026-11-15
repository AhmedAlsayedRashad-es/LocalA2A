# Team A2A — plan (draft 1, 2026-11-11)

Admins' Claude sends work to colleagues' Claude on the office LAN. Most work runs after hours.

## Decisions from the interview

| Topic | Decision |
|---|---|
| Work types | Diagnose, fix setup/config, Revit/CAD work (FirstOption MCPs), code/files, collect info, share skills/files |
| Approval | Fully automatic. No popup, no toast. |
| Permissions on colleague PC | Everything (`--dangerously-skip-permissions`) |
| Safety parts | Audit log only |
| Network | One office LAN |
| Senders | A few admins |
| Flow | Conversation (threads, back and forth) |
| Targets | One PC or a group (broadcast) |
| Hub host | Ahmed's PC |
| Receiver uptime | Colleagues lock the PC (Win+L), do not log off, no sleep. Receiver runs in their session. |
| Admin tools | MCP server in the admin's Claude + web dashboard |
| Results | Morning report |
| Protocol | Simple custom HTTP/JSON, with A2A v1.0 names (task, contextId, status) so we can move to `a2a-sdk` later |

## Parts

1. **Hub** (`hub/`, runs on Ahmed's PC, port 8765)
   - Registry of PCs: name, user, groups, last seen, busy.
   - Task queue and threads (SQLite file).
   - Audit log of every message, in SQLite and `logs/audit.jsonl`.
   - Web dashboard: PCs online, threads, results, morning report.
   - One token per admin and one per PC. The hub refuses unknown tokens.

2. **Receiver** (`receiver/`, on every colleague PC)
   - Starts at Windows login (Task Scheduler, hidden). Restarts if it stops.
   - Connects OUT to the hub and polls for tasks. No firewall change on colleague PCs.
   - Runs `claude -p --output-format json --dangerously-skip-permissions` in the colleague's account, so their seat and their MCPs (Revit, AutoCAD, Civil 3D) are used.
   - Keeps the thread: the first turn saves the Claude `session_id`; next turns use `--resume <session_id>`.
   - One task at a time per PC. A timeout per task.
   - Sends the answer and a local log line back to the hub.

3. **Admin MCP server** (`admin_mcp/`, in each admin's Claude desktop app)
   - Tools: `list_pcs`, `send` (to a PC or group, new thread or reply), `get_thread`, `list_tasks`, `morning_report`, `cancel`.

4. **Morning report**
   - The hub builds one summary of all night tasks: done, failed, waiting for an answer.
   - Questions that colleagues' Claude asked at night wait in the thread until an admin replies.

## Build order

1. Hub with registry, queue, tokens, audit log (no UI).
2. Receiver with `claude -p` and `--resume`; test on 2 PCs (this PC + pc-b).
3. Admin MCP server; test a conversation and a group broadcast.
4. Morning report.
5. Web dashboard.
6. Installer for colleague PCs (copy, config, Task Scheduler entry, power settings).

## Requirements on colleague PCs

- Claude Code CLI installed and signed in to the colleague's Team seat (IT does this).
- Python 3.8+.
- Power plan: no sleep. Users lock, do not log off.

## Open points

- Night tasks use the colleague's seat usage limits.
- Ahmed's PC must stay on overnight, because it hosts the hub.
- Colleagues and IT must know and agree, because tasks run on their PCs with full rights and no notice.
