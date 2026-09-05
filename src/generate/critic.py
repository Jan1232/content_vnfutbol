"""Critic: пост → структурированный вердикт (код + LLM)."""

from __future__ import annotations

import json
import re

from src.config import CRITIC_MODEL, get_openai_client
from src.generate.tone_reference import FORBIDDEN_SIGNATURES, get_tone_examples

CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "revise"]},
        "issues": {"type": "array", "items": {"type": "string"}},
        "how_to_improve": {"type": "string"},
    },
    "required": ["verdict", "issues", "how_to_improve"],
    "additionalProperties": False,
}

BADGE_PREFIXES = ("💣", "🔥", "⚡️", "⚡", "❌", "🚨")

# Плашки-сенсации (не путать с ⚡️ ГОООООЛ / обычными эмодзи-заголовками)
_SENSATION_BADGE_RE = re.compile(
    r"^(💣|🔥|⚡️|⚡|❌|🚨).{0,40}(HERE\s*WE\s*GO|СРОЧНО|ОФИЦИАЛЬНО|ОТМЕНА)",
    re.IGNORECASE,
)

RUMOR_MARKERS = (
    "источник",
    "сообщают",
    "пишет",
    "инсайдер",
    "по информации",
    "романо",
    "моретто",
    "если верить",
    "marca",
    "слухи",
    "слух",
    "по данным",
)

_LENGTH_LIMITS = {
    "result": 8,
    "schedule": 8,
    "goal_live": 6,
    "achievement": 12,
    "humor_list": 12,
    "injury_list": 14,
    "quote_hypocrisy": 8,
    "quote_scandal": 10,
}

# Архетипы без стандартных плашек HERE WE GO / СРОЧНО
_NO_SENSATION_BADGE = {
    "goal_live",
    "achievement",
    "humor_list",
    "injury_list",
    "quote_hypocrisy",
    "quote_scandal",
    "provocation",
    "result",
}

# Дословное тиражирование акта самоповреждения → HARD
_SELF_HARM_PATTERNS = (
    re.compile(r"отреж\w*\s+себе", re.IGNORECASE),
    re.compile(r"отреза\w*\s+себе", re.IGNORECASE),
    re.compile(r"пореж\w*\s+себе", re.IGNORECASE),
    re.compile(r"повеш\w*\s+ся", re.IGNORECASE),
    re.compile(r"самоубий", re.IGNORECASE),
)

def _nonempty_lines(post: str) -> list[str]:
    return [ln.strip() for ln in post.splitlines() if ln.strip()]


def _has_badge_header(line: str) -> bool:
    """True только для плашек HERE WE GO / СРОЧНО / ОФИЦИАЛЬНО / ОТМЕНА."""
    return bool(_SENSATION_BADGE_RE.search(line.strip()))


def _has_self_harm_literal(post: str) -> bool:
    return any(p.search(post) for p in _SELF_HARM_PATTERNS)


def _extract_numbers(text: str) -> set[str]:
    found: set[str] = set()
    for match in re.findall(r"\d+(?:[.,]\d+)?", text):
        found.add(match.replace(",", "."))
    return found


def _find_caps_runs(post: str, *, skip_badge_line: bool = False) -> list[str]:
    """Капс-фразы > 3 слов подряд. HERE/WE/GO и строка-плашка не считаются."""
    badge_tokens = {"HERE", "WE", "GO"}
    runs: list[str] = []
    lines = _nonempty_lines(post)
    for i, line in enumerate(lines):
        if skip_badge_line and i == 0 and _has_badge_header(line):
            continue
        # goal_live: первая строка ГОООООЛ часто в капсе — не штрафуем
        if i == 0 and "гоо" in line.lower():
            continue
        words = re.findall(r"[A-Za-zА-Яа-яЁё0-9\-]+", line)
        run: list[str] = []
        for word in words:
            letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", word)
            if letters and letters == letters.upper():
                if word.upper() in badge_tokens:
                    if len(run) > 3:
                        runs.append(" ".join(run))
                    run = []
                    continue
                run.append(word)
            else:
                if len(run) > 3:
                    runs.append(" ".join(run))
                run = []
        if len(run) > 3:
            runs.append(" ".join(run))
    return runs


def _has_forbidden_signature(post: str) -> str | None:
    post_lower = post.lower()
    for sig in FORBIDDEN_SIGNATURES:
        if sig.lower() in post_lower:
            return sig
    return None


