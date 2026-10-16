from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.max_api import MaxApiError

from predlozka.client import PredlozkaClient
from predlozka.comment_guard import (
    comment_guard_enabled,
    evaluate_comment,
    parse_comment_update,
    sweep_on_join_enabled,
)
from predlozka.comment_sweep import sweep_channel_comments
from predlozka.erid_ads import ingest_message as ingest_erid_message
from predlozka.erid_ads import (
    approaching_digest,
    scan_all_today,
    send_daily_digest,
    should_send_now,
)
from predlozka.db import (
    add_submission,
    ban_user,
    bind_session,
    channel_settings,
    clear_admin_state,
    count_submissions_hour,
    db,
    drop_session,
    get_admin_state,
    get_channel,
    get_channel_by_slug,
    get_meta,
    get_sender,
    get_session,
    init_db,
    is_banned,
    remember_sender,
    list_bans,
    owner_channels,
    set_admin_state,
    set_channel_setting,
    set_channel_status,
    set_channel_title,
    set_meta,
    unban_user,
    upsert_channel,
)
from predlozka.logic import (
    attachment_url,
    ban_list_keyboard,
    callback_keyboard,
    combine_header_and_text,
    deep_link,
    format_added_text,
    format_ban_list_text,
    format_owner_link_text,
    format_settings_text,
    format_submit_welcome,
    format_suggestion_header,
    is_dialog_message,
    is_valid_slug,
    length_error,
    message_attachments,
    message_mid,
    message_text,
    parse_callback_payload,
    parse_command,
    parse_int_setting,
    rate_error,
    sender_of,
    settings_keyboard,
    suggestion_keyboard,
    user_id_of,
    user_label,
)

log = logging.getLogger("predlozka")

UPDATE_TYPES = [
    "bot_added",
    "bot_removed",
    "bot_started",
    "bot_stopped",
    "message_created",
    "message_callback",
    "chat_title_changed",
    "comment_created",
    "comment_edited",
]

NO_LINK_TEXT = (
    "Этот бот принимает предложения только по уникальной ссылке канала. "
    "Обычная ссылка на бота не работает."
)
NOT_OWNER_URL = "Команда /url доступна владельцу канала, в который добавлен бот."
NO_CHANNELS_URL = (
    "Нет активных каналов. Добавьте бота администратором в канал — "
    "ссылка придёт сюда автоматически."
)
DELIVER_FAIL = (
    "Не удалось доставить предложение владельцу. "
    "Попросите его один раз открыть этого бота и повторите отправку."
)


