"""Авто-тесты SPEC v3.4: без сушки, метаданные канала, типографика."""

from __future__ import annotations

from src.generate.dna import FORMAT_DNA
from src.generate.fan import VECTOR_DRY, _FORMAT_REF, _build_user
from src.generate.html_post import format_post_html
from src.generate.producer import _SYSTEM_TEMPLATE
from src.ingest.clean import clean_source_body, strip_source_leak_from_fact
from src.ingest.extract import extract_system_prompt


def test_extractor_prompt_v34() -> None:
    p = extract_system_prompt()
    assert "КОРОЧЕ ИЛИ РАВНО" not in p
    assert "НЕ должен быть длиннее" not in p
    assert "ПОЛНО и ТОЧНО" in p
    assert "ИГНОРИРУЙ название канала" in p or "ИГНОРИРУЙ" in p
    assert "Лига Чемпионов" in p
    assert "стоп" in p.lower()
    print("OK extractor prompt v3.4")


def test_clean_channel_header_diomande() -> None:
    raw = (
        "Лига Чемпионов\n"
        "Ян Диоманде в Реал Мадриде: 5 игр, 5 матчей, 5 выходов"
    )
    body = clean_source_body(raw, "championsleague365")
    assert "диоманде" in body.lower()
    assert "5 игр" in body
    assert "лига чемпионов" not in body.lower()
    print(f"OK clean header: {body!r}")


def test_clean_forward_and_signature() -> None:
    raw = (
        "Forwarded from Лига Чемпионов\n"
        "Ян Диоманде в Реал Мадриде: 5 игр\n"
        "@championsleague365\n"
        "t.me/championsleague365"
    )
    body = clean_source_body(raw, "championsleague365")
    assert body == "Ян Диоманде в Реал Мадриде: 5 игр"
    zh = clean_source_body("ЖИЗНЬ ФУТБОЛ:\nБартра забежал в штрафную", "zhfootballll")
    assert zh == "Бартра забежал в штрафную"
    print("OK clean forward/signature")


def test_keep_ucl_if_in_body() -> None:
    raw = (
        "«Ливерпуль» не включил Кьезу в заявку на общий этап Лиги чемпионов."
    )
    body = clean_source_body(raw, "championsleague365")
    assert "лиги чемпионов" in body.lower()
    fact = strip_source_leak_from_fact(
        "Ливерпуль не включил Кьезу в заявку на общий этап Лиги чемпионов",
        body,
        "championsleague365",
    )
    assert "лиги чемпионов" in fact.lower()
    print("OK keep UCL when it is the news")


def test_strip_leaked_channel_title() -> None:
    body = "Ян Диоманде в Реал Мадриде: 5 игр, 5 матчей, 5 выходов"
    leaked = "Ян Диоманде в Реале в Лиге чемпионов: 5 игр — 5 выходов"
    fixed = strip_source_leak_from_fact(leaked, body, "championsleague365")
    assert "лиге чемпионов" not in fixed.lower()
    assert "диоманде" in fixed.lower()
    assert "5 игр" in fixed
    print(f"OK strip leak: {fixed!r}")


def test_producer_no_hard_shorten_has_typography() -> None:
    dna = FORMAT_DNA
    assert "1–4 строки" not in dna
    assert "•" in dna
    assert "`> `" in dna or "строка начинается с `>`" in dna
    assert "минимум слов" not in VECTOR_DRY
    assert "•" in _FORMAT_REF
    user = _build_user("факт", "verified", "news_opinion", False, "вектор")
    assert "•" in user
    assert ">" in user
    prod = _SYSTEM_TEMPLATE
    assert "сожми" not in prod.lower()
    assert "сократи" not in prod.lower()
    assert "`• `" in prod
    print("OK producer typography / no hard shorten")


def test_blockquote_html() -> None:
    post = (
        "Родри перед ЧМ:\n"
        "> «Переход в Реал? Нельзя отказывать лучшим клубам мира»\n"
        "Родри после матча за Барсу:\n"
        "> «Я всегда хотел в Барсу»"
    )
    html = format_post_html(post)
    assert html.count("<blockquote>") == 2
    assert html.count("</blockquote>") == 2
    assert "&gt;" not in html
    assert "Родри перед ЧМ:" in html
    assert "Переход в Реал" in html
    bullets = format_post_html("• 5 игр\n• 5 матчей")
    assert "• 5 игр" in bullets
    assert "<blockquote>" not in bullets
    print("OK blockquote HTML + bullets passthrough")


if __name__ == "__main__":
    test_extractor_prompt_v34()
    test_clean_channel_header_diomande()
    test_clean_forward_and_signature()
    test_keep_ucl_if_in_body()
    test_strip_leaked_channel_title()
    test_producer_no_hard_shorten_has_typography()
    test_blockquote_html()
    print("\nALL v3.4 AUTO TESTS PASSED")
