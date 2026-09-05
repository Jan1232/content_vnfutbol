"""v2/v2.2: Terra-генератор без критика. Веер (temp=1.0) + калиброванный single (temp=0.7)."""

from __future__ import annotations

from src.config import FAN_MODEL, get_openai_client
from src.generate.calibration_prompt import build_system_prompt
from src.generate.dna import format_dna
from src.generate.fewshot_dataset import FEWSHOT_FOR_PRODUCER
from src.generate.guardrail import check_guardrail, has_self_harm_literal

VECTOR_DRY = (
    "По стилю тяготей к сухой дерзкой констатации с минимумом слов: факт + один "
    "точный акцент, без лишних слов и без универсальных концовок. Это ВЕКТОР, не "
    "жёсткое правило — если конкретная ситуация просит подкола, неожиданного ракурса "
    "или капс-заголовка, ты свободен отклониться. Главное — живо и по делу, не по "
    "шаблону."
)

# Подсказки направления — не рельсы (веер v2)
DIRECTIONS = (
    "злой подкол/провокация",
    "сухая дерзкая констатация, минимум слов",
    "неожиданный ракурс или сравнение",
    "капс-заголовок + факты, как в горячей новости",
    "свободный — пиши как чувствуешь",
)

_FORMAT_REF = """Справка о ФОРМЕ (не трафарет для копирования):
- Длина обычно 1–4 строки; списки (результаты/достижения) длиннее.
- Плашки только для сенсаций: 💣 HERE WE GO! (transfer+verified+sensation),
  ❌ ОТМЕНА HERE WE GO!, 🔥 СРОЧНО, ⚡️ ОФИЦИАЛЬНО. Рядовое — без плашки.
- HERE WE GO! запрещён, если veracity != verified.
- Эмодзи-буллеты деталей (💸 ✍️), флаги стран, капс точечно (1–2 слова).
- Непроверенное — с маркером («сообщают», «Романо», «слухи»).
- Без подписей/ссылок на чужие каналы.
- Цифры и имена — только из факта."""


def _atmosphere_block(n: int = 18) -> str:
    """Разные приёмы канала как ДУХ, не шаблоны (для веера v2)."""
    seen: set[str] = set()
    picks: list[dict] = []
    for ex in FEWSHOT_FOR_PRODUCER:
        if ex["archetype"] in seen and len(picks) >= n // 2:
            continue
        picks.append(ex)
        seen.add(ex["archetype"])
        if len(picks) >= n:
            break
    for ex in FEWSHOT_FOR_PRODUCER:
        if ex in picks:
            continue
        picks.append(ex)
        if len(picks) >= n:
            break

    lines = [
        "Реальные посты канала — это ДИАПАЗОН голоса и дозволенного, "
        "НЕ набор шаблонов для копирования.",
        "",
    ]
    for i, ex in enumerate(picks, 1):
        lines.append(f"[{i}|{ex['archetype']}]\n{ex['post']}")
        lines.append("")
    return "\n".join(lines)


def _build_instructions_fan() -> str:
    """Системник веера v2 (без калибровки вкуса)."""
    return f"""Ты — автор дерзкого футбольного Telegram-канала. Твоя сила в том, что ты не
пишешь шаблонно: ты реагируешь на КАЖДУЮ новость свежо — обыгрываешь ситуацию,
находишь неожиданный ракурс, даёшь живой комментарий. Ты человек, а не
пресс-релиз и не генератор по трафарету.

{_FORMAT_REF}

{_atmosphere_block()}

Планка: дерзко, но умно (не клоунада). Живо, но по делу. Один точный акцент
лучше каскада. НЕ обязан шутить в каждом посте. НЕ лепи универсальные концовки
(«время покажет», «справится ли?») — это шаблон, а мы против шаблонов.

Мат со звёздочкой (ПИ*ДЕЦ) допустим. Деликатные темы (смерть, депрессия) —
не вычищай. Самоповреждение дословно НЕ воспроизводи — только инфоповод без акта.

Вывод — ГОТОВЫЙ текст одного поста. Без JSON, без пояснений, без markdown-кодблоков."""


