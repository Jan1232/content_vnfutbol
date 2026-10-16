"""Промо онлайн-кинотеатров — глобальный фильтр рекламы."""

from __future__ import annotations

from app.filter import is_advertisement

OKKO_POSTER = (
    "Стильный постер остросюжетной драмы «Большая фарма»\n"
    "\n"
    "Сюжет погружает в темную сторону медицинской индустрии. Когда гениальное "
    "изобретение российских ученых ставит под угрозу миллиардные доходы корпораций, "
    "на создателей начинается настоящая охота. В борьбе за спасительное средство "
    "семье разработчиков предстоит пройти через предательство и заговоры\n"
    "\n"
    "Главные роли исполнили Сергей Горошко, Ольга Лерман, Светлана Ходченкова, "
    "Антон Васильев, Игорь Гордин и Сергей Уманов\n"
    "\n"
    "Сериал уже доступен для просмотра только в Okko"
)


def test_okko_series_poster_is_ad() -> None:
    r = is_advertisement(OKKO_POSTER)
    assert r.is_ad, r.reason
    assert "стриминг" in r.reason


def test_ivi_and_kinopoisk_watch_cta() -> None:
    assert is_advertisement("Фильм уже можно посмотреть на IVI").is_ad
    assert is_advertisement("Премьера сериала на Кинопоиске").is_ad
    assert is_advertisement("Смотрите эксклюзивно в Wink").is_ad


def test_morocco_and_rights_news_pass() -> None:
    assert not is_advertisement(
        "Марокко вышло в финал. Игрок из Марокко переходит в «Челси»."
    ).is_ad
    assert not is_advertisement(
        "Окко купила права на показ Лиги чемпионов, сумма сделки не раскрывается."
    ).is_ad


def test_movie_news_without_platform_cta_passes() -> None:
    text = (
        "Стильный постер остросюжетной драмы «Большая фарма»\n"
        "Главные роли исполнили Сергей Горошко и Ольга Лерман."
    )
    assert not is_advertisement(text).is_ad


def test_embassy_watch_on_vk_is_not_ad() -> None:
    text = (
        "Оба масштабных проекта доступны для просмотра в полном объёме "
        "на официальной странице Посольства России в Китае в социальной сети VK"
    )
    assert not is_advertisement(text).is_ad


def test_only_on_okko_broadcast_is_ad() -> None:
    r = is_advertisement(
        "Суперкубок Германии: Боруссия vs Бавария — в Okko!\n"
        "Смотри и наслаждайся игрой немецких грандов уже сегодня в 21:30. Только в Okko"
    )
    assert r.is_ad, r.reason


if __name__ == "__main__":
    test_okko_series_poster_is_ad()
    print("OK okko poster")
    test_ivi_and_kinopoisk_watch_cta()
    print("OK ivi/kinopoisk")
    test_morocco_and_rights_news_pass()
    print("OK morocco/rights news")
    test_movie_news_without_platform_cta_passes()
    print("OK movie news")
    test_only_on_okko_broadcast_is_ad()
    print("OK only on Okko")
    test_embassy_watch_on_vk_is_not_ad()
    print("OK embassy VK")
