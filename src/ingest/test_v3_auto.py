"""Авто-тесты SPEC v3: фильтр, дедуп по отпечатку, live-лог."""

from __future__ import annotations

import time

from src.ingest.aliases import normalize_club, normalize_event, normalize_player
from src.ingest.dedup import find_duplicate, fingerprint_hash
from src.ingest.embed import cosine
from src.ingest.filter import check_garbage
from src.ingest import db


def test_filter_ads() -> None:
    samples = [
        ("Ставка дня! Кэф 2.15, промокод OLIMPBET — забрать бонус", "ad_marker"),
        ("Подписывайся на наш канал t.me/bestbets", "ad"),
        ("Доброе утро!", "greeting"),
        ("", "empty"),
        ("🔥", "no_football"),
    ]
    ok = (
        "💣 HERE WE GO! Энцо Фернандес — новый игрок Манчестер Сити. "
        "Сумма трансфера €146 млн."
    )
    assert check_garbage(ok) is None, check_garbage(ok)

    for text, expect_prefix in samples:
        reason = check_garbage(text)
        assert reason is not None, f"expected filter for: {text!r}"
        assert expect_prefix in reason or reason.startswith(expect_prefix), (text, reason)
        print(f"OK filtered: {reason!r} <- {text[:50]!r}")


def test_filter_real_ads_batch() -> None:
    ads = [
        "⚽️ ПРОГНОЗ НА МАТЧ! Ставка на победу, кэф 1.95. Промокод FOOTBALL",
        "Забери фрибет в 1xBet по ссылке",
        "Реклама. Букмекерская контора MELBET — бонус новым игрокам",
        "Подписывайтесь на наш канал, там эксклюзив каждый день",
        "OLIMPBET дарит бесплатную ставку — жми сюда",
        "Топовый кэф на тотал больше! Успей поставить",
        "Fonbet промокод на сегодня",
        "Переходи по ссылке t.me/stavki_pro и забери бонус",
        "Казино онлайн — крути барабан",
        "Прогноз на РПЛ: ставка ординар, кэф 2.30",
        "Наш паблик публикует экспрессы каждый вечер",
        "Betting tip of the day — freebet inside",
        "Спокойной ночи",
        "Добрый день друзья!!!",
        "🔥 Только сегодня промокод BET100",
    ]
    news = [
        "🇪🇸 Жезус – новый игрок «Барсы»\n💸 Сумма трансфера: €10 млн",
        "❌ ОТМЕНА HERE WE GO! Трансфер Камара в Челси сорвался",
        "⚡️ ГОООООЛ! Беллингем 19'\n🇪🇸 Реал 1:0 Малага 🇪🇸",
    ]
    failed = []
    for t in ads:
        r = check_garbage(t)
        if r is None:
            failed.append(t)
        else:
            print(f"AD OK [{r}]: {t[:60]}")
    assert not failed, failed
    for t in news:
        r = check_garbage(t)
        assert r is None, (t, r)
        print(f"NEWS OK: {t[:50]}")


def test_aliases() -> None:
    assert normalize_club("Барса") == "Барселона"
    assert normalize_club("FCB") == "Барселона"
    assert normalize_club("Сити") == "Манчестер Сити"
    assert normalize_player("Джуд Беллингем") == "Беллингем"
    ev = normalize_event(
        {
            "teams": ["Барса", "Реал"],
            "player": "Беллингем",
            "to_club": None,
            "score": "1-0",
            "minute": 19,
            "event_kind": "goal",
        }
    )
    assert "Барселона" in ev["teams"] and "Реал Мадрид" in ev["teams"]
    assert ev["score"] == "1:0"
    print(f"aliases OK event={ev}")


def _seed_raw(msg_id: int) -> int:
    rid = db.insert_raw(
        source="test_dedup",
        msg_id=msg_id,
        text="seed",
        ts=int(time.time()),
    )
    if rid is None:
        conn = db._connect()
        row = conn.execute(
            "SELECT id FROM raw_messages WHERE source=? AND msg_id=?",
            ("test_dedup", msg_id),
        ).fetchone()
        conn.close()
        return int(row["id"])
    return rid


