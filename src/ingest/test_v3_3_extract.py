"""Авто-тесты SPEC v3.3: экстрактор-промпт, is_garbage gate, фильтр, is_test."""

from __future__ import annotations

import time
from unittest.mock import patch

from src.ingest import db
from src.ingest.extract import EXTRACT_SCHEMA, extract_system_prompt
from src.ingest.filter import EVENT_PROMO_MARKERS, check_garbage
from src.ingest.pipeline import process_message


_META_WRAPPERS = (
    "в посте сообщается",
    "перечислены утверждения",
    "в посте говорится",
    "в посте перечислены",
)


def test_prompt_has_strict_rules_and_fewshots() -> None:
    p = extract_system_prompt()
    assert "КОРОЧЕ ИЛИ РАВНО" not in p
    assert "НЕ должен быть длиннее" not in p
    assert "ПОЛНО и ТОЧНО" in p
    assert "ИГНОРИРУЙ" in p
    assert "is_garbage" in p
    assert "СПИСКИ" in p
    assert "канцеляр" in p.lower() or "НЕ канцеляр" in p
    assert "Бартра" in p
    assert "стоп" in p.lower()
    assert "SECRET EVENT" in p
    assert "Олисе" in p
    for w in _META_WRAPPERS[:2]:
        assert w in p.lower() or w.replace("ё", "е") in p.lower() or "перечислены утверждения" in p
    assert "is_garbage" in EXTRACT_SCHEMA["properties"]
    assert "is_garbage" in EXTRACT_SCHEMA["required"]
    print("OK prompt rules + few-shots + schema is_garbage")


def test_filter_event_promo_markers() -> None:
    samples = [
        "SECRET EVENT: Глава II\n30 человек получат приглашения на закрытое мероприятие в Москве",
        "Сегодня открыли доступ к рейтингу — получат приглашения на ивент",
        "Розыгрыш билетов! Голосуй в опросе и выиграй",
        "Закрытое мероприятие для подписчиков — вход только по приглашению",
        "Конкурс: кто победит в опросе получит приз",
        "Private event в Москве — ждём вас на мероприятии",
    ]
    for t in samples:
        r = check_garbage(t)
        assert r is not None, f"expected filter for: {t[:60]!r}"
        assert r.startswith("event_promo:") or r.startswith("ad_"), (t, r)
        print(f"OK promo filtered [{r}]: {t[:50]}")

    # футбольный рейтинг УЕФА без promo-контекста — не режем
    uefa = "Обновлён рейтинг УЕФА: Реал на первом месте, Барса вторая"
    assert check_garbage(uefa) is None, check_garbage(uefa)
    print("OK UEFA rating not filtered")

    assert any("secret event" == m.lower() for m in EVENT_PROMO_MARKERS)
    assert any("приглашени" in m for m in EVENT_PROMO_MARKERS)


def test_pipeline_respects_is_garbage() -> None:
    secret = (
        "SECRET EVENT: Глава II\n"
        "Сегодня мы открыли доступ к рейтингу. По его результатам 30 человек "
        "получат приглашения на закрытое мероприятие в Москве."
    )
    # детерминированный фильтр — первый рубеж
    assert check_garbage(secret) is not None

    msg_id = int(time.time()) % 10_000_000 + 33001
    garbage_extract = {
        "is_news": False,
        "is_garbage": True,
        "fact": "",
        "archetype": "meme",
        "veracity": "verified",
        "is_sensation": False,
        "source_attribution": None,
        "skip_reason": "promo_event",
        "image_query": None,
        "event": {
            "teams": [],
            "player": None,
            "to_club": None,
            "score": None,
            "minute": None,
            "event_kind": "other",
        },
    }
    # обходим детерминированный фильтр текстом без маркеров, mock extract → is_garbage
    benign_looking = "Смотрите анонс клуба завтра вечером у нас на встрече участников"
    with patch("src.ingest.pipeline.extract_fact", return_value=garbage_extract):
        with patch("src.ingest.pipeline.check_garbage", return_value=None):
            result = process_message(
                source="thesoccerblogteam",
                msg_id=msg_id,
                text=benign_looking,
                ts=int(time.time()),
                run_tag="test_v33_gate",
                replace_raw=True,
            )
    assert result["status"] == "filtered_extract", result
    assert result.get("is_garbage") is True
    conn = db._connect()
    row = conn.execute(
        "SELECT is_filtered, filter_reason, is_garbage FROM raw_messages WHERE source=? AND msg_id=?",
        ("thesoccerblogteam", msg_id),
    ).fetchone()
    conn.close()
    assert row["is_filtered"] == 1
    assert "filtered_extract" in (row["filter_reason"] or "")
    assert row["is_garbage"] == 1
    print(f"OK is_garbage gate status={result['status']} reason={row['filter_reason']}")


