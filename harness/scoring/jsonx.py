"""Robust JSON extraction from model output (code fences, leading prose, trailing commentary)."""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"```(?:json|JSON|csv)?\s*\n(.*?)\n?```", re.S)


def strip_fences(text: str) -> str:
    t = text.strip()
    m = _FENCE.search(t)
    if m and (t.startswith("```") or t.count("```") == 2):
        return m.group(1).strip()
    return t


def parse_json_strict(text: str) -> tuple[bool, Any]:
    """Whole answer (minus fences) must be JSON."""
    try:
        return True, json.loads(strip_fences(text))
    except (json.JSONDecodeError, ValueError):
        return False, None


def parse_json_lenient(text: str) -> tuple[bool, Any]:
    """First balanced JSON object/array anywhere in the text."""
    ok, v = parse_json_strict(text)
    if ok:
        return ok, v
    t = text
    for opener, closer in (("{", "}"), ("[", "]")):
        start = t.find(opener)
        while start != -1:
            depth, in_str, esc = 0, False, False
            for i in range(start, len(t)):
                ch = t[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return True, json.loads(t[start:i + 1])
                        except json.JSONDecodeError:
                            break
            start = t.find(opener, start + 1)
    return False, None


def get_path(obj: Any, path: str) -> tuple[bool, Any]:
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur
