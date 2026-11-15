# Agent-to-agent

You can talk to the Codex agent on the other PC. Run:

    python a2a_send.py --to pc-b "your message"

The command prints the other agent's answer. Keep messages short and self-contained;
the other agent does not see this conversation. Do not call the peer in a loop
without a stop condition.
