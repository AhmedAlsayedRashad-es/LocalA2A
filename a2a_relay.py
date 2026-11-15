"""Ping-pong between two agents for N turns.

Usage: python a2a_relay.py --a localhost --b pc-b --turns 3 "start prompt"
"""
import argparse

from a2a_common import load_config, send_prompt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="localhost")
    ap.add_argument("--b", required=True)
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("prompt", nargs="+")
    x = ap.parse_args()
    cfg = load_config()
    msg = " ".join(x.prompt)
    agents = [x.b, x.a]
    for i in range(x.turns * 2):
        who = agents[i % 2]
        r = send_prompt(cfg, who, msg)
        if not r.get("ok"):
            print(f"ERROR from {who}: {r.get('error')}")
            return
        msg = r["output"]
        print(f"\n===== turn {i + 1}: {who} =====\n{msg}")


if __name__ == "__main__":
    main()
