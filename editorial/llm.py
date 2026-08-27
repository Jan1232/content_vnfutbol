"""Editorial LLM: Platform OpenAI API only. Public helpers stay stable."""

from __future__ import annotations

import json
from typing import Any

from app.config import get_settings
from editorial.jsonutil import parse_json_object
from editorial.openai_client import get_client, usage_scope


def chat(
    messages: list[dict[str, Any]],
    *,
    temperature: float | None = None,
    max_tokens: int = 1200,
    user: str = "editorial",
    model_kind: str = "text",
) -> str:
    _ = user
    if model_kind == "reasoning":
        primary, fallback = _reasoning_models()
    else:
        primary, fallback = _text_models()
    try:
        return get_client().chat(
            primary,
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            fallback=fallback,
            task=_task_from_user(user),
        )
    except Exception as e:
        if not _allow_groq():
            raise
        print(f"[editorial] OpenAI недоступен, Groq (флаг): {e}", flush=True)
        return _chat_groq(messages, temperature=temperature or 0.2, max_tokens=max_tokens)


def chat_json(
    system: str,
    user: str,
    *,
    temperature: float | None = None,
    max_tokens: int = 900,
    tag: str = "editorial",
    model_kind: str = "text",
) -> dict[str, Any]:
    # system стабилен байт-в-байт → prompt caching; переменные только в user
    system_msg = system.rstrip() + "\nОтвечай СТРОГО JSON без markdown."
    with usage_scope(task=_task_from_user(tag)):
        raw = chat(
            [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            user=tag,
            model_kind=model_kind,
        )
    return parse_json_object(raw)


def _text_models() -> tuple[str, str]:
    settings = get_settings()
    primary = (settings.editorial_text_model or "gpt-5.6-luna").strip()
    fallback = (settings.editorial_text_fallback or "gpt-5-mini").strip()
    return primary, fallback


def _reasoning_models() -> tuple[str, str]:
    settings = get_settings()
    primary = (settings.editorial_reasoning_model or "gpt-5.6-terra").strip()
    fallback = (settings.editorial_reasoning_fallback or "gpt-5.6-luna").strip()
    return primary, fallback


def _search_model() -> str:
    return (get_settings().editorial_search_model or "gpt-5-search-api").strip()


def _allow_groq() -> bool:
    return bool(get_settings().editorial_allow_groq_fallback)


def _task_from_user(user: str) -> str:
    u = (user or "chat").replace("ed-", "")
    return u[:40] or "chat"


def classify(title: str, body: str) -> dict[str, Any]:
    data = chat_json(
        "Ты классификатор футбольных новостей. Не выдумывай факты.",
        (
            "Определи тип события.\n"
            "event_type: transfer|injury|match_result|lineup|official_statement|rumor|lifestyle|other\n"
            "teams: список команд (англ. канон если знаешь)\n"
            "players: список игроков\n"
            "competition: PL|PD|SA|BL|FL1|CL|EL|RPL|WC|EC|UNL|NT|'' \n"
            "is_national: bool (сборные)\n"
            "Верни JSON: "
            '{"event_type":"...","teams":[],"players":[],"competition":"","is_national":false}\n\n'
            f"Заголовок: {title}\n\nТекст: {(body or '')[:1800]}"
        ),
        tag="ed-classify",
    )
    return data


def pick_news(
    title: str,
    body: str,
    *,
    signals: dict[str, Any],
    cluster_already_published: bool = False,
    human_factor_share: float = 0.0,
) -> dict[str, Any]:
    from editorial.policy import PICK_FEWSHOT, PICK_SYSTEM

    shots = "\n".join(json.dumps(s, ensure_ascii=False) for s in PICK_FEWSHOT)
    data = chat_json(
        PICK_SYSTEM,
        (
            f"Сигналы: {json.dumps(signals, ensure_ascii=False)}\n"
            f"cluster_already_published: {str(cluster_already_published).lower()}\n"
            f"human_factor_share: {human_factor_share:.2f}\n"
            f"Примеры разметки:\n{shots}\n\n"
            f"Заголовок: {title}\n"
            f"Текст: {(body or '')[:1600]}\n"
            'Верни JSON: {"take": false, "tag": "reject", "reason": "..."}'
        ),
        tag="ed-pick",
        max_tokens=400,
    )
    return data


_TOPIC_SYSTEM = (
    "Ты строгий классификатор темы. Разрешён ТОЛЬКО футбол "
    "(матчи, игроки, тренеры, клубы, сборные, ФИФА/УЕФА/лиги, трансферы, травмы игроков, судьи, стадионы клубов). "
    "Запрещены другие виды спорта, политика, криминал, шоу-бизнес, ставки, реклама, здоровье вне травмы игрока."
)

_REWRITE_SYSTEM = (
    "Ты старший редактор русскоязычного футбольного канала «ВСЕ НА ФУТБОЛ».\n"
    "Пиши ЖИВО и ИНФОРМАТИВНО по-русски. Без воды и кликбейта.\n"
    "Фактическую часть НЕ меняй: имена, суммы, счёт, даты — только из данных.\n"
    "Для матчей счёт обязателен, если он есть в заголовке, тексте или match_score в фактах.\n"
    "Нельзя писать, что счёт не указан, если он уже есть во входных данных.\n"
    "ОБЯЗАТЕЛЬНО переведи на русский. Английских слов в посте быть не должно "
    "(не midfield, deal, player — пиши по-русски).\n"
    "Объём: 2–4 абзаца через пустую строку, обычно 40–90 слов. "
    "В первом абзаце — кто и что случилось. Дальше — детали: имена, цифры, "
    "причина/контекст. Если в источнике есть яркая цитата — приведи её в «ёлочках» "
    "с автором через тире.\n"
    "Нельзя: одна голая фраза; пост = заголовок; дайджест нескольких новостей; "
    "мета-хвосты («официального объявления нет», «цитата не приводится»).\n"
    "Стикеры (ОБЯЗАТЕЛЬНО): каждый смысловой абзац начинается с тематического эмодзи: "
    "⚽ гол/матч, 🔴 срочно, ✍️ контракт, 💰 сумма, 🚑 травма, 🔥 сенсация, 🏆 трофей, 👀 слух. "
    "1 эмодзи на абзац, не гирлянда, не отдельной строкой в конце.\n"
    "Запрещено: orphan-эмодзи (отдельная строка только из эмодзи), >2 эмодзи подряд.\n"
    "Кавычки только «ёлочки», тире длинное —. Без вложенных „лапок“.\n"
    "stickers в JSON можно вернуть пустым []; они не дописываются в конец поста.\n"
    "Без CTA-ссылок."
)

_STORY_RELATION_SYSTEM = (
    "Ты редакторский судья сюжетов футбольного канала.\n"
    "Сравни новый пост с уже опубликованными по тому же сюжету.\n"
    "relation:\n"
    "- duplicate — пересказ уже сказанного (та же драка/слух/счёт другими словами; "
    "мнение/реакция эксперта без нового факта; уточнение формулировок).\n"
    "- development — есть НОВЫЙ фактический факт развития: вердикт, санкции, дисквалификация, "
    "травма, official-подтверждение, новая сумма/срок, решение федерации.\n"
    "- unrelated — это вообще другой сюжет (ключ ошибочно склеил).\n"
    "НЕ засчитывай за development мнения, эмоции и пересказ без новой детали.\n"
    "Верни JSON: "
    '{"relation":"duplicate|development|unrelated","new_facts":[],"confidence":0.0,"reason":"..."}'
)


def topic_check(title: str, body: str, entities: dict[str, Any]) -> dict[str, Any]:
    return chat_json(
        _TOPIC_SYSTEM,
        (
            f"Сущности: {json.dumps(entities, ensure_ascii=False)}\n"
            f"Заголовок: {title}\n"
            f"Текст: {(body or '')[:1600]}\n"
            'Верни JSON: {"is_football": true, "subtype": "match|transfer|injury|club|player|org|other", "reason": "..."}'
        ),
        tag="ed-topic",
    )


def factcheck(item: dict[str, Any], snippets: list[dict[str, Any]]) -> dict[str, Any]:
    return chat_json(
        (
            "Ты фактчекер футбольных новостей. Запрещено додумывать факты, которых нет в сниппетах. "
            "Если источники противоречат — укажи contradiction. "
            "Сенсационные диагнозы/болезни игрока без нескольких независимых источников — фейк."
        ),
        (
            f"Новость: {json.dumps({k: item.get(k) for k in ('title','body','event_type','url','source')}, ensure_ascii=False)}\n"
            f"Сниппеты независимых источников:\n{json.dumps(snippets[:12], ensure_ascii=False)}\n"
            "Верни JSON: "
            '{"consistent": true, "contradiction": null, "is_official": false, "confidence": 0.0, "reason": "..."}'
        ),
        tag="ed-factcheck",
    )


def rewrite(item: dict[str, Any], facts: str = "") -> dict[str, Any]:
    from editorial.stickers import pool_for_prompt

    pool_hint = ", ".join(pool_for_prompt())
    user_extra = ""
    if pool_hint:
        user_extra = f"Дополнительный пул стикеров редакции (можно использовать): {pool_hint}.\n"
    return chat_json(
        _REWRITE_SYSTEM,
        (
            f"{user_extra}"
            f"Заголовок: {item.get('title')}\n"
            f"Текст: {(item.get('body') or '')[:2500]}\n"
            f"Тип: {item.get('event_type')}\n"
            f"Подтверждённые факты:\n{facts[:1500]}\n"
            'Верни JSON: {"post_text":"...","headline":"...","emoji_lead":"⚽️","stickers":[]}'
        ),
        max_tokens=1400,
        tag="ed-rewrite",
    )


def caption(post_text: str, entities: dict[str, Any]) -> dict[str, Any]:
    from editorial.cover_text import prompt_limit_text

    # prompt_limit_text() стабилен (константы) — держим в system для caching
    system = (
        "Сгенерируй КОРОТКИЙ текст-заголовок НА картинку. "
        "Он передаёт суть поста, но НЕ повторяет его дословно и не копирует формулировки. "
        "Без эмодзи. Обычный регистр — капс сделает CSS.\n"
        f"{prompt_limit_text()}\n"
        "Текст — цельная фраза по правилам русского языка, не два обрубка.\n"
        "Цитата / реплика / оценка человека:\n"
        "— только кавычки-ёлочки «…»;\n"
        "— автор после цитаты через тире: «Ребята расстроены» — Галактионов;\n"
        "— или двоеточие перед цитатой: Галактионов: «Ребята расстроены»;\n"
        "— нельзя рвать цитату: имя отдельно, слова цитаты отдельно без «» и тире.\n"
        "Факт, не цитата — без кавычек: законченное предложение или связная именная фраза "
        "с запятыми, тире, двоеточием по норме. Пример: Локомотив уступил Ростову в Кубке.\n"
        "Дефис вместо тире не использовать."
    )
    return chat_json(
        system,
        (
            f"Пост:\n{post_text[:1200]}\n"
            f"Сущности: {json.dumps(entities, ensure_ascii=False)}\n"
            'Верни JSON: {"caption_line1":"...","caption_line2": null}'
        ),
        tag="ed-caption",
    )


def story_relation(
    title: str,
    body: str,
    prior_summaries: list[str],
) -> dict[str, Any]:
    """Повтор vs развитие сюжета. Модель: EDITORIAL_REASONING_MODEL (terra)."""
    listed = "\n".join(f"- {s}" for s in (prior_summaries or [])[:6] if str(s).strip())
    if not listed:
        listed = "- (нет summary)"
    data = chat_json(
        _STORY_RELATION_SYSTEM,
        (
            f"Уже опубликовано по сюжету:\n{listed}\n\n"
            f"Новый пост:\nЗаголовок: {title}\n"
            f"Текст: {(body or '')[:1800]}\n"
        ),
        tag="story_relation",
        max_tokens=500,
        temperature=0.1,
        model_kind="reasoning",
    )
    rel = str(data.get("relation") or "").strip().lower()
    if rel not in {"duplicate", "development", "unrelated"}:
        data["relation"] = "duplicate"
    return data


def image_search_query(title: str, *, year: str = "", event_type: str = "") -> str:
    """Короткий запрос в картинки: субъект + событие, не весь заголовок."""
    title = (title or "").strip()
    if not title:
        return ""
    system = (
        "Короткий запрос для Яндекс.Картинок: 3–6 слов, как человек вбивает в поиск.\n"
        "Один герой или один матч. Субъект + событие (или клуб). Год — только у финала/суперкубка.\n"
        "Не копируй заголовок. Выкинь «в N-й раз», «подряд», «в XXI веке», "
        "«это лучший результат», источники, Романо.\n"
        "Цитата в кавычках — НЕ ищи слова цитаты. Ищи автора (+ его клуб):\n"
        "«Многое меняется с приходом нового тренера». Диаш — о поражении «Ман Сити» "
        "→ Диаш Ман Сити\n"
        "«Мы знаем, что для этого потребуется». Артета — о шансах «Арсенала» → Микель Артета\n"
        "Имя: «цитата» — тоже имя, не цитата.\n"
        "Дайджест тура (несколько матчей через запятую) — только ПЕРВЫЙ матч.\n"
        "«X и Y в составе клуба» — первый названный + клуб, без соперника и турнира.\n"
        "Не соперник, если новость не про него. Не одно голое имя клуба. "
        "Не перечисляй всех. Без слов «фото» и «футбол фото». Без кавычек.\n"
        "Примеры:\n"
        "«Арсенал» в восьмой раз выиграл Суперкубок Англии в XXI веке. Это лучший результат "
        "→ Арсенал выиграл Суперкубок Англии 2026\n"
        "Сафонов и Забарный – в составе «ПСЖ» на матч с «Лансом» → Матвей Сафонов ПСЖ\n"
        "Альфа-Банк РПЛ. «Спартак» победил «Балтику» в гостях, «Зенит» разгромил «Динамо» "
        "→ Альфа-Банк РПЛ матч Спартак Балтика"
    )
    user = (
        f"Заголовок: {title}\n"
        f"Год: {year or '—'}\n"
        f"Тип: {event_type or '—'}\n"
        'Верни JSON: {"query":"..."}'
    )
    settings = get_settings()
    model = (settings.editorial_vision_model or "gpt-4o-mini").strip()
    with usage_scope(task="ed-image-query"):
        raw = get_client().chat(
            model,
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            json_mode=True,
            max_tokens=80,
            temperature=0.2,
            task="ed-image-query",
        )
    data = parse_json_object(raw)
    return str(data.get("query") or "").strip()


def pick_image(title: str, candidates: list[str]) -> dict[str, Any]:
    listed = "\n".join(f"{i}: {c}" for i, c in enumerate(candidates[:12]))
    return chat_json(
        (
            "Выбери самое релевантное и «чистое» фото футбольной новости "
            "(без чужих вотермарков, без коллажей, без скриншотов соцсетей, "
            "без текста на кадре кроме логотипа клуба и Here we go)."
        ),
        f"Новость: {title}\nКандидаты:\n{listed}\n"
        'Верни JSON: {"index": 0, "reason": "..."}',
        tag="ed-image",
    )


def web_search(query: str) -> list[dict[str, Any]]:
    """Веб-поиск через gpt-5-search-api (Platform), не OpenClaw/OAuth."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        return get_client().web_search(_search_model(), q, max_results=8, task="search")
    except Exception as e:
        print(f"[editorial] web_search fail: {e}", flush=True)
        raise


def _chat_groq(
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    max_tokens: int,
) -> str:
    """Последний фолбэк. Вызывается только при EDITORIAL_ALLOW_GROQ_FALLBACK=true."""
    import time
    import re
    from pathlib import Path

    import httpx

    from app.http_util import SYSTEM_CA

    settings = get_settings()
    api_key = (settings.groq_api_key or "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY не задан")
    models: list[str] = []
    for name in (settings.groq_model, "openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b"):
        name = (name or "").strip()
        if name and name not in models:
            models.append(name)
    proxy = (settings.groq_http_proxy or settings.scraper_http_proxy or "").strip() or None
    verify = SYSTEM_CA if Path(SYSTEM_CA).exists() else True
    last_err = "Groq: нет модели"
    with httpx.Client(timeout=90.0, verify=verify, proxy=proxy) as client:
        for model in models:
            payload = {
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            for attempt in range(4):
                r = client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
                if r.status_code == 429:
                    wait = 1.0 + attempt
                    m = re.search(r"try again in (\d+(?:\.\d+)?)ms", r.text or "", re.I)
                    if m:
                        wait = min(8.0, max(0.4, float(m.group(1)) / 1000.0 + 0.2))
                    time.sleep(wait)
                    continue
                if r.status_code >= 400:
                    last_err = f"Groq {r.status_code} {model}: {r.text[:240]}"
                    if r.status_code in {400, 404}:
                        break
                    raise RuntimeError(last_err)
                return r.json()["choices"][0]["message"]["content"].strip()
    raise RuntimeError(last_err)
