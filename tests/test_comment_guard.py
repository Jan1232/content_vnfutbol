from predlozka.comment_guard import (
    NearDupTracker,
    ParsedComment,
    UserBurstTracker,
    evaluate_comment,
    has_profanity,
    has_promo_spam,
    is_delete_complaint,
    is_keyboard_mash,
)


def _c(
    text: str,
    *,
    uid: int = 1,
    cid: str = "c1",
    ts: int = 1_791_200_000_000,
    is_admin: bool = False,
) -> ParsedComment:
    return ParsedComment(
        post_mid="post1",
        comment_mid=cid,
        chat_id=-1,
        user_id=uid,
        text=text,
        is_bot=False,
        timestamp_ms=ts,
        is_admin=is_admin,
    )


def test_promo_without_link() -> None:
    text = (
        "Парни привет 👋\n"
        "Все кто любит футбол ⚽ подписывайтесь на канал в профиле.\n"
        "Контент каждый день. Скоро будет конкурс. Наша цель 100 подписчиков."
    )
    assert has_promo_spam(text)
    d = evaluate_comment(_c(text))
    assert d.action == "delete" and d.reason == "promo"


def test_keyboard_mash() -> None:
    for t in ("Еии", "Еипи", "Пиип", "Ипип", "Ипипип", "П п", "П п п"):
        assert is_keyboard_mash(t), t
        d = evaluate_comment(_c(t, cid=t))
        assert d.action == "delete", (t, d)
    assert not is_keyboard_mash("гол")
    assert not is_keyboard_mash("красава")
    assert evaluate_comment(_c("топ")).action == "ok"


def test_profanity_and_delete_whine() -> None:
    assert has_profanity("Суки")
    assert has_profanity("ну и хуйня")
    assert not has_profanity("какой матч")
    assert is_delete_complaint("Чё мои комментарии удаляют?")
    assert is_delete_complaint("комменты удаляют суки")
    assert is_delete_complaint("бот чистит коммы")
    assert not is_delete_complaint("удалили игрока из заявки")
    d = evaluate_comment(_c("Чё мои комментарии удаляют?\nСуки"))
    assert d.action == "delete" and d.reason == "profanity"
    d2 = evaluate_comment(_c("почему комментарии удаляют"))
    assert d2.action == "delete" and d2.reason == "delete_whine"
    owner = evaluate_comment(_c("сука", uid=44784432, is_admin=True))
    assert owner.action == "ok"


def test_user_burst() -> None:
    burst = UserBurstTracker()
    near = NearDupTracker()
    last = None
    for i, t in enumerate(["ну", "так", "вот", "же"], start=1):
        last = evaluate_comment(
            _c(t, cid=f"b{i}", ts=1_791_200_000_000 + i),
            burst_tracker=burst,
            near_tracker=near,
        )
    assert last is not None
    assert last.action == "delete"
    assert last.reason.startswith("burst:")
    assert len(last.delete_mids) >= 4


if __name__ == "__main__":
    test_promo_without_link()
    test_keyboard_mash()
    test_profanity_and_delete_whine()
    test_user_burst()
    print("ok")
