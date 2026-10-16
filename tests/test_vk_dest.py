"""Назначение VK: разбор ответа API и правила канала."""

from __future__ import annotations

from app.vk_api import VkApiError, VkClient


def test_unpack_groups_v5() -> None:
    vk = VkClient("dummy")
    raw = {
        "groups": [
            {
                "id": 12345,
                "name": "ВСЕ НА ФУТБОЛ",
                "screen_name": "vnfutbol",
                "members_count": 10,
            }
        ]
    }
    groups = vk._unpack_groups(raw)
    assert groups[0]["id"] == 12345
    assert vk._unpack_groups([{"id": 1}])[0]["id"] == 1
    assert vk._unpack_groups(None) == []


def test_group_token_upload_error() -> None:
    from app.vk_api import VkApiError

    msg = VkClient._upload_error(VkApiError("27: Group authorization failed", code=27))
    assert "сообщества не умеет" in msg
    assert "администратора" in msg


def test_empty_token() -> None:
    try:
        VkClient("")
    except VkApiError as e:
        assert "Пустой" in str(e)
        return
    raise AssertionError("expected VkApiError")


def test_group_token_upload_error() -> None:
    msg = VkClient._upload_error(VkApiError("27: Group authorization failed", code=27))
    assert "сообщества не умеет" in msg
    assert "администратора" in msg


if __name__ == "__main__":
    test_unpack_groups_v5()
    print("OK unpack")
    test_empty_token()
    print("OK empty token")
    test_group_token_upload_error()
    print("OK upload error 27")
