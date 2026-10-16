"""Глобальные маркеры рекламы: ЕГЭ, VPN, школа, рулетка, заказ, ссылки."""

from __future__ import annotations

from app.filter import is_advertisement


def _ad(text: str) -> None:
    r = is_advertisement(text)
    assert r.is_ad, f"ожидали рекламу: {text!r} -> {r.reason}"


def _ok(text: str) -> None:
    r = is_advertisement(text)
    assert not r.is_ad, f"не должны резать: {text!r} -> {r.reason}"


def test_ege_vpn_school() -> None:
    _ad("Готовим к ЕГЭ за 2 месяца, места ещё есть")
    _ad("Лучший VPN для телефона, скидка 70%")
    _ad("Настрой ВПН и смотри любые каналы")
    _ad("Подготовка к школе: набор в группу")
    _ad("Открыта подготовку к школе, пишите")


def test_roulette_casino_order_promo_link() -> None:
    _ad("Крути рулетку и забери бонус")
    _ad("Новый казик с фриспинами")
    _ad("Футболки на заказ с номером игрока")
    _ad("Промокод на первый депозит внутри")
    _ad("Забрать подарок по ссылке тут")
    _ad("Подробности по ссылке в шапке")


def test_job_and_edu_ads() -> None:
    _ad("Онлайн-курсы английского, старт на этой неделе")
    _ad("Обучение с нуля, диплом гос. образца")
    _ad("Поступи в ВУЗ без ЕГЭ")
    _ad("Университет Синергия: приём документов")
    _ad("Готовим к ОГЭ и контрольным")
    _ad("Удаленная работа, свободный график")
    _ad("Открыта вакансия менеджера")
    _ad("Заработная плата от 80 000, оформление")
    _ad("Профессиональная подготовка кадров")
    _ad("Ищем людей на удалёнку, гибкий график")


def test_labeled_ad_erid_advertiser() -> None:
    _ad("#реклама")
    _ad("Радиодатчик протечки поможет оперативно заметить аварию\n#реклама\nsberdevices.ru")
    _ad("Узнать больше\nО рекламодателе")
    _ad("Полный разбор. erid: 2SDnYMabcd")
    _ad("Токен ERID=2SDnfxyz в подписи")
    _ok("«Реал» обыграл «Барселону» 3:1, хет-трик Мбаппе")


def test_football_news_still_passes() -> None:
    _ok("«Реал» обыграл «Барселону» 3:1, хет-трик Мбаппе")
    _ok("Сборная вышла в финал чемпионата мира")


if __name__ == "__main__":
    test_ege_vpn_school()
    test_roulette_casino_order_promo_link()
    test_job_and_edu_ads()
    test_labeled_ad_erid_advertiser()
    test_football_news_still_passes()
    print("OK ad words")
