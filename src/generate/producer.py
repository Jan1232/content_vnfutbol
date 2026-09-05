"""Producer: сухой факт → Telegram-пост в стиле @zhfootballll."""

from __future__ import annotations

from src.config import PRODUCER_MODEL, get_openai_client
from src.generate.dna import format_dna, format_fewshot_block, format_tone_counterexamples

_SYSTEM_TEMPLATE = """Ты — дерзкий, но не клоунский редактор футбольного Telegram-канала.
Планка тона: 80% информативная подача + 20% лёгкий подкол/вопрос.
Юмор — приправа (один акцент), не блюдо. Не вычурные метафоры, не каскад шуток.

{dna}

{fewshot}

{tone_examples}

Правила:
- Работай ТОЛЬКО с переданным фактом. Никаких цифр, имён, сумм сверх данных.
- Если veracity != verified — обязателен маркер непроверенности.
- HERE WE GO! только при veracity=verified.
- Капс точечный (1–2 слова), не фразой целиком (исключение: короткие акценты вроде НИ-КО-ГДА).
- НИКОГДА не ставь подписи/ссылки на чужие каналы (ЖФ | ТРАНСФЕРЫ, t.me/, подписывайтесь…).
- НЕ воспроизводи дословно описания/угрозы самоповреждения. Инфоповод можно взять
  (ставка/обещание на кон) — без буквальной фразы акта.
- Мат-эмоция со звёздочкой (ПИ*ДЕЦ) — допустима, если уместна по факту.
- Деликатные темы (смерть, депрессия, болезни) — не вычищать, это голос канала.
- Вывод — ГОТОВЫЙ текст поста. Без JSON, без markdown-кодблоков, без пояснений."""

_ARCHETYPE_RULES = {
    "goal_live": (
        "archetype=goal_live: короткий live-гол.\n"
        "Структура: `[эмодзи] ГОООООЛ! [эпитет если дан] АВТОР минута'` затем строка "
        "`[флаг] Команда СЧЁТ Команда [флаг]`. Фактурно, почти без юмора. "
        "Эпитеты («СОЛЬНЫЙ ШЕДЕВР») — только если они есть во факте или очевидно следует "
        "из формулировки факта; не выдумывай."
    ),
    "quote_hypocrisy": (
        "archetype=quote_hypocrisy: две РЕАЛЬНЫЕ противоречащие цитаты одного лица.\n"
        "Структура: заголовок-подкол («мастер переобувания») + "
        "`[флаг] X до …: «…»` / `[флаг] X после: «…»`.\n"
        "Цитаты передавай ДОСЛОВНО из факта. НЕ сочиняй и НЕ перефразируй цитаты."
    ),
    "quote_scandal": (
        "archetype=quote_scandal: скандальная цитата ИЛИ расследование через факты.\n"
        "Заголовок с эмоцией («ЭТО БЫЛО ЖЁСТКО!»), цепочка фактов → вывод. "
        "Капс вразрядку допустим для акцента (НИ-КО-ГДА). Только факты/цитаты из входа."
    ),
    "achievement": (
        "archetype=achievement: рекорд/веха + сравнительный список или стата построчно.\n"
        "Структура: заголовок-достижение, затем `[флаг] Игрок – N` или "
        "`⚽️ / 🅰️ / 🏆` статистика. Цифры строго из факта. Юмора почти нет."
    ),
    "humor_list": (
        "archetype=humor_list: юмористический топ/список.\n"
        "Заголовок + нумерованные/маркированные пункты с подколом. "
        "Юмор — ядро формата, но один заход, не каскад шуток поверх."
    ),
    "injury_list": (
        "archetype=injury_list: сатирический перечень (травмы/статистика).\n"
        "Заголовок-сатира («состав немощи») + построчно `❌ Игрок – N дней`. "
        "Цифры строго из факта."
    ),
    "schedule": (
        "archetype=schedule: если во факте указано время/дата матчей — "
        "ОБЯЗАТЕЛЬНО выведи их построчно у каждой пары "
        "(«„Реал“ — „Ливерпуль“ 25 сентября в 20:00 по мск»). "
        "Не выдумывай время, если его нет в факте."
    ),
}


