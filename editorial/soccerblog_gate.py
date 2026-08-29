"""Multimodal LLM gate for SoccerBlog TG meme feed."""

from __future__ import annotations

from typing import Any

from app.config import get_settings
from editorial.jsonutil import parse_json_object
from editorial.media_preview import media_preview_from_post
from editorial.openai_client import get_client, usage_scope
from editorial.topic_gate import classify_meme_event

_GATE_SYSTEM = (
    "Ты классификатор постов футбольного Telegram-канала SoccerBlog.\n"
    "Смотри текст И картинку/кадр видео.\n"
    "kind:\n"
    "- meme — юмор, мем-композиция, коллаж с подписью, реакция болельщиков, lifestyle;\n"
    "- news — футбольная новость, которую стоит упаковать в шаблонную карточку "
    "(интервью, заявление, аналитика, не мем);\n"
    "- reject — трансферы, составы, результаты матчей, таблицы, дубли сухих новостей.\n"
    "is_media_meme=true если на медиа мем-композиция (подпись/коллаж/юмор на фото).\n"
    "Верни JSON: "
    '{"kind":"meme|news|reject","reason":"...","confidence":0.0,'
    '"is_media_meme":false,"text_lang":"ru|en|other"}'
)


def _gate_model() -> str:
    settings = get_settings()
    explicit = (getattr(settings, "soccerblog_gate_model", None) or "").strip()
    if explicit:
        return explicit
    return (settings.editorial_vision_model or "gpt-5.6-luna").strip()


def _normalize_verdict(data: dict[str, Any]) -> dict[str, Any]:
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in {"meme", "news", "reject"}:
        # fallback по тексту
        hard = classify_meme_event(str(data.get("_text") or ""))
        kind = "reject" if hard in {"transfer", "injury", "match_result", "lineup", "official_statement"} else "meme"
    try:
        conf = float(data.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    lang = str(data.get("text_lang") or "ru").strip().lower()
    if lang not in {"ru", "en", "other"}:
        lang = "other"
    return {
        "kind": kind,
        "reason": str(data.get("reason") or "")[:400],
        "confidence": conf,
        "is_media_meme": bool(data.get("is_media_meme")),
        "text_lang": lang,
    }


def _text_fallback_verdict(text: str) -> dict[str, Any]:
    hard = classify_meme_event(text)
    if hard in {"transfer", "injury", "match_result", "lineup", "official_statement"}:
        return {
            "kind": "reject",
            "reason": f"text rule: {hard}",
            "confidence": 0.85,
            "is_media_meme": False,
            "text_lang": "ru",
            "fallback": "text_rules",
        }
    return {
        "kind": "meme",
        "reason": "text rule: lifestyle",
        "confidence": 0.6,
        "is_media_meme": False,
        "text_lang": "ru",
        "fallback": "text_rules",
    }


def soccerblog_gate(
    text: str,
    media: list[dict[str, Any]] | None = None,
    *,
    media_type: str = "",
) -> dict[str, Any]:
    """Мультимодальный вердикт: meme | news | reject."""
    settings = get_settings()
    if not bool(getattr(settings, "soccerblog_gate_enabled", True)):
        return _text_fallback_verdict(text)

    blob = (text or "").strip()
    preview = media_preview_from_post(media or [], media_type=media_type)
    user_text = (
        f"Текст поста:\n{blob[:2000]}\n"
        f"Тип медиа: {media_type or 'unknown'}\n"
        "Классифицируй по правилам."
    )
    model = _gate_model()
    try:
        with usage_scope(task="soccerblog_gate"):
            if preview:
                data = get_client().vision(
                    model,
                    [preview],
                    user_text,
                    json_mode=True,
                    max_tokens=600,
                    task="soccerblog_gate",
                )
            else:
                raw = get_client().chat(
                    model,
                    [
                        {"role": "system", "content": _GATE_SYSTEM + "\nОтвечай СТРОГО JSON."},
                        {"role": "user", "content": user_text},
                    ],
                    json_mode=True,
                    max_tokens=600,
                    task="soccerblog_gate",
                )
                data = parse_json_object(raw)
        data["_text"] = blob
        return _normalize_verdict(data)
    except Exception as e:
        print(f"[editorial] soccerblog_gate fail: {e}", flush=True)
        fb = _text_fallback_verdict(blob)
        fb["reason"] = f"gate error: {e}"[:200]
        fb["confidence"] = 0.5
        return fb


def gate_verdict_of_row(row: dict[str, Any]) -> dict[str, Any] | None:
    import json

    try:
        entities = json.loads(row.get("entities_json") or "{}")
    except Exception:
        entities = {}
    v = entities.get("soccerblog_gate")
    return v if isinstance(v, dict) else None


def should_auto_publish(row: dict[str, Any]) -> bool:
    settings = get_settings()
    if not bool(getattr(settings, "soccerblog_auto_publish", False)):
        return False
    if not bool(getattr(settings, "soccerblog_gate_enabled", True)):
        return False
    v = gate_verdict_of_row(row)
    if not v:
        return False
    try:
        conf = float(v.get("confidence") or 0)
    except (TypeError, ValueError):
        return False
    return conf >= float(getattr(settings, "soccerblog_auto_confidence", 0.8) or 0.8)
