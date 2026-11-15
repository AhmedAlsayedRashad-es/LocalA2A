"""Shared helpers: config loading and HTTP calls to a peer."""
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("port", 8765)
    cfg.setdefault("codex_cmd", ["codex", "exec", "--skip-git-repo-check", "-"])
    cfg["workdir"] = os.path.join(HERE, cfg.get("workdir", "."))
    cfg.setdefault("timeout_s", 600)
    cfg.setdefault("peers", {})
    return cfg


def peer_url(cfg, target):
    if target in cfg["peers"]:
        return cfg["peers"][target].rstrip("/")
    if target.startswith("http"):
        return target.rstrip("/")
    return f"http://{target}:{cfg['port']}"


def send_prompt(cfg, target, prompt, timeout=None):
    url = peer_url(cfg, target) + "/prompt"
    body = json.dumps({"prompt": prompt, "from": cfg.get("name", "unknown")}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "X-A2A-Token": cfg["token"]},
    )
    with urllib.request.urlopen(req, timeout=timeout or cfg["timeout_s"] + 30) as resp:
        return json.loads(resp.read().decode("utf-8"))