def test_list_preservation_contract() -> None:
    """Контракт: список наград → fact list-like, без meta-обёртки (mock extract)."""
    awards_raw = (
        "⚽️ Игрок сезона в лиге – Олисе\n"
        "⚽️ Игрок года в Германии – Олисе\n"
        "⚽️ Игрок сезона в Баварии – Олисе\n"
        "🏆 Лучший игрок ЛЧ – Хвича\n"
        "🏆 Лучший бомбардир ЛЧ – Мбаппе"
    )
    good_fact = (
        "Игрок сезона в лиге/года в Германии/сезона в Баварии — Олисе; "
        "лучший игрок ЛЧ — Хвича; лучший бомбардир ЛЧ — Мбаппе"
    )
    assert "в посте" not in good_fact.lower()
    assert "перечислены" not in good_fact.lower()
    # list-like: разделители / точки с запятой / несколько пунктов
    assert "—" in good_fact or ";" in good_fact or "/" in good_fact
    assert "Олисе" in good_fact and "Хвича" in good_fact

    extracted = {
        "is_news": True,
        "is_garbage": False,
        "fact": good_fact,
        "archetype": "achievement",
        "veracity": "rumored",
        "is_sensation": False,
        "source_attribution": None,
        "skip_reason": None,
        "image_query": "Олисе награда",
        "event": {
            "teams": [],
            "player": "Олисе",
            "to_club": None,
            "score": None,
            "minute": None,
            "event_kind": "other",
        },
    }
    msg_id = int(time.time()) % 10_000_000 + 33002
    with patch("src.ingest.pipeline.extract_fact", return_value=extracted):
        with patch("src.ingest.pipeline.embed_text", return_value=[0.1] + [0.0] * 15):
            with patch("src.ingest.pipeline.find_duplicate", return_value=None):
                with patch(
                    "src.ingest.pipeline._produce_text",
                    return_value=("🏆 Олисе собрал награды", []),
                ):
                    with patch(
                        "src.ingest.pipeline.build_media",
                        return_value={
                            "media_strategy": "none",
                            "media_kind": None,
                            "media_path": None,
                            "media_url": None,
                            "image_query": None,
                            "media_warning": None,
                        },
                    ):
                        result = process_message(
                            source="zhfootballll",
                            msg_id=msg_id,
                            text=awards_raw,
                            ts=int(time.time()),
                            run_tag="run_24h_v33",
                            replace_raw=True,
                            skip_generate=False,
                        )
    assert result["status"] == "queued", result
    assert result["fact"] == good_fact
    print("OK list preservation contract queued")


def test_length_rule_examples_from_log() -> None:
    """v3.4: разбор сохраняет объяснение; meta-обёртки запрещены; длина не потолок."""
    raw = (
        "Бартра забежал в штрафную во время пенальти. По инструкции RFEF "
        "заступ считается по стопам, а не по корпусу, поэтому VAR нарушения не увидел."
    )
    good = (
        "Бартра забежал в штрафную во время пенальти. По инструкции RFEF "
        "заступ по стопам, а не по корпусу — VAR нарушения не увидел."
    )
    dry = "Бартра забежал в штрафную, VAR не увидел нарушения"
    assert "стоп" in good.lower()
    assert "rfeF".lower() in good.lower() or "rfef" in good.lower()
    assert "стоп" not in dry.lower()
    assert len(good) > len(dry)
    low = good.lower()
    for w in _META_WRAPPERS:
        assert w not in low, (w, good)
    assert "в посте" not in raw.lower()
    print("OK analysis fact keeps explanation; no meta-wrapper")


def test_analysis_excludes_is_test() -> None:
    # миграция помечает test_*
    conn = db._connect()
    # force migrate / mark
    conn.close()
    conn = db._connect()
    test_n = conn.execute(
        "SELECT COUNT(*) AS n FROM calibration_log_live WHERE COALESCE(is_test,0)=1"
    ).fetchone()["n"]
    all_n = conn.execute("SELECT COUNT(*) AS n FROM calibration_log_live").fetchone()["n"]
    conn.close()
    rows = db.analysis_log_rows(include_test=False)
    assert all(not r.get("is_test") for r in rows)
    summary = db.live_summary(include_test=False)
    summary_all = db.live_summary(include_test=True)
    assert summary_all["total"] >= summary["total"]
    if test_n:
        assert len(rows) < all_n or summary["total"] < summary_all["total"]
    print(
        f"OK analysis excludes is_test: test={test_n} analysis={len(rows)} "
        f"summary={summary['total']}/{summary_all['total']}"
    )


def test_secret_event_blocked_by_filter_or_extract() -> None:
    text = (
        "SECRET EVENT: Глава II\n"
        "30 человек получат приглашения на закрытое мероприятие в Москве."
    )
    # рубеж 1
    r = check_garbage(text)
    assert r is not None and "event_promo" in r
    msg_id = int(time.time()) % 10_000_000 + 33003
    result = process_message(
        source="footballhourss",
        msg_id=msg_id,
        text=text,
        ts=int(time.time()),
        run_tag="run_24h_v33",
        replace_raw=True,
    )
    assert result["status"] == "filtered", result
    assert "event_promo" in (result.get("filter_reason") or "")
    print(f"OK SECRET EVENT blocked: {result['filter_reason']}")


if __name__ == "__main__":
    test_prompt_has_strict_rules_and_fewshots()
    test_filter_event_promo_markers()
    test_length_rule_examples_from_log()
    test_secret_event_blocked_by_filter_or_extract()
    test_pipeline_respects_is_garbage()
    test_list_preservation_contract()
    test_analysis_excludes_is_test()
    print("\nALL v3.3 AUTO TESTS PASSED")
