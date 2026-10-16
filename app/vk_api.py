"""Клиент VK API для публикации в сообщества (ключ сообщества)."""

from __future__ import annotations

import mimetypes
from typing import Any

from app.http_util import http_client

VK_API = "https://api.vk.com/method"
VK_VERSION = "5.199"


class VkApiError(Exception):
    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = int(code or 0)
        self.body = message


class VkClient:
    def __init__(self, token: str) -> None:
        self.token = (token or "").strip()
        if not self.token:
            raise VkApiError("Пустой ключ VK")

    def call(self, method: str, **params: Any) -> Any:
        body = {k: v for k, v in params.items() if v is not None and v != ""}
        body["access_token"] = self.token
        body["v"] = VK_VERSION
        with http_client(timeout=60.0) as c:
            r = c.post(f"{VK_API}/{method}", data=body)
            r.raise_for_status()
            payload = r.json()
        if "error" in payload:
            err = payload["error"] or {}
            code = int(err.get("error_code") or 0)
            msg = (err.get("error_msg") or "VK API error").strip()
            raise VkApiError(f"{code}: {msg}", code=code)
        return payload.get("response")

    def identify_community(self) -> dict[str, Any]:
        """По ключу сообщества вернуть группу: id, title, screen_name, …"""
        group = self._group_from_get_by_id()
        if not group:
            owner_id = self._owner_from_wall()
            if owner_id:
                group = self._group_from_get_by_id(abs(int(owner_id)))
        if not group:
            raise VkApiError(
                "Ключ не похож на ключ сообщества. "
                "Создайте его в группе: Управление → Работа с API → Создать ключ"
            )
        gid = int(group["id"])
        screen = (group.get("screen_name") or f"club{gid}").strip()
        photo = (
            group.get("photo_200")
            or group.get("photo_100")
            or group.get("photo_50")
            or ""
        )
        return {
            "id": gid,
            "owner_id": -gid,
            "title": (group.get("name") or screen).strip(),
            "screen_name": screen,
            "link": f"https://vk.com/{screen}",
            "description": (group.get("description") or "")[:800],
            "members_count": int(group.get("members_count") or 0),
            "photo": photo,
            "type": "group",
        }

    def _unpack_groups(self, raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, dict):
            groups = raw.get("groups") or raw.get("items") or []
            return [g for g in groups if isinstance(g, dict)]
        if isinstance(raw, list):
            return [g for g in raw if isinstance(g, dict)]
        return []

    def _group_from_get_by_id(self, group_id: int | None = None) -> dict[str, Any] | None:
        params: dict[str, Any] = {
            "fields": "members_count,description,photo_200,photo_100,screen_name",
        }
        if group_id:
            params["group_id"] = int(group_id)
        try:
            raw = self.call("groups.getById", **params)
        except VkApiError:
            return None
        groups = self._unpack_groups(raw)
        return groups[0] if groups else None

    def _owner_from_wall(self) -> int | None:
        try:
            raw = self.call("wall.get", count=1)
        except VkApiError:
            return None
        if not isinstance(raw, dict):
            return None
        items = raw.get("items") or []
        if items and isinstance(items[0], dict) and items[0].get("owner_id"):
            return int(items[0]["owner_id"])
        return None

    def can_upload_wall_photo(self, group_id: int) -> None:
        """Проверить, что ключ умеет грузить фото на стену группы (не ключ сообщества)."""
        try:
            self.call("photos.getWallUploadServer", group_id=int(group_id))
        except VkApiError as e:
            raise VkApiError(self._upload_error(e), code=e.code) from e

    @staticmethod
    def _upload_error(exc: VkApiError) -> str:
        if exc.code == 27 or "group auth" in str(exc).casefold():
            return (
                "Ключ сообщества не умеет загружать фото и видео (VK 27). "
                "В карточке группы нужен ключ администратора — пользовательский, "
                "с правами photos, wall, video, groups, offline."
            )
        return str(exc)

    def upload_wall_photo(self, group_id: int, content: bytes, filename: str = "photo.jpg") -> str:
        """Залить фото на стену. Возвращает attachment photo{owner}_{id}."""
        try:
            server = self.call("photos.getWallUploadServer", group_id=int(group_id))
        except VkApiError as e:
            raise VkApiError(self._upload_error(e), code=e.code) from e
        if not isinstance(server, dict) or not server.get("upload_url"):
            raise VkApiError("Нет upload_url для фото")
        ctype = mimetypes.guess_type(filename)[0] or "image/jpeg"
        with http_client(timeout=120.0) as c:
            ur = c.post(
                server["upload_url"],
                files={"photo": (filename, content, ctype)},
            )
            ur.raise_for_status()
            uploaded = ur.json()
        try:
            saved = self.call(
                "photos.saveWallPhoto",
                group_id=int(group_id),
                photo=uploaded.get("photo"),
                server=uploaded.get("server"),
                hash=uploaded.get("hash"),
            )
        except VkApiError as e:
            raise VkApiError(self._upload_error(e), code=e.code) from e
        items = saved if isinstance(saved, list) else (saved or {}).get("items") or []
        if not items:
            raise VkApiError("photos.saveWallPhoto пустой ответ")
        photo = items[0]
        return f"photo{photo['owner_id']}_{photo['id']}"

    def upload_wall_video(self, group_id: int, content: bytes, filename: str = "video.mp4") -> str:
        try:
            saved = self.call(
                "video.save",
                group_id=int(group_id),
                name=filename[:64] or "video",
                wallpost=0,
            )
        except VkApiError as e:
            raise VkApiError(self._upload_error(e), code=e.code) from e
        if not isinstance(saved, dict) or not saved.get("upload_url"):
            raise VkApiError("Нет upload_url для видео")
        ctype = mimetypes.guess_type(filename)[0] or "video/mp4"
        with http_client(timeout=180.0) as c:
            ur = c.post(
                saved["upload_url"],
                files={"video_file": (filename, content, ctype)},
            )
            ur.raise_for_status()
            try:
                done = ur.json()
            except Exception:
                done = {}
        owner_id = int((done or {}).get("owner_id") or saved.get("owner_id") or -int(group_id))
        video_id = int((done or {}).get("video_id") or saved.get("video_id") or 0)
        if not video_id:
            raise VkApiError("video.save не вернул video_id")
        return f"video{owner_id}_{video_id}"

    def wall_post(
        self,
        owner_id: int,
        message: str = "",
        attachments: list[str] | None = None,
    ) -> int:
        params: dict[str, Any] = {
            "owner_id": int(owner_id),
            "from_group": 1,
            "message": (message or "")[:15000],
        }
        if attachments:
            params["attachments"] = ",".join(attachments[:10])
        if not params["message"] and not params.get("attachments"):
            raise VkApiError("Пустой пост")
        raw = self.call("wall.post", **params)
        if isinstance(raw, dict) and raw.get("post_id"):
            return int(raw["post_id"])
        raise VkApiError(f"wall.post странный ответ: {raw!r}")
