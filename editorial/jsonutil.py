from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def parse_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from an LLM reply, stripping ```json fences."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("пустой JSON-ответ")
    raw = _FENCE.sub("", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"нет JSON-объекта: {raw[:200]}")
    blob = raw[start : end + 1]
    data = json.loads(blob)
    if not isinstance(data, dict):
        raise ValueError("ожидался JSON-объект")
    return data
