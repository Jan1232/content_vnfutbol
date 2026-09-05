"""Авто-тесты SPEC v3.2: goal_live без медиа, duplicate_of в логе."""

from __future__ import annotations

import time

from src.ingest import db
from src.ingest.media import build_media
from src.ingest.media_router import resolve_media_plan


def test_goal_live_no_media() -> None:
    for src in ("footballhourss", "zhfootballll", "thesoccerblogteam"):
        plan = resolve_media_plan(
            archetype="goal_live",
            source=src,
            has_source_media=True,
            media_kind="photo",
        )
        assert plan.strategy == "none", (src, plan)
    media = build_media(
        archetype="goal_live",
        source="footballhourss",
        source_media_path="/tmp/fake.jpg",
        media_kind="photo",
        image_query="Беллингем гол",
        dest_stem="test_goal",
    )
    assert media["media_strategy"] == "none"
    assert media["media_path"] is None
    print("OK goal_live → none, build_media skipped search")


def test_log_decisions_fields() -> None:
    ts = int(time.time())
    rid = db.insert_raw(
        source="test_v32",
        msg_id=ts % 10_000_000,
        text="raw goal text",
        ts=ts,
    )
    assert rid
    fid = db.insert_fact(
        raw_msg_id=rid,
        fact="Беллингем 19' Реал 1:0",
        archetype="goal_live",
        veracity="verified",
        is_sensation=False,
        attribution=None,
        embedding=[0.1, 0.0, 0.0],
        event={
            "teams": ["Реал Мадрид", "Малага"],
            "player": "Беллингем",
            "to_club": None,
            "score": "1:0",
            "minute": 19,
            "event_kind": "goal",
        },
        image_query=None,
    )
    gid = db.insert_generated(
        fact_id=fid,
        text="⚡️ ГООЛ",
        guardrail_flag=None,
        media_strategy="none",
        run_tag="run_24h",
    )
    db.register_run_24h(
        news_id=gid,
        generated_id=gid,
        fact_id=fid,
        source="test_v32",
        msg_id=ts % 10_000_000,
        raw_text="raw goal text",
        fact="Беллингем 19' Реал 1:0",
        event={"event_kind": "goal", "teams": ["Реал Мадрид", "Малага"], "player": "Беллингем", "score": "1:0", "minute": 19},
        image_query=None,
        archetype="goal_live",
        media_strategy="none",
    )
    assert db.news_id_exists(gid)
    assert not db.news_id_exists(999999991)

    # accepted
    db.log_live_decision(
        fact_id=fid,
        generated_id=gid,
        generated="⚡️ ГООЛ",
        decision="accepted",
        edited_text=None,
        source="test_v32",
        source_msg_link=None,
        model="test",
        eval_scope="skip_media",
        news_id=gid,
        archetype_final="goal_live",
    )
    # duplicate на синтетическом втором
    gid2 = db.insert_generated(
        fact_id=fid, text="повтор", guardrail_flag=None, run_tag="run_24h"
    )
    db.register_run_24h(
        news_id=gid2,
        generated_id=gid2,
        fact_id=fid,
        source="test_v32",
        msg_id=1,
        raw_text="dup",
        fact="same",
        event={},
        image_query=None,
        archetype="goal_live",
        media_strategy="none",
    )
    db.log_live_decision(
        fact_id=fid,
        generated_id=gid2,
        generated="повтор",
        decision="duplicate",
        edited_text=None,
        source="test_v32",
        source_msg_link=None,
        model="test",
        eval_scope="skip_media",
        news_id=gid2,
        duplicate_of=gid,
        archetype_final="goal_live",
    )
    conn = db._connect()
    row = conn.execute(
        "SELECT decision, duplicate_of, news_id, eval_scope FROM calibration_log_live WHERE generated_id=?",
        (gid2,),
    ).fetchone()
    r24 = conn.execute(
        "SELECT decision, duplicate_of FROM run_24h WHERE news_id=?", (gid2,)
    ).fetchone()
    conn.close()
    assert row["decision"] == "duplicate"
    assert row["duplicate_of"] == gid
    assert row["news_id"] == gid2
    assert r24["duplicate_of"] == gid
    print(f"OK log duplicate news={gid2} of={gid} eval={row['eval_scope']}")


if __name__ == "__main__":
    test_goal_live_no_media()
    test_log_decisions_fields()
    print("\nALL v3.2 AUTO TESTS PASSED")
