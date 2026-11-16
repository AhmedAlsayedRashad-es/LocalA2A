"""Small helpers shared by the hub and the receiver. Standard library only."""
import hashlib
import json

_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


def safe_name(name):
    """Return name when it is a plain, safe Windows file name; else raise ValueError."""
    if not isinstance(name, str) or not name:
        raise ValueError("file name is empty")
    if name in (".", ".."):
        raise ValueError(f"bad file name '{name}'")
    if len(name) > 200:
        raise ValueError("file name is longer than 200 characters")
    for ch in name:
        if ch in '/\\:' or ord(ch) < 32 or ord(ch) == 127:
            raise ValueError(f"bad character in file name '{name}'")
        if ch in '<>"|?*':
            raise ValueError(f"bad character in file name '{name}'")
    if name[-1] in ". ":
        raise ValueError(f"file name ends with a dot or space: '{name}'")
    if name.split(".")[0].strip().upper() in _RESERVED:
        raise ValueError(f"reserved Windows file name '{name}'")
    return name


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def version_tuple(v):
    """'1.2.3' -> (1, 2, 3). ValueError when a part is not a plain number."""
    parts = str(v).strip().split(".")
    if not parts or any(not (x.isascii() and x.isdigit()) for x in parts):
        raise ValueError(f"bad version '{v}' (use numbers like 1.2.3)")
    return tuple(int(x) for x in parts)


def canonical(obj):
    """Stable bytes of a JSON object, for signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