def test_dedup_fingerprint_branches() -> None:
    """Критерий 3: (а) одна новость 2 формулировки; (б) гол ±5 мин; (в) разные голы / гол≠итог."""
    base = int(time.time()) % 1_000_000
    emb_a = [1.0] + [0.0] * 15
    emb_b = [0.95] + [0.05] + [0.0] * 14  # близкий, но для слоя 1 не нужен
    emb_far = [0.0, 1.0] + [0.0] * 14

    # (а) final_result Реал-Малага 10:2 — две формулировки → 1 факт + confirms
    ev_final = normalize_event(
        {
            "teams": ["Реал", "Малага"],
            "player": None,
            "to_club": None,
            "score": "10:2",
            "minute": None,
            "event_kind": "final_result",
        }
    )
    rid1 = _seed_raw(base + 1)
    fid_final = db.insert_fact(
        raw_msg_id=rid1,
        fact="Реал разгромил Малагу 10:2",
        archetype="result",
        veracity="verified",
        is_sensation=True,
        attribution=None,
        embedding=emb_a,
        event=ev_final,
        event_fingerprint=fingerprint_hash(ev_final),
    )
    hit = find_duplicate(
        event={
            "teams": ["Реал Мадрид", "Малага"],
            "player": None,
            "to_club": None,
            "score": "10:2",
            "minute": None,
            "event_kind": "final_result",
        },
        fact_text="Реал Мадрид победил Малагу со счётом 10:2, три победы подряд",
        embedding=emb_b,
    )
    assert hit is not None, "final_result duplicate not found"
    assert hit[0] == fid_final and hit[1] == "fingerprint"
    db.increment_confirms(fid_final)
    conn = db._connect()
    c = conn.execute("SELECT confirms_count FROM facts WHERE id=?", (fid_final,)).fetchone()
    conn.close()
    assert c["confirms_count"] >= 2
    print(f"(a) final_result OK fact={fid_final} confirms={c['confirms_count']} layer={hit[1]}")

    # (б) гол Беллингема 19/20/18 → один
    ev_goal19 = normalize_event(
        {
            "teams": ["Реал", "Малага"],
            "player": "Беллингем",
            "to_club": None,
            "score": "1:0",
            "minute": 19,
            "event_kind": "goal",
        }
    )
    rid2 = _seed_raw(base + 2)
    fid_goal = db.insert_fact(
        raw_msg_id=rid2,
        fact="Беллингем 19' Реал 1:0 Малага",
        archetype="goal_live",
        veracity="verified",
        is_sensation=False,
        attribution=None,
        embedding=emb_a,
        event=ev_goal19,
        event_fingerprint=fingerprint_hash(ev_goal19),
    )
    for minute in (20, 18):
        hit_g = find_duplicate(
            event={
                "teams": ["Реал Мадрид", "Малага"],
                "player": "Джуд Беллингем",
                "to_club": None,
                "score": "1:0",
                "minute": minute,
                "event_kind": "goal",
            },
            fact_text=f"ГОЛ Беллингем {minute}'",
            embedding=emb_b,
        )
        assert hit_g is not None and hit_g[0] == fid_goal, (minute, hit_g)
        assert hit_g[1] == "fingerprint"
        print(f"(b) goal minute={minute} -> same fact={fid_goal}")

    # (в) гол 31' — НЕ дубль
    hit_31 = find_duplicate(
        event={
            "teams": ["Реал", "Малага"],
            "player": "Беллингем",
            "to_club": None,
            "score": "2:0",
            "minute": 31,
            "event_kind": "goal",
        },
        fact_text="Беллингем 31' Реал 2:0",
        embedding=emb_far,
    )
    assert hit_31 is None or hit_31[0] != fid_goal, hit_31
    # score different + minute far — should be None for fingerprint; embedding far too
    assert hit_31 is None, hit_31
    print("(c1) goal 31' NOT duplicate of 19' OK")

    # (в) финальный итог и гол — НЕ дубли
    hit_cross = find_duplicate(
        event=ev_final,
        fact_text="Реал 10:2 Малага итог",
        embedding=emb_a,
    )
    # may match fid_final via fingerprint — but must NOT match fid_goal
    assert hit_cross is not None and hit_cross[0] == fid_final
    assert hit_cross[0] != fid_goal
    # reverse: looking for goal against final shouldn't collapse kinds
    hit_goal_vs_final = find_duplicate(
        event=ev_goal19,
        fact_text="гол",
        embedding=emb_a,
    )
    assert hit_goal_vs_final is not None and hit_goal_vs_final[0] == fid_goal
    print("(c2) goal vs final_result stay separate OK")

    # transfer: две формулировки
    ev_tr = normalize_event(
        {
            "teams": ["Тоттенхэм", "Трабзонспор"],
            "player": "Ришарлисон",
            "to_club": "Трабзонспор",
            "score": None,
            "minute": None,
            "event_kind": "transfer",
        }
    )
    rid3 = _seed_raw(base + 3)
    fid_tr = db.insert_fact(
        raw_msg_id=rid3,
        fact="Ришарлисон → Трабзонспор >€25млн",
        archetype="transfer",
        veracity="rumored",
        is_sensation=True,
        attribution="Романо",
        embedding=emb_a,
        event=ev_tr,
        event_fingerprint=fingerprint_hash(ev_tr),
    )
    hit_tr = find_duplicate(
        event={
            "teams": [],
            "player": "Richarlison",
            "to_club": "Trabzonspor",
            "score": None,
            "minute": None,
            "event_kind": "transfer",
        },
        fact_text="Ришарлисон близок к переходу в Трабзонспор",
        embedding=emb_far,  # даже далёкий эмбеддинг — слой 1
    )
    assert hit_tr is not None and hit_tr[0] == fid_tr and hit_tr[1] == "fingerprint"
    print(f"(a2) transfer fingerprint OK fact={fid_tr}")


