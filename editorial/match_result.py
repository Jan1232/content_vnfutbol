"""Извлечение результата матча с картинки донора (vision) и подготовка к рендеру."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.config import get_settings
from editorial.club_logos import resolve_pair
from editorial.jsonutil import parse_json_object
from editorial.openai_client import get_client, usage_scope

_EXTRACT_PROMPT = (
    "На картинке табло/графика футбольного матча с итоговым счётом.\n"
    "Извлеки данные С ИЗОБРАЖЕНИЯ (табло, гербы, подписи). В тексте поста счёт может отсутствовать.\n"
    "Если счёт или команда не читаются уверенно — поставь низкий confidence (<0.5).\n\n"
    "JSON:\n"
    "{\n"
    '  "home_team": "название домашней команды",\n'
    '  "away_team": "название гостевой команды",\n'
    '  "score_home": 0,\n'
    '  "score_away": 0,\n'
    '  "scorers_home": [{"name":"Фамилия","minute":"33"}],\n'
    '  "scorers_away": [{"name":"Фамилия","minute":"16"}],\n'
    '  "competition": "лига/турнир или пусто",\n'
    '  "stage": "тур/этап или пусто",\n'
    '  "confidence": 0.0,\n'
    '  "source": "image"\n'
    "}"
)

_SCORE_IN_TEXT = re.compile(r"(\d+)\s*[:\-–]\s*(\d+)")


def post_subtype_of(row: dict[str, Any], entities: dict[str, Any] | None = None) -> str:
    ent = entities
    if ent is None:
        try:
            ent = json.loads(row.get("entities_json") or "{}")
        except Exception:
            ent = {}
    gate = (ent or {}).get("donor_gate") or (ent or {}).get("soccerblog_gate") or {}
    if isinstance(gate, dict):
        ps = str(gate.get("post_subtype") or "").strip().lower()
        if ps:
            return ps
    if str(row.get("event_type") or "") == "match_result":
        return "match_result"
    return ""


def is_match_result_row(row: dict[str, Any], entities: dict[str, Any] | None = None) -> bool:
    settings = get_settings()
    if not bool(getattr(settings, "result_template_enabled", True)):
        return False
    ent = entities
    if ent is None:
        try:
            ent = json.loads(row.get("entities_json") or "{}")
        except Exception:
            ent = {}
    if post_subtype_of(row, ent) == "match_result":
        return True
    if str(row.get("event_type") or "") != "match_result":
        return False
    if str(row.get("media_type") or "") == "video":
        return False
    gate = (ent or {}).get("donor_gate") or (ent or {}).get("soccerblog_gate") or {}
    from editorial.soccerblog_gate import effective_gate_kind

    return effective_gate_kind(gate) == "template"


def _vision_model() -> str:
    settings = get_settings()
    return (getattr(settings, "editorial_vision_model", None) or "gpt-5.6-luna").strip()


def _reasoning_effort() -> str | None:
    effort = (getattr(get_settings(), "editorial_reasoning_effort", None) or "").strip()
    return effort or None


def _normalize_scorers(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        minute = str(row.get("minute") or "").strip().rstrip("'")
        if name:
            out.append({"name": name, "minute": minute})
    return out


def _parse_scores(data: dict[str, Any]) -> tuple[int | None, int | None]:
    try:
        sh = int(data.get("score_home"))
        sa = int(data.get("score_away"))
        if sh < 0 or sa < 0 or sh > 30 or sa > 30:
            return None, None
        return sh, sa
    except (TypeError, ValueError):
        return None, None


def _merge_text_hint(data: dict[str, Any], text: str) -> dict[str, Any]:
    out = dict(data)
    if out.get("home_team") and out.get("away_team"):
        return out
    m = _SCORE_IN_TEXT.search(text or "")
    if m and out.get("score_home") is None:
        try:
            out["score_home"] = int(m.group(1))
            out["score_away"] = int(m.group(2))
            out["source"] = "text"
        except ValueError:
            pass
    return out


def extract_match_result_from_image(image_path: str, text: str = "") -> dict[str, Any]:
    from editorial.imagery import preview_jpeg

    path = Path(image_path)
    if not path.is_file():
        raise RuntimeError("нет файла изображения донора")
    preview = preview_jpeg(path, max_side=1024)
    blob = (text or "").strip()[:1500]
    user = f"Текст поста (может не содержать счёт):\n{blob}\n\n{_EXTRACT_PROMPT}"
    model = _vision_model()
    with usage_scope(task="match_result_extract"):
        raw = get_client().vision(
            model,
            [preview],
            user,
            json_mode=True,
            max_tokens=2000,
            task="match_result_extract",
            reasoning_effort=_reasoning_effort(),
        )
    data = raw if isinstance(raw, dict) else parse_json_object(str(raw))
    data = _merge_text_hint(data, text)
    sh, sa = _parse_scores(data)
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return {
        "home_team": str(data.get("home_team") or "").strip(),
        "away_team": str(data.get("away_team") or "").strip(),
        "score_home": sh,
        "score_away": sa,
        "scorers_home": _normalize_scorers(data.get("scorers_home")),
        "scorers_away": _normalize_scorers(data.get("scorers_away")),
        "competition": str(data.get("competition") or "").strip(),
        "stage": str(data.get("stage") or "").strip(),
        "confidence": conf,
        "source": str(data.get("source") or "image").strip() or "image",
    }


def validate_match_result(
    data: dict[str, Any],
    home_logo: dict[str, Any],
    away_logo: dict[str, Any],
) -> tuple[bool, str]:
    settings = get_settings()
    min_conf = float(getattr(settings, "result_min_conf", 0.7) or 0.7)
    require_scorers = bool(getattr(settings, "result_require_scorers", True))
    logo_fallback = bool(getattr(settings, "result_logo_fallback", False))

    conf = float(data.get("confidence") or 0)
    if conf < min_conf:
        return False, f"низкий confidence ({conf:.2f} < {min_conf})"
    if not data.get("home_team") or not data.get("away_team"):
        return False, "не прочитаны команды"
    if data.get("score_home") is None or data.get("score_away") is None:
        return False, "не прочитан счёт"
    if not logo_fallback:
        if home_logo.get("missing") or not home_logo.get("path"):
            return False, f"нет логотипа: {data.get('home_team')}"
        if away_logo.get("missing") or not away_logo.get("path"):
            return False, f"нет логотипа: {data.get('away_team')}"
    if require_scorers:
        sh, sa = int(data["score_home"]), int(data["score_away"])
        gh = len(data.get("scorers_home") or [])
        ga = len(data.get("scorers_away") or [])
        if sh > 0 and gh == 0:
            return False, "нет авторов голов (хозяева)"
        if sa > 0 and ga == 0:
            return False, "нет авторов голов (гости)"
    return True, "ok"


def prepare_match_result(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Скачать медиа донора, vision-извлечение, логотипы. Raises RuntimeError при сбое."""
    from editorial.tg_media import download_item_media

    media_path = str(row.get("media_path") or "").strip()
    if not media_path or not Path(media_path).is_file():
        media_path = download_item_media(row)
    text = f"{row.get('title') or ''}\n{row.get('body') or ''}".strip()
    extracted = extract_match_result_from_image(media_path, text)
    home_logo, away_logo = resolve_pair(extracted["home_team"], extracted["away_team"])
    ok, reason = validate_match_result(extracted, home_logo, away_logo)
    if not ok:
        raise RuntimeError(reason)
    payload = {
        **extracted,
        "donor_image": media_path,
        "home_logo": home_logo,
        "away_logo": away_logo,
    }
    return payload, home_logo, away_logo
