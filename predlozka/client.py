from __future__ import annotations

from typing import Any

from app.max_api import MaxClient, MaxApiError


class PredlozkaClient(MaxClient):
    def send_message(
        self,
        chat_id: int | None = None,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        notify: bool = True,
        disable_link_preview: bool = True,
        format: str | None = None,
        user_id: int | None = None,
        link: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"notify": notify}
        if text is not None:
            body["text"] = text[:4000]
        if attachments is not None:
            body["attachments"] = attachments
        if format:
            body["format"] = format
        if link:
            body["link"] = link
        params: dict[str, Any] = {
            "disable_link_preview": str(disable_link_preview).lower(),
        }
        if user_id is not None:
            params["user_id"] = int(user_id)
        elif chat_id is not None:
            params["chat_id"] = int(chat_id)
        else:
            raise ValueError("нужен user_id или chat_id")
        return self._request("POST", "/messages", params=params, json=body)

    def send_to_user(
        self,
        user_id: int,
        text: str | None = None,
        attachments: list[dict[str, Any]] | None = None,
        format: str | None = "html",
        link: dict[str, Any] | None = None,
        notify: bool = True,
    ) -> dict[str, Any]:
        return self.send_message(
            user_id=int(user_id),
            text=text,
            attachments=attachments,
            format=format,
            link=link,
            notify=notify,
            disable_link_preview=True,
        )

    def answer_callback(
        self,
        callback_id: str,
        *,
        notification: str | None = None,
        message: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        body: dict[str, Any] = {}
        if notification:
            body["notification"] = notification[:200]
        if message is not None:
            body["message"] = message
        if not body:
            body["notification"] = "OK"
        return self._request(
            "POST",
            "/answers",
            params={"callback_id": callback_id},
            json=body,
        )

    def get_admins(self, chat_id: int) -> list[dict[str, Any]]:
        data = self._request("GET", f"/chats/{int(chat_id)}/members/admins")
        if isinstance(data, dict):
            members = data.get("members") or data.get("chat_members") or []
            return [m for m in members if isinstance(m, dict)]
        return []

    def get_my_membership(self, chat_id: int) -> dict[str, Any] | None:
        try:
            data = self._request("GET", f"/chats/{int(chat_id)}/members/me")
        except MaxApiError:
            return None
        return data if isinstance(data, dict) else None

    def set_commands(self, commands: list[dict[str, str]]) -> Any:
        return self._request("PATCH", "/me/commands", json={"commands": commands})

    def delete_comment(self, post_mid: str, comment_id: str) -> dict[str, Any] | None:
        """Удалить комментарий к посту. Нужны права read_all_messages + delete."""
        mid = (post_mid or "").strip()
        cid = (comment_id or "").strip()
        if not mid or not cid:
            raise ValueError("нужны post_mid и comment_id")
        try:
            return self._request(
                "DELETE",
                f"/messages/{mid}/comments",
                params={"comment_id": cid},
            )
        except MaxApiError as exc:
            # альтернативный путь из use-case moderation
            if exc.status in {404, 405}:
                return self._request("DELETE", f"/comments/{cid}")
            raise

    def list_messages(
        self,
        chat_id: int,
        *,
        count: int = 50,
        from_ms: int | None = None,
        to_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "chat_id": int(chat_id),
            "count": max(1, min(100, int(count))),
        }
        if from_ms is not None:
            params["from"] = int(from_ms)
        if to_ms is not None:
            params["to"] = int(to_ms)
        data = self._request("GET", "/messages", params=params)
        msgs = data.get("messages") if isinstance(data, dict) else None
        return [m for m in (msgs or []) if isinstance(m, dict)]

    def list_comments(
        self,
        post_mid: str,
        *,
        count: int = 100,
        before: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"count": max(1, min(100, int(count)))}
        if before is not None:
            params["before"] = int(before)
        data = self._request(
            "GET",
            f"/messages/{(post_mid or '').strip()}/comments",
            params=params,
        )
        msgs = data.get("messages") or data.get("comments") if isinstance(data, dict) else None
        return [m for m in (msgs or []) if isinstance(m, dict)]