def test_cosine_dedup() -> None:
    a = [1.0, 0.0, 0.0]
    b = [0.99, 0.1, 0.0]
    c = [0.0, 1.0, 0.0]
    assert cosine(a, a) > 0.99
    assert cosine(a, b) > 0.85
    assert cosine(a, c) < 0.2
    print(f"cosine identical={cosine(a,a):.3f} near={cosine(a,b):.3f} far={cosine(a,c):.3f}")


def test_live_log_write() -> None:
    raw_id = db.insert_raw(
        source="test_source",
        msg_id=999001,
        text="test transfer news",
        ts=1,
    )
    if raw_id is None:
        conn = db._connect()
        row = conn.execute(
            "SELECT id FROM raw_messages WHERE source=? AND msg_id=?",
            ("test_source", 999001),
        ).fetchone()
        conn.close()
        raw_id = int(row["id"])

    fid = db.insert_fact(
        raw_msg_id=raw_id,
        fact="Тестовый игрок перешёл в Тест-клуб за €1 млн",
        archetype="transfer",
        veracity="verified",
        is_sensation=False,
        attribution=None,
        embedding=[0.1, 0.2, 0.3],
        event={
            "teams": ["Тест-клуб"],
            "player": "Тестовый",
            "to_club": "Тест-клуб",
            "score": None,
            "minute": None,
            "event_kind": "transfer",
        },
    )
    gid = db.insert_generated(fact_id=fid, text="🇪🇸 Тест — новый игрок", guardrail_flag=None)
    db.set_generated_status(gid, "sent")
    db.log_live_decision(
        fact_id=fid,
        generated_id=gid,
        generated="🇪🇸 Тест — новый игрок",
        decision="accepted",
        edited_text=None,
        source="test_source",
        source_msg_link="https://t.me/test_source/999001",
        model="gpt-5.6-terra",
    )
    db.set_generated_status(gid, "decided")
    conn = db._connect()
    row = conn.execute(
        "SELECT decision, source FROM calibration_log_live WHERE generated_id=?",
        (gid,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["decision"] == "accepted"
    print(f"live log OK id_gen={gid} decision={row['decision']} source={row['source']}")


if __name__ == "__main__":
    test_filter_ads()
    test_filter_real_ads_batch()
    test_aliases()
    test_cosine_dedup()
    test_dedup_fingerprint_branches()
    test_live_log_write()
    print("\nALL AUTO TESTS PASSED")
