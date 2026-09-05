"""Авто-тесты SPEC v3.1: медиа-роутер, schedule без LLM, запрет ЖФ-картинок."""

from __future__ import annotations

from src.ingest.media_router import media_source_allowed, resolve_media_plan
from src.ingest.schedule import is_schedule_post, schedule_phrase
from src.ingest.sources import SCHEDULE_PHRASES


def test_media_mapping() -> None:
    cases = [
        ("transfer", "zhfootballll", False, None, "yandex"),
        ("transfer", "thesoccerblogteam", True, "photo", "yandex"),
        ("news_opinion", "footballhourss", False, None, "yandex"),
        ("result", "thesoccerblogteam", True, "photo", "source"),
        ("result", "zhfootballll", True, "photo", "yandex"),  # не брать ЖФ
        ("schedule", "footballhourss", True, "photo", "source"),
        ("schedule", "zhfootballll", True, "photo", "none"),
        ("lineup", "championsleague365", True, "photo", "source"),
        ("lineup", "zhfootballll", True, "photo", "yandex"),
        ("goal_live", "footballhourss", True, "photo", "none"),
        ("goal_live", "zhfootballll", True, "video", "none"),
        ("goal_live", "thesoccerblogteam", False, None, "none"),
        ("meme", "footballhourss", True, "photo", "as_is"),
        ("meme", "championsleague365", True, "photo", "as_is"),
        ("meme", "championsleague365", True, "video", "as_is"),
        ("meme", "zhfootballll", True, "photo", "none"),
        ("meme", "footballhourss", False, None, "none"),
        ("video", "zhfootballll", True, "video", "as_is"),
        ("video", "thesoccerblogteam", True, "video", "as_is"),
        ("achievement", "any", False, None, "yandex"),
    ]
    for arch, src, has, kind, expect in cases:
        plan = resolve_media_plan(
            archetype=arch, source=src, has_source_media=has, media_kind=kind
        )
        assert plan.strategy == expect, (arch, src, plan.strategy, expect)
        print(f"OK map {arch}@{src} → {plan.strategy}")


def test_no_zh_images() -> None:
    # готовые картинки из ЖФ запрещены
    assert not media_source_allowed(
        strategy="source", source="zhfootballll", media_kind="photo"
    )
    assert not media_source_allowed(
        strategy="as_is", source="zhfootballll", media_kind="photo"
    )
    # видео из ЖФ ок
    assert media_source_allowed(
        strategy="as_is", source="zhfootballll", media_kind="video"
    )
    # non-ZH ок (в т.ч. CL365 для мема)
    assert media_source_allowed(
        strategy="source", source="thesoccerblogteam", media_kind="photo"
    )
    assert media_source_allowed(
        strategy="as_is", source="championsleague365", media_kind="photo"
    )
    print("OK ZH images blocked, video allowed, clean ok")


def test_schedule_no_llm() -> None:
    assert is_schedule_post("📊 Расписание матчей на сегодня", "footballhourss")
    assert is_schedule_post("🍿 Главные матчи дня", "footballhourss")
    assert is_schedule_post("📺 Расписание игрового дня", "footballhourss")
    assert not is_schedule_post("Расписание матчей", "zhfootballll")
    assert not is_schedule_post("Трансфер Месси", "footballhourss")

    # фразы только из списка
    seen = {schedule_phrase() for _ in range(40)}
    assert seen <= set(SCHEDULE_PHRASES)
    assert len(seen) >= 1
    print(f"OK schedule phrases sample={seen}")


if __name__ == "__main__":
    test_media_mapping()
    test_no_zh_images()
    test_schedule_no_llm()
    print("\nALL v3.1 AUTO TESTS PASSED")