class PredlozkaBot:
    def __init__(self, client: PredlozkaClient) -> None:
        self.client = client
        me = client.me()
        self.bot_username = str(me.get("username") or "").lstrip("@")
        self.bot_user_id = user_id_of(me)
        if not self.bot_username:
            raise RuntimeError("у бота нет username — нельзя собрать диплинк")
        self._register_commands()
        self._erid_thread = threading.Thread(
            target=self._erid_loop,
            name="predlozka-erid-ads",
            daemon=True,
        )
        self._erid_thread.start()

    def _register_commands(self) -> None:
        try:
            self.client.set_commands(
                [
                    {"name": "url", "description": "Ссылка предложки для вашего канала"},
                    {"name": "setting", "description": "Антифрод: лимиты и длина сообщений"},
                    {"name": "ban", "description": "Список банов и разбан"},
                ]
            )
        except MaxApiError as exc:
            log.warning("не удалось зарегистрировать команды: %s", exc)

    def channel_url(self, slug: str) -> str:
        return deep_link(self.bot_username, slug)

    def safe_dm(
        self,
        user_id: int | None,
        text: str,
        attachments: list[dict[str, Any]] | None = None,
        link: dict[str, Any] | None = None,
        format: str | None = "html",
    ) -> bool:
        if not user_id:
            return False
        try:
            self.client.send_to_user(
                int(user_id),
                text=text,
                attachments=attachments,
                link=link,
                format=format,
            )
            return True
        except MaxApiError as exc:
            log.warning("DM %s fail: %s", user_id, exc)
            return False

    def ack(self, callback_id: str, notification: str, message: dict[str, Any] | None = None) -> None:
        try:
            self.client.answer_callback(
                callback_id,
                notification=notification,
                message=message,
            )
        except MaxApiError as exc:
            log.warning("callback answer fail: %s", exc)

    def owned_channel(self, uid: int, chat_id: int) -> dict[str, Any] | None:
        with db() as conn:
            channel = get_channel(conn, chat_id)
            allowed = owner_channels(conn, uid)
        if not channel or channel.get("status") != "active":
            return None
        if int(channel["chat_id"]) not in {int(c["chat_id"]) for c in allowed}:
            return None
        return channel

    def pick_channel(
        self,
        uid: int,
        *,
        empty_text: str,
        multi_text: str,
        prefix: str,
        on_single,
    ) -> None:
        with db() as conn:
            channels = owner_channels(conn, uid)
        if not channels:
            self.safe_dm(uid, empty_text)
            return
        if len(channels) == 1:
            on_single(channels[0])
            return
        self.safe_dm(uid, multi_text, attachments=callback_keyboard(channels, prefix=prefix))

    def resolve_owner(self, chat_id: int, added_by: int | None) -> int | None:
        try:
            for member in self.client.get_admins(chat_id):
                if member.get("is_bot"):
                    continue
                if member.get("is_owner"):
                    uid = user_id_of(member)
                    if uid:
                        return uid
        except MaxApiError as exc:
            log.warning("admins %s fail: %s", chat_id, exc)
        return added_by

    def chat_title(self, chat_id: int, fallback: str = "") -> tuple[str, str]:
        try:
            chat = self.client.get_chat(chat_id)
        except MaxApiError as exc:
            log.warning("get_chat %s fail: %s", chat_id, exc)
            return fallback or f"канал {chat_id}", "channel"
        title = str(chat.get("title") or fallback or f"канал {chat_id}").strip()
        chat_type = str(chat.get("type") or "channel").strip() or "channel"
        return title, chat_type

    def notify_owner_link(self, channel: dict[str, Any], *, added: bool) -> bool:
        url = self.channel_url(channel["slug"])
        title = channel.get("title") or "канал"
        text = format_added_text(title, url) if added else format_owner_link_text(title, url)
        targets: list[int] = []
        for key in ("owner_user_id", "added_by_user_id"):
            raw = channel.get(key)
            if raw is None:
                continue
            uid = int(raw)
            if uid not in targets:
                targets.append(uid)
        for uid in targets:
            if self.safe_dm(uid, text):
                return True
        return False

    def handle_bot_added(self, update: dict[str, Any]) -> None:
        chat_id = update.get("chat_id")
        if chat_id is None:
            return
        chat_id = int(chat_id)
        added_by = user_id_of(update.get("user") if isinstance(update.get("user"), dict) else None)
        title, chat_type = self.chat_title(
            chat_id,
            fallback="канал" if update.get("is_channel") else "чат",
        )
        owner_id = self.resolve_owner(chat_id, added_by)
        if not owner_id:
            log.warning("bot_added %s: нет владельца", chat_id)
            return
        with db() as conn:
            channel = upsert_channel(
                conn,
                chat_id=chat_id,
                title=title,
                chat_type=chat_type,
                owner_user_id=owner_id,
                added_by_user_id=added_by,
            )
        sent = self.notify_owner_link(channel, added=True)
        if not sent:
            log.info(
                "ссылка канала %s не доставлена — владелец ещё не открыл бота",
                chat_id,
            )
        self._start_comment_sweep(chat_id, is_channel=bool(update.get("is_channel", True)))

    def _start_comment_sweep(self, chat_id: int, *, is_channel: bool = True) -> None:
        if not comment_guard_enabled() or not sweep_on_join_enabled():
            return
        if not is_channel:
            return
        t = threading.Thread(
            target=self._run_comment_sweep,
            args=(int(chat_id),),
            daemon=True,
            name=f"predlozka-sweep-{chat_id}",
        )
        t.start()
        log.info("comment sweep queued chat=%s", chat_id)

    def _run_comment_sweep(self, chat_id: int) -> None:
        from predlozka.config import api_base, bot_token

        client = PredlozkaClient(token=bot_token(), base=api_base())
        try:
            stats = sweep_channel_comments(
                client,
                chat_id,
                bot_user_id=self.bot_user_id,
            )
            log.info("comment sweep finished chat=%s %s", chat_id, stats)
        except Exception:
            log.exception("comment sweep crash chat=%s", chat_id)
        finally:
            client.close()

    def handle_bot_removed(self, update: dict[str, Any]) -> None:
        chat_id = update.get("chat_id")
        if chat_id is None:
            return
        with db() as conn:
            set_channel_status(conn, int(chat_id), "removed")

    def handle_title_changed(self, update: dict[str, Any]) -> None:
        chat_id = update.get("chat_id")
        title = str(update.get("title") or "").strip()
        if chat_id is None or not title:
            return
        with db() as conn:
            set_channel_title(conn, int(chat_id), title)

    def handle_bot_started(self, update: dict[str, Any]) -> None:
        user = update.get("user") if isinstance(update.get("user"), dict) else {}
        uid = user_id_of(user)
        if not uid:
            return
        payload = str(update.get("payload") or "").strip()
        self._start_user(uid, payload, user)

    def handle_bot_stopped(self, update: dict[str, Any]) -> None:
        user = update.get("user") if isinstance(update.get("user"), dict) else {}
        uid = user_id_of(user)
        if uid:
            with db() as conn:
                drop_session(conn, uid)

    def _start_user(self, uid: int, payload: str, user: dict[str, Any]) -> None:
        if is_valid_slug(payload):
            with db() as conn:
                channel = get_channel_by_slug(conn, payload)
                banned = bool(channel and is_banned(conn, int(channel["chat_id"]), uid))
                if channel and not banned:
                    bind_session(conn, uid, int(channel["chat_id"]))
            if not channel:
                self.safe_dm(uid, "Ссылка недействительна или канал больше не подключён.")
                return
            if banned:
                self.safe_dm(uid, "Вы заблокированы в предложке этого канала.")
                return
            self.safe_dm(uid, format_submit_welcome(channel["title"]))
            return

        with db() as conn:
            channels = owner_channels(conn, uid)
        if not channels:
            self.safe_dm(uid, NO_LINK_TEXT)
            return
        if len(channels) == 1:
            self.notify_owner_link(channels[0], added=False)
            return
        self.safe_dm(
            uid,
            "Вы владелец нескольких каналов. "
            "Команда /url — выбрать канал и получить ссылку предложки.",
        )

    def handle_url_command(self, uid: int) -> None:
        self.pick_channel(
            uid,
            empty_text=NO_CHANNELS_URL,
            multi_text="Выберите канал, для которого нужна ссылка предложки:",
            prefix="url",
            on_single=lambda ch: self.notify_owner_link(ch, added=False),
        )

    def show_settings(self, uid: int, channel: dict[str, Any]) -> None:
        rate, max_chars = channel_settings(channel)
        self.safe_dm(
            uid,
            format_settings_text(channel.get("title") or "", rate, max_chars),
            attachments=settings_keyboard(int(channel["chat_id"])),
        )

    def handle_setting_command(self, uid: int) -> None:
        self.pick_channel(
            uid,
            empty_text=NO_CHANNELS_URL,
            multi_text="Выберите канал для настроек антифрода:",
            prefix="setmenu",
            on_single=lambda ch: self.show_settings(uid, ch),
        )

    def ask_setting(self, uid: int, chat_id: int, action: str) -> None:
        with db() as conn:
            set_admin_state(conn, uid, action, chat_id)
        if action == "wait_rate":
            text = (
                "Отправьте число сообщений в час от одного отправителя.\n"
                "0 — без ограничения. Диапазон: 0–100."
            )
        else:
            text = (
                "Отправьте максимальную длину текста в символах.\n"
                "0 — без ограничения. Диапазон: 0–4000."
            )
        self.safe_dm(uid, text)

    def handle_setting_input(self, uid: int, text: str) -> bool:
        with db() as conn:
            state = get_admin_state(conn, uid)
        if not state:
            return False
        action = str(state.get("action") or "")
        chat_id = state.get("chat_id")
        if action not in {"wait_rate", "wait_len"} or chat_id is None:
            with db() as conn:
                clear_admin_state(conn, uid)
            return False
        channel = self.owned_channel(uid, int(chat_id))
        if not channel:
            with db() as conn:
                clear_admin_state(conn, uid)
            self.safe_dm(uid, "Нет доступа к этому каналу.")
            return True
        value = parse_int_setting(text)
        if action == "wait_rate":
            if value is None or value < 0 or value > 100:
                self.safe_dm(uid, "Нужно целое число от 0 до 100. Или /setting для отмены.")
                return True
            field, label = "rate_per_hour", "лимит сообщений в час"
        else:
            if value is None or value < 0 or value > 4000:
                self.safe_dm(uid, "Нужно целое число от 0 до 4000. Или /setting для отмены.")
                return True
            field, label = "max_chars", "макс. длина текста"
        with db() as conn:
            set_channel_setting(conn, int(chat_id), field, value)
            clear_admin_state(conn, uid)
            channel = get_channel(conn, int(chat_id))
        shown = "без лимита" if value == 0 else str(value)
        self.safe_dm(uid, f"Сохранено: {label} — {shown}.")
        if channel:
            self.show_settings(uid, channel)
        return True

    def show_bans(self, uid: int, channel: dict[str, Any]) -> None:
        with db() as conn:
            bans = list_bans(conn, int(channel["chat_id"]))
        self.safe_dm(
            uid,
            format_ban_list_text(channel.get("title") or "", bans),
            attachments=ban_list_keyboard(int(channel["chat_id"]), bans) or None,
        )

    def handle_ban_command(self, uid: int) -> None:
        self.pick_channel(
            uid,
            empty_text=NO_CHANNELS_URL,
            multi_text="Выберите канал, чтобы посмотреть баны:",
            prefix="banlist",
            on_single=lambda ch: self.show_bans(uid, ch),
        )

    def apply_ban(self, uid: int, chat_id: int, target_id: int, sender: dict[str, Any] | None) -> str:
        channel = self.owned_channel(uid, chat_id)
        if not channel:
            return "Нет доступа"
        if target_id == uid or target_id == int(channel.get("owner_user_id") or 0):
            return "Нельзя забанить владельца"
        username = ""
        if isinstance(sender, dict):
            username = str(sender.get("username") or "").strip()
        label = user_label(sender) if sender else ""
        if not label:
            with db() as conn:
                stored = get_sender(conn, chat_id, target_id)
            if stored:
                label = str(stored.get("label") or "")
                username = username or str(stored.get("username") or "")
        label = label or f"id {target_id}"
        with db() as conn:
            added = ban_user(
                conn,
                chat_id=chat_id,
                user_id=target_id,
                label=label,
                username=username,
                banned_by=uid,
            )
        if added:
            self.safe_dm(target_id, "Вас заблокировали в предложке этого канала.")
            return "Пользователь забанен"
        return "Уже в бане"

    def apply_unban(self, uid: int, chat_id: int, target_id: int) -> str:
        channel = self.owned_channel(uid, chat_id)
        if not channel:
            return "Нет доступа"
        with db() as conn:
            removed = unban_user(conn, chat_id, target_id)
        return "Разбанен" if removed else "Не был в бане"

    def handle_callback(self, update: dict[str, Any]) -> None:
        callback = update.get("callback") if isinstance(update.get("callback"), dict) else {}
        callback_id = str(callback.get("callback_id") or "").strip()
        user = callback.get("user") if isinstance(callback.get("user"), dict) else {}
        uid = user_id_of(user)
        parsed = parse_callback_payload(callback.get("payload"))
        if not callback_id:
            return
        if not uid or not parsed:
            self.ack(callback_id, "Не получилось")
            return
        kind, parts = parsed
        try:
            chat_id = int(parts[0])
        except (TypeError, ValueError, IndexError):
            self.ack(callback_id, "Не получилось")
            return

        if kind == "url":
            channel = self.owned_channel(uid, chat_id)
            if not channel:
                self.ack(callback_id, "Нет доступа")
                return
            sent = self.notify_owner_link(channel, added=False)
            self.ack(callback_id, "Ссылка отправлена" if sent else "Не удалось отправить")
            return

        if kind == "setmenu":
            channel = self.owned_channel(uid, chat_id)
            if not channel:
                self.ack(callback_id, "Нет доступа")
                return
            self.ack(callback_id, "Настройки")
            self.show_settings(uid, channel)
            return

        if kind in {"setrate", "setlen"}:
            channel = self.owned_channel(uid, chat_id)
            if not channel:
                self.ack(callback_id, "Нет доступа")
                return
            action = "wait_rate" if kind == "setrate" else "wait_len"
            self.ack(callback_id, "Жду число")
            self.ask_setting(uid, chat_id, action)
            return

        if kind == "banlist":
            channel = self.owned_channel(uid, chat_id)
            if not channel:
                self.ack(callback_id, "Нет доступа")
                return
            self.ack(callback_id, "Список банов")
            self.show_bans(uid, channel)
            return

        if kind == "ban":
            if len(parts) < 2:
                self.ack(callback_id, "Не получилось")
                return
            try:
                target_id = int(parts[1])
            except ValueError:
                self.ack(callback_id, "Не получилось")
                return
            note = self.apply_ban(uid, chat_id, target_id, None)
            self.ack(callback_id, note)
            return

        if kind == "unban":
            if len(parts) < 2:
                self.ack(callback_id, "Не получилось")
                return
            try:
                target_id = int(parts[1])
            except ValueError:
                self.ack(callback_id, "Не получилось")
                return
            note = self.apply_unban(uid, chat_id, target_id)
            self.ack(callback_id, note)
            channel = self.owned_channel(uid, chat_id)
            if channel:
                self.show_bans(uid, channel)
            return

        self.ack(callback_id, "Неизвестная кнопка")

    def copy_attachments(self, attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        copied: list[dict[str, Any]] = []
        for att in attachments:
            typ = str(att.get("type") or "").strip().lower()
            url = attachment_url(att)
            if not url:
                continue
            uploaded = None
            try:
                if typ == "image":
                    uploaded = self.client.upload_image_from_url(url)
                elif typ == "video":
                    uploaded = self.client.upload_video_from_url(url)
                elif typ in {"file", "audio"}:
                    uploaded = self.client.upload_file_from_url(url)
            except Exception as exc:
                log.warning("upload %s fail: %s", typ, exc)
                uploaded = None
            if uploaded:
                copied.append(uploaded)
        return copied

    def antifraud_block(self, channel: dict[str, Any], sender_id: int, text: str) -> str | None:
        chat_id = int(channel["chat_id"])
        rate, max_chars = channel_settings(channel)
        with db() as conn:
            if is_banned(conn, chat_id, sender_id):
                return "Вы заблокированы в предложке этого канала."
            counted = count_submissions_hour(conn, chat_id, sender_id)
        blocked = rate_error(counted, rate)
        if blocked:
            return blocked
        return length_error(text, max_chars)

    def deliver_suggestion(self, channel: dict[str, Any], message: dict[str, Any]) -> bool:
        sender = sender_of(message)
        header = format_suggestion_header(channel.get("title") or "", sender)
        text = message_text(message)
        mid = message_mid(message)
        keyboard = suggestion_keyboard(int(channel["chat_id"]), sender)
        targets: list[int] = []
        for key in ("owner_user_id", "added_by_user_id"):
            raw = channel.get(key)
            if raw is None:
                continue
            uid = int(raw)
            if uid not in targets:
                targets.append(uid)
        if not targets:
            return False

        if mid:
            for uid in targets:
                if self.safe_dm(uid, header, attachments=keyboard or None, link={"type": "forward", "mid": mid}):
                    return True

        copied = self.copy_attachments(message_attachments(message))
        attachments = list(copied)
        if keyboard:
            attachments.extend(keyboard)
        combined = combine_header_and_text(header, text)
        for uid in targets:
            if self.safe_dm(uid, combined, attachments=attachments or None):
                return True
        return False

    def _erid_loop(self) -> None:
        from predlozka.config import api_base, bot_token, content_bot_token

        time.sleep(3)
        client = PredlozkaClient(token=bot_token(), base=api_base())
        sender = PredlozkaClient(token=content_bot_token(), base=api_base())
        try:
            with db() as conn:
                stats = scan_all_today(client, conn, light=False)
            log.info("erid initial scan %s", stats)
            while True:
                try:
                    send_now = False
                    with db() as conn:
                        light = not approaching_digest()
                        scan_all_today(client, conn, light=light)
                        send_now = should_send_now(conn)
                    if send_now:
                        with db() as conn:
                            send_daily_digest(client, conn, sender=sender)
                except Exception:
                    log.exception("erid loop tick fail")
                time.sleep(90)
        finally:
            client.close()
            sender.close()

    def handle_message(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if isinstance(message, dict) and not is_dialog_message(message):
            try:
                with db() as conn:
                    ingest_erid_message(conn, message)
            except Exception:
                log.exception("erid ingest fail")
        if not isinstance(message, dict) or not is_dialog_message(message):
            return
        sender = sender_of(message)
        if sender.get("is_bot"):
            return
        uid = user_id_of(sender)
        if not uid:
            return
        text = message_text(message)
        cmd = parse_command(text)
        if cmd:
            name, arg = cmd
            with db() as conn:
                clear_admin_state(conn, uid)
            if name == "start":
                self._start_user(uid, arg, sender)
                return
            if name == "url":
                self.handle_url_command(uid)
                return
            if name in {"setting", "settings"}:
                self.handle_setting_command(uid)
                return
            if name == "ban":
                self.handle_ban_command(uid)
                return
            if name in {"help", "helpme"}:
                with db() as conn:
                    owned = owner_channels(conn, uid)
                if owned:
                    self.safe_dm(
                        uid,
                        "Команды владельца:\n"
                        "/url — ссылка предложки\n"
                        "/setting — лимит сообщений и длина текста\n"
                        "/ban — список банов и разбан",
                    )
                else:
                    self.safe_dm(uid, NO_LINK_TEXT)
                return

        if self.handle_setting_input(uid, text):
            return

        with db() as conn:
            session = get_session(conn, uid)
        if not session or session.get("status") != "active":
            with db() as conn:
                owned = owner_channels(conn, uid)
            if owned:
                self.safe_dm(
                    uid,
                    "Чтобы получить ссылку предложки, отправьте /url. "
                    "Принимать чужие предложения бот будет только по уникальной ссылке.",
                )
                return
            self.safe_dm(uid, NO_LINK_TEXT)
            return

        block = self.antifraud_block(session, uid, text)
        if block:
            self.safe_dm(uid, block)
            return

        with db() as conn:
            remember_sender(
                conn,
                int(session["chat_id"]),
                uid,
                user_label(sender),
                str(sender.get("username") or ""),
            )

        ok = self.deliver_suggestion(session, message)
        if ok:
            with db() as conn:
                add_submission(conn, int(session["chat_id"]), uid)
            self.safe_dm(uid, "Отправлено владельцу канала.")
        else:
            self.safe_dm(uid, DELIVER_FAIL)

    def handle_comment(self, update: dict[str, Any]) -> None:
        if not comment_guard_enabled():
            return
        parsed = parse_comment_update(update)
        if not parsed:
            log.warning("comment update parse fail keys=%s", sorted(update.keys()))
            return
        decision = evaluate_comment(parsed, bot_user_id=self.bot_user_id)
        if decision.action != "delete":
            return
        ok = 0
        fail = 0
        for cid in decision.delete_mids:
            try:
                self.client.delete_comment(parsed.post_mid, cid)
                ok += 1
            except Exception as exc:
                fail += 1
                log.warning(
                    "comment delete fail post=%s comment=%s: %s",
                    parsed.post_mid,
                    cid,
                    exc,
                )
        log.info(
            "comment guard delete reason=%s user=%s chat=%s ok=%s fail=%s text=%r",
            decision.reason,
            parsed.user_id,
            parsed.chat_id,
            ok,
            fail,
            (parsed.text or "")[:80],
        )

    def handle_update(self, update: dict[str, Any]) -> None:
        ut = str(update.get("update_type") or "")
        if ut == "bot_added":
            self.handle_bot_added(update)
        elif ut == "bot_removed":
            self.handle_bot_removed(update)
        elif ut == "bot_started":
            self.handle_bot_started(update)
        elif ut == "bot_stopped":
            self.handle_bot_stopped(update)
        elif ut == "chat_title_changed":
            self.handle_title_changed(update)
        elif ut == "message_created":
            self.handle_message(update)
        elif ut == "message_callback":
            self.handle_callback(update)
        elif ut in {"comment_created", "comment_edited"}:
            self.handle_comment(update)

    def poll_once(self, timeout: int = 30) -> None:
        with db() as conn:
            marker_raw = get_meta(conn, "updates_marker", "")
        marker = int(marker_raw) if marker_raw.isdigit() else None
        data = self.client.get_updates(
            marker=marker,
            limit=100,
            timeout=timeout,
            types=UPDATE_TYPES,
        )
        updates = data.get("updates") or []
        new_marker = data.get("marker")
        for update in updates:
            if not isinstance(update, dict):
                continue
            try:
                self.handle_update(update)
            except Exception:
                log.exception("ошибка обработки update %s", update.get("update_type"))
        if new_marker is not None:
            with db() as conn:
                set_meta(conn, "updates_marker", str(new_marker))

    def run(self) -> None:
        log.info(
            "predlozka polling bot=@%s id=%s",
            self.bot_username,
            self.bot_user_id,
        )
        while True:
            try:
                self.poll_once()
            except MaxApiError as exc:
                log.warning("updates fail: %s", exc)
                time.sleep(3)
            except Exception:
                log.exception("poll crash")
                time.sleep(3)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    from predlozka.config import api_base, bot_token, load_dotenv

    load_dotenv()
    init_db()
    client = PredlozkaClient(token=bot_token(), base=api_base())
    try:
        PredlozkaBot(client).run()
    finally:
        client.close()
