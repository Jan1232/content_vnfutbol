"""Фильтр мата только для SoccerBlog → ВСЕ НА ФУТБОЛ."""

from __future__ import annotations

from app.filter import contains_profanity, find_profanity, is_advertisement
from app.rules import reload_rules, resolve_rules


def test_rule_only_for_soccerblog() -> None:
    reload_rules()
    vnf = resolve_rules(
        chat_id=-75796650734896,
        title="ФУТБОЛ СЕГОДНЯ | МАТЧ ФУТБОЛ",
        link="https://max.ru/channel_vnfutbol",
        source_url="https://t.me/s/thesoccerblogteam",
    )
    assert vnf.block_profanity is True

    other_src = resolve_rules(
        chat_id=-75796650734896,
        title="ФУТБОЛ СЕГОДНЯ | МАТЧ ФУТБОЛ",
        link="https://max.ru/channel_vnfutbol",
        source_url="https://t.me/s/transfermarktru",
    )
    assert other_src.block_profanity is False

    other_channel = resolve_rules(
        chat_id=-76951120689882,
        title="БАРСЕЛОНА | BARCELONA",
        link="https://max.ru/channel_fcbarca",
        source_url="https://t.me/s/thesoccerblogteam",
    )
    assert other_channel.block_profanity is True

    vk = resolve_rules(
        chat_id=-240916179,
        title="ВСЕ НА ФУТБОЛ",
        link="https://vk.com/club240916179",
        source_url="https://t.me/s/thesoccerblogteam",
    )
    assert vk.block_profanity is True

    core = resolve_rules(
        chat_id=-75796650734896,
        title="ФУТБОЛ СЕГОДНЯ | МАТЧ ФУТБОЛ",
        link="https://max.ru/channel_vnfutbol",
    )
    assert core.block_profanity is False


def test_profanity_hits() -> None:
    dirty = [
        "это полный пиздец",
        "ну и хуйня",
        "блять какой момент",
        "бля, опять мимо",
        "похуй кто выиграл",
        "заебали уже",
        "сука не забил",
        "what the fuck",
        "п*здец какой сезон",
        "п0здец какой сезон",
        "похер на таблицу",
        "х*у*й а не оборона",
        "п.и.з.д.е.ц",
        "выебал соперника в контратаке",
        "пиздобол как обычно",
        "долбоёб на бровке",
        "nahuy этот пенальти",
        "ебашить до финального свистка",
        "спиздили три очка",
        "охуели совсем",
        "f*ck this referee",
    ]
    for text in dirty:
        assert contains_profanity(text), text
        fr = is_advertisement(text, mode="strip", block_profanity=True)
        assert fr.is_ad and fr.reason.startswith("мат:"), (text, fr)


def test_profanity_false_positives() -> None:
    clean = [
        "Расписание матчей на сегодня",
        "Херсонский клуб сыграет завтра",
        "ближайший тур АПЛ",
        "хлеб и зрелища после победы",
        "требует победы в дерби",
        "небольшой запас прочности",
        "художник нарисовал постер",
        "хулиганы выбежали на поле",
        "Месси забил с пенальти",
        "ребёнок в семейной секции",
        "мандарин на форме партнёра",
        "новый диск с гимном клуба",
        "скажи о тебе больше",
        "поединок тура",
        "сукно стола в судейской",
    ]
    for text in clean:
        assert not contains_profanity(text), (text, find_profanity(text))
        fr = is_advertisement(text, mode="strip", block_profanity=True)
        assert not fr.is_ad, (text, fr)


def test_other_sources_keep_ads_only() -> None:
    text = "это полный пиздец, какой гол"
    fr = is_advertisement(text, mode="strip", block_profanity=False)
    assert not fr.is_ad


if __name__ == "__main__":
    test_rule_only_for_soccerblog()
    print("OK rules scoped to soccerblog")
    test_profanity_hits()
    print("OK profanity hits")
    test_profanity_false_positives()
    print("OK false positives")
    test_other_sources_keep_ads_only()
    print("OK other sources unchanged")