def check_form(
    post: str,
    fact: str,
    veracity: str,
    is_sensation: bool,
    archetype: str,
) -> list[str]:
    violations: list[str] = []
    lines = _nonempty_lines(post)
    limit = _LENGTH_LIMITS.get(archetype, 4)

    if len(lines) > limit:
        violations.append(
            f"soft: слишком длинный пост ({len(lines)} строк, лимит {limit})"
        )

    first_line = lines[0] if lines else ""
    has_badge = _has_badge_header(first_line)

    if archetype not in _NO_SENSATION_BADGE:
        if is_sensation and not has_badge:
            violations.append(
                "soft: is_sensation=true, но первая строка без плашки "
                "(💣 HERE WE GO / 🔥 СРОЧНО / ⚡️ ОФИЦИАЛЬНО / ❌ ОТМЕНА)"
            )
        if not is_sensation and has_badge:
            violations.append(
                "soft: is_sensation=false, но есть плашка-заголовок — рядовой пост без плашки"
            )

    post_lower = post.lower()
    if veracity != "verified":
        if "here we go" in post_lower:
            violations.append(
                "HARD: HERE WE GO! при veracity != verified — слух выдан за подтверждение"
            )
        if not any(marker in post_lower for marker in RUMOR_MARKERS):
            violations.append(
                "HARD: veracity != verified, но нет маркера непроверенности "
                "(источник / сообщают / пишет / инсайдер / по информации / Романо / Моретто)"
            )

    forbidden = _has_forbidden_signature(post)
    if forbidden:
        violations.append(
            f"HARD: подпись/ссылка на чужой канал («{forbidden}») — реклама конкурента"
        )

    if _has_self_harm_literal(post):
        violations.append(
            "HARD: дословное описание/угроза самоповреждения — переформулировать инфоповод "
            "без тиражирования акта"
        )

    caps_runs = _find_caps_runs(post, skip_badge_line=True)
    for run in caps_runs:
        violations.append(
            f"soft: капс-фраза > 3 слов подряд — капс должен быть точечным: «{run}»"
        )

    fact_nums = _extract_numbers(fact)
    post_nums = _extract_numbers(post)
    extra = post_nums - fact_nums
    if extra:
        violations.append(
            f"soft: числа в посте, которых нет во факте — проверить вручную: {sorted(extra)}"
        )

    return violations


def _is_hard(violation: str) -> bool:
    return violation.upper().startswith("HARD")


def _format_critic_tone_examples(archetype: str) -> str:
    examples = get_tone_examples(archetype, k=4)
    lines = [
        "Калибровочные эталоны планки тона:",
        "good = pass (эталон владельца); bad = revise (переюмор / канцелярит / запрещёнка).",
    ]
    for i, ex in enumerate(examples, 1):
        lines.append(f"\n--- Эталон {i} ({ex['archetype']}) ---")
        if ex.get("bad"):
            lines.append(f"BAD (revise):\n{ex['bad']}")
        lines.append(f"GOOD (pass):\n{ex['good']}")
        if ex.get("note"):
            lines.append(f"Правило: {ex['note']}")
    return "\n".join(lines)


def _llm_review(post: str, fact: str, veracity: str, archetype: str) -> dict:
    client = get_openai_client()
    system = (
        "Ты редактор футбольного канала с планкой: 80% информативная подача + 20% лёгкий подкол. "
        "Твоя задача — держать баланс, а НЕ максимизировать юмор. Ставь revise В ОБЕ стороны:\n"
        " - если пост ПРЕСНЫЙ (сухой пресс-релиз, канцелярит «новый игрок X», нет ни одной живой ноты) → revise;\n"
        " - если пост ПЕРЕЮМОРЕН (каскад шуток, вычурные метафоры типа «священное писание», "
        "«отдельная экономика», «рынок ограбили», клоунада, шутка ради шутки) → revise.\n"
        "Идеал: факты поданы чётко + ОДИН лёгкий акцент (подкол или открытый вопрос). "
        "Часто наблюдение «насколько оправдан — время покажет» лучше готовой шутки. "
        "НЕ каждый пост обязан шутить: трансфер может быть почти сухим, провокация/итог — резче. "
        "Капс должен быть точечным (1–2 слова), длинные капс-фразы помечай. "
        "Любая подпись на чужой канал — недопустима.\n\n"
        + _format_critic_tone_examples(archetype)
    )
    user = (
        f"Факт (источник правды):\n{fact}\n\n"
        f"Достоверность: {veracity}\n"
        f"Архетип: {archetype}\n\n"
        f"Пост для оценки:\n{post}"
    )

    response = client.responses.create(
        model=CRITIC_MODEL,
        instructions=system,
        input=user,
        reasoning={"effort": "none"},
        temperature=0.2,
        text={
            "format": {
                "type": "json_schema",
                "name": "critic_verdict",
                "strict": True,
                "schema": CRITIC_SCHEMA,
            }
        },
    )

    raw = getattr(response, "output_text", None) or ""
    if not raw:
        for item in response.output:
            if item.type == "message":
                for content in item.content:
                    if content.type == "output_text":
                        raw = content.text
                        break
    return json.loads(raw)


def review(
    post: str,
    fact: str,
    veracity: str,
    is_sensation: bool,
    archetype: str,
) -> dict:
    form_issues = check_form(post, fact, veracity, is_sensation, archetype)
    hard_issues = [v for v in form_issues if _is_hard(v)]
    soft_form_issues = [v for v in form_issues if not _is_hard(v)]

    llm = _llm_review(post, fact, veracity, archetype)

    issues = form_issues + llm.get("issues", [])
    how_to_improve = llm.get("how_to_improve", "")

    if hard_issues or llm.get("verdict") == "revise":
        verdict = "revise"
    else:
        verdict = "pass"

    return {
        "verdict": verdict,
        "issues": issues,
        "how_to_improve": how_to_improve,
        "hard_violations": hard_issues,
        "soft_form_issues": soft_form_issues,
        "llm_verdict": llm.get("verdict"),
    }
