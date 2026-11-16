"""Stand-in for `claude -p --output-format json` used by the tests.

The message (stdin) picks the behaviour:
  SLEEP n     sleep n seconds, then answer (default echo, with the turn number)
  CWD         answer 'CWD: <current folder>'
  BIG n       answer with n Arabic letters
  ASK         answer with 'QUESTION: ...'
  FAIL        JSON result with is_error=true
  EXIT        exit code 3, no JSON
  default     echo; reports the turn number in this session and whether a system prompt came
With --resume SID it continues the session (history in FAKE_CLAUDE_DIR); an unknown SID fails
like the real CLI ("No conversation found").
"""
import json
import os
import sys
import time
import uuid

args = sys.argv[1:]
msg = sys.stdin.buffer.read().decode("utf-8")
store = os.environ.get("FAKE_CLAUDE_DIR") or os.path.join(os.path.dirname(__file__), "_fake_sessions")
os.makedirs(store, exist_ok=True)

sid = args[args.index("--resume") + 1] if "--resume" in args else None
sp = ""
if "--append-system-prompt-file" in args:
    sp = open(args[args.index("--append-system-prompt-file") + 1], encoding="utf-8").read()
if sid:
    path = os.path.join(store, sid + ".json")
    if not os.path.exists(path):
        sys.stderr.write(f"No conversation found with session ID: {sid}\n")
        sys.exit(1)
    hist = json.load(open(path, encoding="utf-8"))
else:
    sid = str(uuid.uuid4())
    hist = []
hist.append(msg)
json.dump(hist, open(os.path.join(store, sid + ".json"), "w", encoding="utf-8"))

first = msg.strip().split()[0] if msg.strip() else ""
if first == "SLEEP":
    time.sleep(float(msg.split()[1]))
if first == "EXIT":
    sys.stderr.write("boom\n")
    sys.exit(3)
out = {"type": "result", "subtype": "success", "is_error": False, "session_id": sid,
       "total_cost_usd": 0.001, "cwd": os.getcwd()}
if first == "BIG":
    out["result"] = "م" * int(msg.split()[1])  # Arabic letter, n times
elif first == "ASK":
    out["result"] = "QUESTION: which drive should I clean, C or D?"
elif first == "CWD":
    out["result"] = "CWD: " + os.getcwd()
elif first == "FAIL":
    out["is_error"] = True
    out["result"] = "API Error: something broke"
else:
    out["result"] = (f"ECHO: {msg.strip()} | turn={len(hist)} | sp={'yes' if 'Team A2A' in sp else 'no'}"
                     f" | admin_in_sp={'yes' if 'admin' in sp else 'no'}")
print("some log line that is not JSON")
# Like `--output-format stream-json --verbose`: the plan limits come before the result line.
print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
    "status": "allowed", "resetsAt": 4102444800, "rateLimitType": "five_hour", "unifiedWindows": {
        "five_hour": {"utilization": 0.37, "resetsAt": 4102444800, "status": "allowed"},
        "seven_day": {"utilization": 0.05, "resetsAt": 4103049600, "status": "allowed"}}},
    "session_id": sid}))
print(json.dumps(out))
