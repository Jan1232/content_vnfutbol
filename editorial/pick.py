"""Editorial pick: content_filter (YAML) + cap/дедуп + опциональный LLM."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from editorial.content_filter import load_content_filter, news_to_item
from editorial.models import NewsItem
from editorial.policy import HUMAN_FACTOR_CAP, PICK_TAGS, POLICY_RULES


@dataclass(frozen=True)
class PickVerdict:
    take: bool
    tag: str
    reason: str
    by: str  # filter | llm | cap | cluster

    def as_dict(self) -> dict[str, Any]:
        return {"take": self.take, "tag": self.tag, "reason": self.reason, "by": self.by}


def pick_tag_of(item: dict[str, Any]) -> str:
    ent = item.get("entities")
    if not isinstance(ent, dict):
        try:
            ent = json.loads(item.get("entities_json") or "{}")
        except Exception:
            ent = {}
    pick = ent.get("pick") if isinstance(ent.get("pick"), dict) else {}
    tag = str(pick.get("tag") or "")
    return tag if tag in PICK_TAGS else tag


def human_factor_share(published: list[dict[str, Any]]) -> float:
    if not published:
        return 0.0
    n = sum(1 for row in published if pick_tag_of(row) == "human_factor")
    return n / len(published)


def _signals(item: NewsItem) -> dict[str, Any]:
    ent = item.entities or {}
    return {
        "event_type": item.event_type or "other",
        "competition": item.competition or "",
        "teams": list(ent.get("teams") or [])[:6],
        "players": list(ent.get("players") or [])[:6],
        "policy": POLICY_RULES,
    }


def _from_decision(take: bool, tag: str, reason: str, by: str) -> PickVerdict:
    if tag not in PICK_TAGS:
        tag = "top_name" if take else "reject"
    if not take:
        tag = "reject"
    return PickVerdict(take, tag, reason, by)


def rule_reject(item: NewsItem | dict[str, Any], *, allow_rumors: bool = False) -> PickVerdict | None:
    """Жёсткий отсев служебного/слухов. None = не служебный мусор (решение за полным фильтром)."""
    cf = load_content_filter()
    row = news_to_item(item)
    if cf._m("service", row["title"]) or len((row["title"] or "").strip()) < 8:
        return _from_decision(False, "reject", "служебная сводка/трансляция", "filter")
    if not allow_rumors and row.get("event_type_guess") == "rumor":
        return _from_decision(False, "reject", "слух (allow_rumors=false)", "filter")
    return None


def pick_offline(
    item: NewsItem,
    *,
    allow_rumors: bool = False,
    cluster_already_published: bool = False,
    human_factor_ratio: float = 0.0,
) -> PickVerdict:
    from editorial.content_blocks import is_content_blocked

    blocked, breason = is_content_blocked(item)
    if blocked:
        return _from_decision(False, "reject", f"content block: {breason}", "block")

    cf = load_content_filter()
    row = news_to_item(item)
    title = row["title"]

    if cluster_already_published:
        if cf._m("addition", title):
            return _from_decision(True, "addition", "дополнение к событию", "cluster")
        return _from_decision(False, "reject", "повтор события", "cluster")

    dec = cf.decide(row, allow_rumors=allow_rumors)
    if not dec.take:
        return _from_decision(False, "reject", dec.note, "filter")

    if dec.tag == "human_factor" and human_factor_ratio >= HUMAN_FACTOR_CAP:
        return _from_decision(
            False,
            "reject",
            f"human-factor cap ({human_factor_ratio:.0%}≥{HUMAN_FACTOR_CAP:.0%})",
            "cap",
        )
    return _from_decision(True, dec.tag, dec.note, "filter")


def score_pool(items: list[NewsItem], *, allow_rumors: bool = False) -> list[PickVerdict]:
    cf = load_content_filter()
    rows = [news_to_item(it) for it in items]
    cf.decide_batch(rows, allow_rumors=allow_rumors)
    return [
        _from_decision(bool(r["model_take"]), str(r["model_tag"]), str(r["note"]), "filter")
        for r in rows
    ]


def pick(
    item: NewsItem,
    *,
    allow_rumors: bool = False,
    cluster_already_published: bool = False,
    human_factor_ratio: float = 0.0,
    use_llm: bool = True,
) -> PickVerdict:
    verdict = pick_offline(
        item,
        allow_rumors=allow_rumors,
        cluster_already_published=cluster_already_published,
        human_factor_ratio=human_factor_ratio,
    )
    if not verdict.take:
        return verdict
    if not use_llm:
        return verdict

    from editorial import llm

    try:
        payload = llm.pick_news(
            item.title,
            item.body,
            signals=_signals(item),
            cluster_already_published=cluster_already_published,
            human_factor_share=human_factor_ratio,
        )
    except Exception:
        return verdict

    take = bool(payload.get("take"))
    tag = str(payload.get("tag") or verdict.tag)
    reason = str(payload.get("reason") or verdict.reason)[:400]
    if not take:
        return _from_decision(False, "reject", reason, "llm")
    if tag == "human_factor" and human_factor_ratio >= HUMAN_FACTOR_CAP:
        return _from_decision(
            False,
            "reject",
            f"human-factor cap ({human_factor_ratio:.0%}≥{HUMAN_FACTOR_CAP:.0%})",
            "cap",
        )
    if cluster_already_published and tag != "addition":
        return _from_decision(False, "reject", "повтор события", "cluster")
    return _from_decision(True, tag, reason, "llm")


def story_throttle_ok(channel_slug: str, item: NewsItem) -> tuple[bool, str]:
    """Анти-дубли сюжетов поверх pick. False → deferred."""
    try:
        from editorial.story_throttle import story_gate

        ok, reason, _key, _rank = story_gate(channel_slug, item)
        return ok, reason
    except Exception as e:
        print(f"[editorial] story_throttle skip: {e}", flush=True)
        return True, "ok"