def _build_instructions_calibrated() -> str:
    """Системник v2.2: ДНК + вкус владельца из calibration_prompt."""
    base = f"""Ты — автор дерзкого футбольного Telegram-канала. Ты человек-редактор, не
генератор афоризмов. Реагируй на новость свежо и по делу.

{format_dna()}

Мат со звёздочкой (ПИ*ДЕЦ) допустим. Деликатные темы (смерть, депрессия) —
не вычищай. Самоповреждение дословно НЕ воспроизводи — только инфоповод без акта.
Криминал / уголовные дела — только фактическая подача, БЕЗ подкола.

Вывод — ГОТОВЫЙ текст одного поста. Без JSON, без пояснений, без markdown-кодблоков."""
    return build_system_prompt(base)


def _build_user(
    fact: str,
    veracity: str,
    archetype: str,
    is_sensation: bool,
    direction: str,
    *,
    note: str | None = None,
    reformulate_self_harm: bool = False,
) -> str:
    parts = [
        f"Факт: {fact}",
        f"Достоверность: {veracity}",
        f"Архетип: {archetype}",
        f"Сенсация: {is_sensation}",
        f"Направление (подсказка, не рельсы): {direction}",
        "Напиши ОДИН пост в стиле канала. Реагируй свежо — обыграй именно ЭТУ ситуацию, "
        "а не подставляй универсальный шаблон. Работай только с переданным фактом: "
        "никаких цифр/имён сверх данных. Если не verified — маркер источника обязателен, "
        "HERE WE GO запрещён. "
        "Свобода — в подаче, не в фактах: не приписывай СМИ/инсайдеру слова и образы, "
        "которых нет во входе. Авторский подкол — голосом канала, отдельно, не под видом цитаты источника.",
    ]
    if note:
        parts.append(f"Пометка к факту: {note}")
    if reformulate_self_harm or has_self_harm_literal(fact):
        parts.append(
            "Во факте есть угроза/акт самоповреждения: возьми инфоповод "
            "(обещание/ставка на кон), но НЕ пиши дословно акт самоповреждения."
        )
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


def _clean_post(post: str) -> str:
    post = post.strip()
    if post.startswith("```"):
        post = post.strip("`").strip()
        if post.lower().startswith("text\n"):
            post = post[5:].strip()
    return post


def _call_terra(
    *,
    instructions: str,
    user: str,
    temperature: float,
) -> str:
    client = get_openai_client()
    response = client.responses.create(
        model=FAN_MODEL,
        instructions=instructions,
        input=user,
        reasoning={"effort": "none"},
        temperature=temperature,
        text={"verbosity": "low"},
    )
    return _clean_post(_extract_text(response))


def generate_one(
    fact: str,
    veracity: str,
    archetype: str,
    is_sensation: bool,
    direction: str,
    *,
    temperature: float = 1.0,
    calibrated: bool = False,
    note: str | None = None,
) -> str:
    instructions = (
        _build_instructions_calibrated() if calibrated else _build_instructions_fan()
    )
    user = _build_user(
        fact, veracity, archetype, is_sensation, direction, note=note
    )
    post = _call_terra(
        instructions=instructions, user=user, temperature=temperature
    )

    if has_self_harm_literal(post):
        user = _build_user(
            fact,
            veracity,
            archetype,
            is_sensation,
            direction,
            note=note,
            reformulate_self_harm=True,
        )
        post = _call_terra(
            instructions=instructions, user=user, temperature=temperature
        )

    return post


def generate_fan(
    fact: str,
    veracity: str,
    archetype: str,
    is_sensation: bool,
    n: int = 5,
) -> list[dict]:
    """n отдельных вызовов → список {index, direction, post, flags}."""
    results: list[dict] = []
    for i, direction in enumerate(DIRECTIONS[:n], 1):
        post = generate_one(fact, veracity, archetype, is_sensation, direction)
        flags = check_guardrail(post, veracity, fact=fact)
        results.append(
            {
                "index": i,
                "direction": direction,
                "post": post,
                "flags": flags,
            }
        )
    return results


def generate_single(
    fact: str,
    veracity: str,
    archetype: str,
    is_sensation: bool,
    *,
    note: str | None = None,
) -> dict:
    """Один пост Terra v2.2: калибровка вкуса, temperature=0.7."""
    post = generate_one(
        fact,
        veracity,
        archetype,
        is_sensation,
        VECTOR_DRY,
        temperature=0.7,
        calibrated=True,
        note=note,
    )
    flags = check_guardrail(post, veracity, fact=fact)
    return {"post": post, "flags": flags}
