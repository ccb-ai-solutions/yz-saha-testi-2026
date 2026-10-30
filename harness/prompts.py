"""Prompt templates live in prompts/tr/*.md so they are versioned, hashed and publishable."""
from __future__ import annotations

from functools import lru_cache

from .config import PROMPT_DIR


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8").strip()


def render(template: str, **kw) -> str:
    """Replace {key} placeholders only for the given keys (JSON braces elsewhere stay untouched)."""
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out