def _build_instructions(archetype: str) -> str:
    return _SYSTEM_TEMPLATE.format(
        dna=format_dna(),
        fewshot=format_fewshot_block(archetype),
        tone_examples=format_tone_counterexamples(archetype, k=3),
    )


def _build_user_prompt(
    fact: str,
    veracity: str,
    archetype: str,
    is_sensation: bool,
    feedback: str | None = None,
) -> str:
    sensation_rules = {
        True: (
            "is_sensation=true → нужна плашка-заголовок (где уместно):\n"
            "  transfer + verified → 💣 HERE WE GO! (в начале, далее имя КАПСОМ точечно)\n"
            "  transfer_cancel → ❌ ОТМЕНА HERE WE GO!\n"
            "  официальное подтверждение → ⚡️ ОФИЦИАЛЬНО\n"
            "  иначе срочная сенсация → 🔥 СРОЧНО\n"
            "  goal_live / achievement / quote_scandal / injury_list / result — "
            "плашка не обязательна; держи формат архетипа (ГОООООЛ / заголовок-достижение)."
        ),
        False: "is_sensation=false → БЕЗ плашки HERE WE GO/СРОЧНО/ОФИЦИАЛЬНО, сразу с сути.",
    }

    parts = [
        f"Факт: {fact}",
        f"Достоверность (veracity): {veracity}",
        f"Архетип: {archetype}",
        f"Сенсация (is_sensation): {is_sensation}",
        sensation_rules[is_sensation],
        "Ничего не додумывай. Цифры и имена — только из факта.",
        "Один лёгкий акцент максимум (подкол или вопрос). Не переюморивай.",
    ]

    if veracity != "verified":
        parts.append(
            "veracity != verified → маркер слуха обязателен "
            "(«сообщают источники», «Романо пишет», «если верить Романо», «Моретто пишет», "
            "«Marca», «AS», «появлялись слухи»). "
            "HERE WE GO! ЗАПРЕЩЕНА."
        )

    if archetype in _ARCHETYPE_RULES:
        parts.append(_ARCHETYPE_RULES[archetype])

    # Guardrail hint when fact mentions self-harm
    fact_l = fact.lower()
    if any(x in fact_l for x in ("отреж", "отреза", "самоповрежд", "пореж", "повеш")):
        parts.append(
            "GUARDRAIL: во факте есть угроза/акт самоповреждения. "
            "Возьми инфоповод (обещание/ставка на кон), но НЕ воспроизводи дословно акт. "
            "Переформулируй без конкретной фразы самоповреждения."
        )

    if feedback:
        parts.append(
            f"Прошлая версия и замечания редактора:\n{feedback}\n"
            "Перепиши, устранив их. Если замечание про переюмор — убери каскад шуток, "
            "оставь факты + один лёгкий акцент. "
            "Если HARD про самоповреждение — убери дословный акт, оставь инфоповод."
        )

    parts.append("Напиши готовый пост:")
    return "\n\n".join(parts)


def _extract_text(response) -> str:
    text = getattr(response, "output_text", None)
    if text:
        return text.strip()
    chunks: list[str] = []
    for item in response.output:
        if item.type == "message":
            for content in item.content:
                if content.type == "output_text":
                    chunks.append(content.text)
    return "\n".join(chunks).strip()


def generate_post(
    fact: str,
    veracity: str,
    archetype: str,
    is_sensation: bool,
    feedback: str | None = None,
) -> str:
    client = get_openai_client()
    response = client.responses.create(
        model=PRODUCER_MODEL,
        instructions=_build_instructions(archetype),
        input=_build_user_prompt(fact, veracity, archetype, is_sensation, feedback),
        reasoning={"effort": "none"},
        temperature=0.9,
        text={"verbosity": "low"},
    )
    post = _extract_text(response)
    if post.startswith("```"):
        post = post.strip("`").strip()
        if post.lower().startswith("text\n"):
            post = post[5:].strip()
    return post
