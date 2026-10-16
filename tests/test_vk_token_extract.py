"""Разбор access_token из URL/тела запроса VK."""

from __future__ import annotations

from workers.vk_token_refresh import extract_access_token, storage_state_from_cookie_header


def test_extract_from_query_and_body() -> None:
    token = "vk1.a." + ("B" * 80)
    url = f"https://web.api.vk.ru/method/photos.getWallUploadServer?access_token={token}&v=5.288"
    assert extract_access_token(url) == token
    body = f"group_id=1&access_token={token}"
    assert extract_access_token("https://web.api.vk.ru/method/x", body) == token
    js = '{"access_token":"' + token + '"}'
    assert extract_access_token(js) == token
    assert extract_access_token("Bearer " + token) == token
    assert extract_access_token("https://example.com") == ""


def test_storage_state_from_cookie_header() -> None:
    state = storage_state_from_cookie_header("remixsid=abc123; remixstid=zzz; other=1")
    names = {(c["name"], c["domain"]) for c in state["cookies"]}
    assert ("remixsid", ".vk.com") in names
    assert ("remixsid", ".vk.ru") in names
    try:
        storage_state_from_cookie_header("foo=bar")
    except ValueError:
        pass
    else:
        raise AssertionError("expected missing remixsid")


if __name__ == "__main__":
    test_extract_from_query_and_body()
    test_storage_state_from_cookie_header()
    print("OK extract")
