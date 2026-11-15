"""Send a prompt to a peer's Codex and print the answer.

Usage: python a2a_send.py --to pc-b "your prompt"
"""
import argparse
import sys

from a2a_common import load_config, send_prompt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="peer name from config.json, host, or URL")
    ap.add_argument("prompt", nargs="+")
    a = ap.parse_args()
    try:
        r = send_prompt(load_config(), a.to, " ".join(a.prompt))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    if not r.get("ok"):
        print(f"ERROR: {r.get('error')}", file=sys.stderr)
        sys.exit(1)
    print(r["output"])


if __name__ == "__main__":
    main()
