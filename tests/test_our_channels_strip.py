"""Глобально вырезаем «наши каналы», посты при этом не блокируем."""

from __future__ import annotations

from app.filter import is_advertisement, sanitize_for_publish


def _ok_ad(text: str) -> None:
    r = is_advertisement(text)
    assert not r.is_ad, f"не должны резать пост: {text!r} -> {r.reason}"


def test_strips_our_channels_but_keeps_post() -> None:
    samples = [
        "Голы в концовке.\n\nНаши другие каналы 👇",
        "Голы в концовке.\nнаши каналы тут",
        "Голы в концовке.\nНаши каналы",
        "Голы в концовке.\nдругие наши каналы",
        "Голы в концовке.\nнаш канал 👇",
        "Голы в концовке.\nнаши тг каналы",
        "Голы в концовке.\nПодписывайтесь на наши каналы",
        "Голы в концовке.\n\n🏆 LA LIGA • Наши Каналы",
        "Голы в концовке. Наши каналы тут 👇",
    ]
    for raw in samples:
        _ok_ad(raw)
        cleaned = sanitize_for_publish(raw)
        assert "Голы в концовке" in cleaned
        low = cleaned.casefold()
        assert "наши каналы" not in low
        assert "наш канал" not in low
        assert "другие наши" not in low


def test_news_about_channels_stays() -> None:
    news = "По нашим каналам связи поступила информация о встрече лидеров"
    _ok_ad(news)
    cleaned = sanitize_for_publish(news)
    assert "каналам связи" in cleaned


if __name__ == "__main__":
    test_strips_our_channels_but_keeps_post()
    test_news_about_channels_stays()
    print("OK our channels strip")
