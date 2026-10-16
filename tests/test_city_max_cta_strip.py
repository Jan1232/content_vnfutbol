"""Концовки городских СМИ: лейблы бывших ссылок в MAX."""

from __future__ import annotations

from app.filter import sanitize_for_publish
from app.rules import load_core_rules, reload_rules


def test_core_yaml_has_city_cta_filters() -> None:
    reload_rules()
    core = load_core_rules()
    joined = " ".join(core.strip_line_contains)
    assert "прислать новость в max" in joined
    assert "читайте нас в max" in joined


def test_strips_send_news_and_read_us_in_max() -> None:
    raw = (
        "Участок улицы Красноармейской в Воронеже закроют на неделю\n"
        "\n"
        "↗️ Прислать новость в MAX ✅ Подписаться\n"
        "▶️ Читайте нас в MAX"
    )
    cleaned = sanitize_for_publish(raw)
    low = cleaned.casefold()
    assert "Красноармейской" in cleaned
    assert "прислать новость" not in low
    assert "читайте нас" not in low
    assert "подписаться" not in low


def test_strips_subscribe_send_news_smile() -> None:
    samples = [
        "Беспилотную опасность объявили\n😊 Подписаться| Прислать новость",
        "Добби такого поворота точно не просил\n😊 Подписаться | Прислать новость",
    ]
    for raw in samples:
        cleaned = sanitize_for_publish(raw)
        low = cleaned.casefold()
        assert "прислать новость" not in low
        assert "подписаться" not in low
        assert cleaned.splitlines()[0]


def test_strips_other_city_source_footers() -> None:
    samples = {
        "Медленно грузит? Мы в MAX 📲": "грузит",
        "✅ЧП Дагестан | Подписаться": "дагестан |",
        "🐪 Подписаться": "подписаться",
        "🔔ПОДПИСАТЬСЯ": "подписаться",
        "🫡Подписаться на Новости": "подписаться",
        "❤️ Подписывайся на": "подписывайся",
        "•Подписаться на": "подписаться",
        "Предложить новость Админу👈": "админу",
        "💌 Прислать материал": "материал",
        "☝️ ЧП Пермь в: ТГ | ВК | МАКС — Подпишитесь": "подпишитесь",
        "📲 Если у вас плохо прогружаются файлы, все новости также доступны в нашем канале в MAX": "прогружаются",
        "(Чтобы найти наш канал в MAX, откройте раздел «Безопасность» в своём профиле и отключите «Безопасный режим»)": "безопасный режим",
        "📲 MAXимум новостей 📱«Крым 24» | «Радио Крым» 🎙": "крым 24",
        "Подробнее ➡️ /news/216834": "/news/",
        "🇷🇺 Подписаться | Поделиться новостью": "поделиться",
        "Список адресов, где отключат свет, смотрите в Дзене": "дзене",
        "utv": "utv",
        "•Дублируем новости в MAХ": "дублируем",
        "⚡️ ТГ замедлили": "замедлили",
        "Не грузится телеграмм ?": "телеграмм",
        "✅Не грузит медиа? Смотри ЧП в МАХ": "не грузит",
        "🔳 Подпишись на Афишу Е1": "афишу",
        "🔳 Подпишись на": "подпишись",
        "🔳 Подпишись на Е1 | Предложи новость": "предложи",
        "🟥 Подпишись на новости Якутии |Сообщи свою новость": "якутии",
        "⏺️Канал в Мах — подпишитесь": "подпишитесь",
        "⏺️Канал в Телеграм —": "телеграм",
        "👉 Хабаровск Сегодня": "хабаровск сегодня",
        "🟢 ХАБАРОВСК СЕГОДНЯ": "хабаровск сегодня",
        "Легендарный 24 | VK | Легендарный МАХ": "легендарный",
        "Telegram | Все каналы «Фонтанки» | Max": "фонтанки",
        "Если интернета нет, давайте быть на связи в MAX": "на связи в",
        "Подписывайтесь на Е1 в МАКС ⬅": "е1 в макс",
        "⛽️ Дарим бензин нашим подписчикам!": "дарим бензин",
        "ТУТ МЫ ДАРИМ БЕНЗИН": "дарим бензин",
        "Бесплатные билеты на концерт тут": "бесплатные билеты",
        "Подпишись на СтарХит": "стархит",
        "Предложи новость|Подпишись на МАХ": "предложи новость",
    }
    body = "Коротко о событии в городе"
    for footer, needle in samples.items():
        cleaned = sanitize_for_publish(f"{body}\n{footer}")
        low = cleaned.casefold()
        assert body in cleaned, footer
        assert needle not in low, f"осталось {footer!r} -> {cleaned!r}"


def test_strips_e1_inline_subscribe_tail() -> None:
    raw = (
        "Мотоциклиста сбили на улице Белинского. "
        "🔛 Здесь мы всегда на связи. Подписывайтесь!"
    )
    cleaned = sanitize_for_publish(raw)
    low = cleaned.casefold()
    assert "мотоциклиста" in low
    assert "на связи" not in low
    assert "подписывайтесь" not in low


if __name__ == "__main__":
    test_core_yaml_has_city_cta_filters()
    test_strips_send_news_and_read_us_in_max()
    test_strips_subscribe_send_news_smile()
    test_strips_other_city_source_footers()
    test_strips_e1_inline_subscribe_tail()
    print("OK city max cta strip")
