# -*- coding: utf-8 -*-
"""HTML для Telegram: экранирование + цитаты `>` → <blockquote>."""

from __future__ import annotations

import html
import re


def format_post_html(text: str) -> str:
    """Экранирует пост и превращает строки `> цитата` в <blockquote>."""
    if not text:
        return ""
    lines = text.splitlines()
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        inner = html.escape("\n".join(buf))
        out.append(f"<blockquote>{inner}</blockquote>")
        buf.clear()

    for line in lines:
        if line.startswith("> "):
            buf.append(line[2:])
        elif line == ">":
            buf.append("")
        elif re.match(r"^>\S", line):
            buf.append(line[1:])
        else:
            flush()
            out.append(html.escape(line))
    flush()
    return "\n".join(out)
