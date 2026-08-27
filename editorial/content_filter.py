# -*- coding: utf-8 -*-
"""Rule-based pre-filter отбора контента. Правила — editorial/rules_content.yaml."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.config import ROOT
from editorial.models import NewsItem

RULES_PATH = ROOT / "editorial" / "rules_content.yaml"

STOP = set("и в во на о об про с по за из от до у к the a an of to in on for and".split())


@dataclass
class Decision:
    take: bool
    tag: str
    note: str


class ContentFilter:
    def __init__(self, rules: dict):
        self.r = rules
        self.grands_ru = rules["grands_ru"]
        self.grands_eu = rules["grands_eu"]
        self.big_names = rules["big_names"]
        self.rpl_clubs = rules["rpl_clubs"]
        self.ru_export = rules["ru_export_names"]
        self.top_comps = rules["top_competitions"]
        self.money = rules["money_markers"]
        p = rules["patterns"]
        self.re = {k: re.compile(self._clean(v), re.I) for k, v in p.items()}
        self.params = rules["params"]
        self.rpl_exc = rules["rpl_exceptions"]

    @staticmethod
    def _clean(pat: str) -> str:
        pat = re.sub(r"\s*\n\s*", "", pat).strip()
        return re.sub(r"\s*\|\s*", "|", pat)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "ContentFilter":
        with open(path or RULES_PATH, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    def _has(self, text: str, arr) -> bool:
        t = text.lower()
        return any(k.lower() in t for k in arr)

    def _m(self, key: str, text: str) -> bool:
        return bool(self.re[key].search(text))

    def _narrative(self, ti: str) -> bool:
        return (
            self._m("narrative_achievement", ti)
            or self._m("narrative_sensation", ti)
            or self._m("narrative_drama", ti)
            or self._m("narrative_individual", ti)
            or self._m("narrative_trophy", ti)
        )

    def _is_rpl(self, it) -> bool:
        t = it["title"] + " " + it.get("body", "")
        if any(m in t for m in ("РПЛ", "Кубок России", "FONBET", "Мир РПЛ")):
            return True
        if (
            it.get("lang") == "ru"
            and any(c in t for c in self.rpl_clubs)
            and not self._has(t, self.grands_eu)
            and not self._has(t, self.big_names)
        ):
            return True
        return False

    def _rpl_pass(self, it):
        t = it["title"] + " " + it.get("body", "")
        ti = it["title"]
        if (
            self.rpl_exc.get("referee_incident")
            and re.search(
                r"удал[её]н|красн(ая|ую) карт|отмен(ил|ён|ен).*(гол|пенальти)|потасовк|драк|стычк",
                ti,
                re.I,
            )
            and self._has(ti, self.grands_ru)
        ):
            return True, "РПЛ: судейство/инцидент в топ-матче"
        if (
            self.rpl_exc.get("ru_player_to_europe")
            and self._m("transfer", t)
            and self._has(t, self.ru_export)
            and self._has(
                t,
                self.grands_eu
                + [
                    "Галатасар",
                    "Порту",
                    "Бенфик",
                    "серию А",
                    "Бундеслиг",
                    "АПЛ",
                    "Ла Лиг",
                    "Европ",
                ],
            )
        ):
            return True, "РПЛ: переход РФ игрока в Европу"
        if (
            self.rpl_exc.get("top_match_narrative")
            and it.get("event_type_guess") == "match_result"
            and sum(1 for g in self.grands_ru if g in ti) >= self.params["rpl_topmatch_min_grands"]
            and self._narrative(ti)
        ):
            return True, "РПЛ: топ-матч грандов с нарративом"
        if (
            self.rpl_exc.get("cup_semifinal_plus")
            and re.search(r"1/2 финала|полуфинал|финал", ti, re.I)
            and "Кубок России" in t
        ):
            return True, "РПЛ: Кубок России полуфинал+"
        return False, "РПЛ: вне критериев"

    def _reaction_only(self, it) -> bool:
        return self._m("reaction", it["title"])

    def _bright_quote(self, it) -> bool:
        ti = it["title"]
        return bool(re.search(r"«[^»]{12,}»", ti)) and self._m("bright_quote_flavor", ti)

    def _human(self, it) -> bool:
        return it.get("event_type_guess") == "lifestyle" or self._m(
            "human_factor", it["title"] + " " + it.get("body", "")
        )

    def _big_transfer(self, it) -> bool:
        t = it["title"] + " " + it.get("body", "")
        return self._m("transfer", t) and (
            self._has(t, self.grands_eu + self.big_names) or self._has(t, self.money)
        )

    def decide(self, it: dict[str, Any], *, allow_rumors: bool = False) -> Decision:
        t = it["title"] + " " + it.get("body", "")
        ti = it["title"]
        etype = it.get("event_type_guess") or "other"

        if self._m("service", ti) or len(ti.strip()) < 8:
            return Decision(False, "reject", "служебная сводка/трансляция")

        if self._human(it) and not self._reaction_only(it):
            return Decision(True, "human_factor", "человеческий фактор/юмор")

        if self._reaction_only(it) and not self._bright_quote(it) and not self._m("event_verb", ti):
            return Decision(False, "reject", "вторичная реакция/комментарий")

        if self._is_rpl(it):
            ok, note = self._rpl_pass(it)
            return Decision(ok, "rpl_exception" if ok else "reject", note)

        if etype == "rumor" and not allow_rumors:
            return Decision(False, "reject", "слух (allow_rumors=false)")

        if etype == "match_result":
            if self._narrative(ti):
                return Decision(True, "match_narrative", "нарратив матча")
            if self._m("bare_result", ti):
                return Decision(False, "reject", "голый счёт → контур результатов")

        if self._m("friendly", ti) and not self._narrative(ti):
            return Decision(False, "reject", "товарищеский/контрольный матч")

        if self._big_transfer(it):
            return Decision(True, "transfer_money", "трансфер/деньги топ-клуба или имени")

        if self._has(t, self.grands_eu + self.big_names) or self._has(t, self.top_comps):
            if etype in ("match_result", "injury", "official_statement", "transfer", "other", "lineup"):
                if self._reaction_only(it) and not self._m("event_verb", ti) and not self._bright_quote(it):
                    return Decision(False, "reject", "вторичная реакция/комментарий")
                return Decision(True, "top_name", "топ-клуб/имя/турнир")
            return Decision(False, "reject", "без инфоповода")

        if self._bright_quote(it):
            return Decision(True, "bright_quote", "яркая цитата")

        return Decision(False, "reject", "без топ-инфоповода")

    def _entset(self, ti: str):
        pool = self.grands_ru + self.grands_eu + self.big_names + self.ru_export
        return frozenset(n.lower()[:5] for n in pool if n.lower() in ti.lower())

    def _subtok(self, ti: str):
        return set(
            w
            for w in re.sub(r"[^\w€]+", " ", ti.lower()).split()
            if w not in STOP and len(w) > 3
        )

    def decide_batch(self, items: list, *, allow_rumors: bool = False) -> list:
        for it in items:
            dec = self.decide(it, allow_rumors=allow_rumors)
            it["model_take"], it["model_tag"], it["note"] = dec.take, dec.tag, dec.note

        groups = defaultdict(list)
        for it in items:
            if not it["model_take"]:
                continue
            e = self._entset(it["title"])
            if not e:
                continue
            groups[(it.get("event_type_guess") or "other", e)].append(it)

        for _, g in groups.items():
            if len(g) < 2:
                continue
            g.sort(key=lambda it: it.get("published_at") or "")
            kept = [g[0]]
            for it in g[1:]:
                base = set().union(*[self._subtok(k["title"]) for k in kept])
                novel = len(self._subtok(it["title"]) - base)
                if self._m("addition", it["title"]) and novel >= self.params["dedup_min_novel_tokens"]:
                    it["note"], it["model_tag"] = "дополнение к событию", "addition"
                    kept.append(it)
                else:
                    it["model_take"], it["model_tag"], it["note"] = False, "reject", "повтор события"

        trues = [it for it in items if it["model_take"]]
        hf = [it for it in trues if it["model_tag"] == "human_factor"]
        cap = int(self.params["human_factor_cap"] * len(trues)) if trues else 0
        if len(hf) > cap:
            def rank(it):
                ti = it["title"]
                return (
                    self._has(ti, self.big_names + self.grands_eu),
                    self._has(ti, ["свадьб", "мем", "рекорд"] + self.money),
                )

            for it in sorted(hf, key=rank, reverse=True)[cap:]:
                it["model_take"], it["model_tag"] = False, "reject"
                it["note"] = "человеческий фактор (срез по лимиту 40%)"

        return items


def news_to_item(item: NewsItem | dict[str, Any]) -> dict[str, Any]:
    if isinstance(item, dict):
        return {
            "title": item.get("title") or "",
            "body": item.get("body") or "",
            "lang": item.get("lang") or "ru",
            "event_type_guess": item.get("event_type") or item.get("event_type_guess") or "other",
            "published_at": str(item.get("published_at") or ""),
        }
    published = ""
    if item.published_at:
        published = item.published_at.isoformat()
    return {
        "title": item.title,
        "body": item.body or "",
        "lang": item.lang or "ru",
        "event_type_guess": item.event_type or "other",
        "published_at": published,
    }


@lru_cache
def load_content_filter() -> ContentFilter:
    return ContentFilter.load(RULES_PATH)


def reload_content_filter() -> None:
    load_content_filter.cache_clear()


if __name__ == "__main__":
    import json
    import sys
    from collections import Counter

    cf = ContentFilter.load()
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    items = data["items"] if isinstance(data, dict) else data
    cf.decide_batch(items)
    take = sum(1 for it in items if it["model_take"])
    print(f"take {take}/{len(items)} ({100 * take / len(items):.1f}%)")
    print(dict(Counter(it["model_tag"] for it in items if it["model_take"]).most_common()))
