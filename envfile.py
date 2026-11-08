"""Tiny .env reader (standard library only) so `python audit.py serve` picks up settings the same way docker compose does.
Lines look like KEY=value; blank lines and lines starting with # are ignored; quotes around a value are removed; a
trailing ` # comment` after an unquoted value is ignored. Variables already set in the real environment win."""
import os
from pathlib import Path


def parse(text):
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.removeprefix("export ").strip()
        val = val.strip()
        if val[:1] in ("'", '"') and val.endswith(val[0]) and len(val) >= 2:
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        if key:
            out[key] = val
    return out


def load(path):
    """Set os.environ from the file (without overriding anything already set); returns the names it set."""
    p = Path(path)
    if not p.exists():
        return []
    set_now = []
    for k, v in parse(p.read_text(encoding="utf-8-sig")).items():
        if v != "" and k not in os.environ:
            os.environ[k] = v
            set_now.append(k)
    return set_now
