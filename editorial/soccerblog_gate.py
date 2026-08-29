"""Multimodal donor gate for TG feeds: as_is | template | reject."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT, get_settings
from editorial.jsonutil import parse_json_object
from editorial.media_preview import media_preview_from_post
from editorial.openai_client import get_client, usage_scope

GATE_VERSION = 2

_GATE_SYSTEM = (
    "Ты классификатор постов футбольных Telegram-каналов. Смотри текст И медиа.\n"
    "Определи, как публиковать пост. Тема (трансфер/матч/состав/новость) НЕ влияет — все темы равны.\n"
    "Реши по ФОРМАТУ и НАМЕРЕНИЮ:\n\n"
    "kind:\n"
    "- as_is — публикуем без переоформления:\n"
    "  * видео (любой темы);\n"
    "  * чистый мем/юмор: шутка, реакция болельщиков, юмор-коллаж, издёвка.\n"
    "    Это про НАМЕРЕНИЕ (посмеяться), не про «на картинке есть текст».\n"
    "- template — упаковываем в свой шаблон (ДЕФОЛТ для большинства):\n"
    "  * любая футбольная новость: трансфер, результат матча, гол, состав, заявление, аналитика;\n"
    "  * пост-картинка со счётом/графикой матча — это НОВОСТЬ, не мем (счёт ≠ юмор).\n"
    "- reject — не публикуем:\n"
    "  * реклама, промо, ставки, букмекеры, казино, промокоды, партнёрские посты;\n"
    "  * не футбол;\n"
    "  * голая турнирная таблица, технический дубль.\n\n"
    "Правило по умолчанию: если футбол, не видео/мем, не реклама → template.\n"
    "Отличай мем от новости-с-графикой: счёт матча на картинке = template; шутка на картинке = as_is.\n\n"
    'JSON: {"kind":"as_is|template|reject","reason":"...","confidence":0.0,'
    '"is_video":false,"is_ad":false,"text_lang":"ru|en|other"}'
)

_RULES_PATH = ROOT / "editorial" / "rules_content.yaml"


@lru_cache
def _donor_gate_rules() -> dict[str, Any]:
    try:
        with open(_RULES_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        data = {}
    block = data.get("donor_gate") if isinstance(data.get("donor_gate"), dict) else {}
    pat = str(block.get("ad_patterns") or "").strip()
    if pat:
        ad_re = re.compile(re.sub(r"\s*\n\s*", "", pat), re.I)
    else:
        ad_re = re.compile(
            r"ставк|букмекер|казино|промокод|реф\.?\s*ссыл|партнёрск|партнерск|"
            r"переходи по ссылке|видео прогноз|ставлю на матч",
            re.I,
        )
    markers = block.get("ad_markers") or []
    if not isinstance(markers, list):
        markers = []
    return {"ad_re": ad_re, "ad_markers": [str(x) for x in markers if str(x).strip()]}


def reload_donor_gate_rules() -> None:
    _donor_gate_rules.cache_clear()


def detect_ad(text: str) -> tuple[bool, str]:
    blob = (text or "").strip()
    if not blob:
        return False, ""
    rules = _donor_gate_rules()
    if rules["ad_re"].search(blob):
        return True, "ad pattern"
    low = blob.lower()
    for m in rules["ad_markers"]:
        if m.lower() in low:
            return True, f"ad marker: {m}"
    return False, ""


def effective_gate_kind(verdict: dict[str, Any] | None) -> str:
    k = str((verdict or {}).get("kind") or "").strip().lower()
    if k == "meme":
        return "as_is"
    if k == "news":
        return "template"
    if k in {"as_is", "template", "reject"}:
        return k
    settings = get_settings()
    default = str(getattr(settings, "donor_gate_default", "template") or "template").strip().lower()
    return default if default in {"as_is", "template", "reject"} else "template"


def _gate_model() -> str:
    settings = get_settings()
    explicit = (getattr(settings, "soccerblog_gate_model", None) or "").strip()
    if explicit:
        return explicit
    return (settings.editorial_vision_model or "gpt-5.6-luna").strip()


def _gate_reasoning_effort() -> str | None:
    effort = (getattr(get_settings(), "editorial_reasoning_effort", None) or "").strip()
    return effort or None


def _ad_reject_verdict(reason: str) -> dict[str, Any]:
    return {
        "kind": "reject",
        "reason": reason[:400],
        "confidence": 1.0,
        "is_video": False,
        "is_ad": True,
        "text_lang": "ru",
        "gate_version": GATE_VERSION,
        "source": "ad_rules",
    }


def _gate_error_verdict(error: Exception | str) -> dict[str, Any]:
    msg = str(error)[:200]
    return {
        "kind": "reject",
        "reason": f"gate error: {msg}",
        "confidence": 0.0,
        "is_video": False,
        "is_ad": False,
        "text_lang": "ru",
        "gate_version": GATE_VERSION,
        "fallback": "gate_error",
        "gate_failed": True,
    }


def _default_template_verdict(reason: str = "default template") -> dict[str, Any]:
    return {
        "kind": "template",
        "reason": reason[:400],
        "confidence": 0.55,
        "is_video": False,
        "is_ad": False,
        "text_lang": "ru",
        "gate_version": GATE_VERSION,
        "fallback": "default",
    }


def _normalize_verdict(
    data: dict[str, Any],
    *,
    has_video: bool = False,
    text: str = "",
) -> dict[str, Any]:
    settings = get_settings()
    strict_ad = bool(getattr(settings, "ad_reject_strict", True))

    is_ad = bool(data.get("is_ad"))
    ad_hit, ad_reason = detect_ad(text)
    if ad_hit or is_ad:
        if strict_ad or is_ad:
            return _ad_reject_verdict(ad_reason or "is_ad from model")

    kind = effective_gate_kind(data)
    if has_video and kind != "reject":
        kind = "as_is"

    if kind not in {"as_is", "template", "reject"}:
        kind = str(getattr(settings, "donor_gate_default", "template") or "template")

    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))

    lang = str(data.get("text_lang") or "ru").strip().lower()
    if lang not in {"ru", "en", "other"}:
        lang = "other"

    out: dict[str, Any] = {
        "kind": kind,
        "reason": str(data.get("reason") or "")[:400],
        "confidence": conf,
        "is_video": bool(data.get("is_video")) or has_video,
        "is_ad": False,
        "text_lang": lang,
        "gate_version": GATE_VERSION,
    }
    if data.get("gate_failed"):
        out["gate_failed"] = True
        out["fallback"] = "gate_error"
    elif data.get("fallback"):
        out["fallback"] = data.get("fallback")
    return out


def donor_gate(
    text: str,
    media: list[dict[str, Any]] | None = None,
    *,
    media_type: str = "",
) -> dict[str, Any]:
    """Мультимодальный вердикт донора: as_is | template | reject."""
    settings = get_settings()
    blob = (text or "").strip()
    has_video = media_type == "video" or any((m.get("type") or "") == "video" for m in (media or []))

    ad_hit, ad_reason = detect_ad(blob)
    if ad_hit and bool(getattr(settings, "ad_reject_strict", True)):
        return _ad_reject_verdict(ad_reason)

    if not bool(getattr(settings, "soccerblog_gate_enabled", True)):
        if has_video:
            return _normalize_verdict({"kind": "as_is", "confidence": 0.7, "reason": "video"}, has_video=True, text=blob)
        return _default_template_verdict("gate disabled")

    preview = media_preview_from_post(media or [], media_type=media_type)
    user_text = (
        f"Текст поста:\n{blob[:2000]}\n"
        f"Тип медиа: {media_type or 'unknown'}\n"
        "Классифицируй по правилам."
    )
    model = _gate_model()
    effort = _gate_reasoning_effort()
    try:
        with usage_scope(task="donor_gate"):
            if preview:
                data = get_client().vision(
                    model,
                    [preview],
                    user_text,
                    json_mode=True,
                    max_tokens=2500,
                    task="donor_gate",
                    reasoning_effort=effort,
                )
            else:
                raw = get_client().chat(
                    model,
                    [
                        {"role": "system", "content": _GATE_SYSTEM + "\nОтвечай СТРОГО JSON."},
                        {"role": "user", "content": user_text},
                    ],
                    json_mode=True,
                    max_tokens=2500,
                    task="donor_gate",
                    reasoning_effort=effort,
                )
                data = parse_json_object(raw)
        data["_text"] = blob
        return _normalize_verdict(data, has_video=has_video, text=blob)
    except Exception as e:
        print(f"[editorial] donor_gate fail: {e}", flush=True)
        return _gate_error_verdict(e)


# обратная совместимость
soccerblog_gate = donor_gate


def gate_verdict_of_row(row: dict[str, Any]) -> dict[str, Any] | None:
    import json

    try:
        entities = json.loads(row.get("entities_json") or "{}")
    except Exception:
        entities = {}
    for key in ("donor_gate", "soccerblog_gate"):
        v = entities.get(key)
        if isinstance(v, dict):
            return v
    return None


def should_auto_publish(row: dict[str, Any]) -> bool:
    settings = get_settings()
    if not bool(getattr(settings, "soccerblog_auto_publish", False)):
        return False
    if not bool(getattr(settings, "soccerblog_gate_enabled", True)):
        return False
    v = gate_verdict_of_row(row)
    if not v or v.get("gate_failed"):
        return False
    if effective_gate_kind(v) != "as_is":
        return False
    try:
        conf = float(v.get("confidence") or 0)
    except (TypeError, ValueError):
        return False
    return conf >= float(getattr(settings, "soccerblog_auto_confidence", 0.8) or 0.8)
