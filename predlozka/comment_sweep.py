"""Разовый обход комментариев в канале после добавления бота."""

from __future__ import annotations

import logging
import time
from typing import Any

from predlozka.client import PredlozkaClient
from predlozka.comment_guard import (
    CommentFloodTracker,
    NearDupTracker,
    UserBurstTracker,
    evaluate_comment,
    parse_api_comment,
    sweep_post_limit,
)

log = logging.getLogger("predlozka")


def fetch_all_comments(
    client: PredlozkaClient,
    post_mid: str,
    *,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    before: int | None = None
    for _ in range(max_pages):
        batch = client.list_comments(post_mid, count=100, before=before)
        if not batch:
            break
        out.extend(batch)
        ts = [int(m.get("timestamp") or 0) for m in batch]
        oldest = min(ts) if ts else None
        if oldest is None or len(batch) < 100:
            break
        if before is not None and oldest >= before:
            break
        before = oldest
        time.sleep(0.08)
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for m in out:
        body = m.get("body") if isinstance(m.get("body"), dict) else {}
        mid = str(body.get("mid") or "")
        if mid and mid in seen:
            continue
        if mid:
            seen.add(mid)
        uniq.append(m)
    return uniq


def post_mid_of(msg: dict[str, Any]) -> str:
    body = msg.get("body") if isinstance(msg.get("body"), dict) else {}
    return str(body.get("mid") or msg.get("mid") or "").strip()


def sweep_channel_comments(
    client: PredlozkaClient,
    chat_id: int,
    *,
    bot_user_id: int = 0,
    post_limit: int | None = None,
    delete: bool = True,
    sleep_sec: float = 0.25,
) -> dict[str, int]:
    """Последние N постов: удалить комментарии, подходящие под фильтр."""
    limit = post_limit if post_limit is not None else sweep_post_limit()
    posts = client.list_messages(int(chat_id), count=limit)
    stats = {
        "posts": len(posts),
        "comments": 0,
        "delete": 0,
        "ok": 0,
        "fail": 0,
    }
    log.info("comment sweep start chat=%s posts=%s", chat_id, len(posts))
    for post in posts:
        pmid = post_mid_of(post)
        if not pmid:
            continue
        try:
            raw = fetch_all_comments(client, pmid)
        except Exception:
            log.exception("comment sweep fetch fail post=%s", pmid)
            continue
        parsed = []
        for m in raw:
            item = parse_api_comment(m, pmid, chat_id)
            if item:
                parsed.append(item)
        parsed.sort(key=lambda x: x.timestamp_ms)
        stats["comments"] += len(parsed)

        flood = CommentFloodTracker()
        near = NearDupTracker()
        burst = UserBurstTracker()
        to_delete: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for item in parsed:
            decision = evaluate_comment(
                item,
                bot_user_id=bot_user_id,
                tracker=flood,
                near_tracker=near,
                burst_tracker=burst,
                now=(item.timestamp_ms / 1000.0) if item.timestamp_ms else None,
            )
            if decision.action != "delete":
                continue
            for cid in decision.delete_mids:
                if cid in seen:
                    continue
                seen.add(cid)
                to_delete.append((pmid, cid, decision.reason))

        stats["delete"] += len(to_delete)
        if not delete:
            continue
        for post_mid, cid, reason in to_delete:
            try:
                client.delete_comment(post_mid, cid)
                stats["ok"] += 1
            except Exception as exc:
                stats["fail"] += 1
                log.warning(
                    "comment sweep delete fail post=%s comment=%s: %s",
                    post_mid,
                    cid,
                    exc,
                )
            time.sleep(sleep_sec)
        if to_delete:
            log.info(
                "comment sweep post=%s deleted=%s reasons=%s",
                pmid[-12:],
                len(to_delete),
                sorted({r for _, _, r in to_delete}),
            )
    log.info("comment sweep done chat=%s %s", chat_id, stats)
    return stats


def known_channel_ids() -> list[int]:
    """Каналы из БД предложки + MAX-каналы админки."""
    ids: set[int] = set()
    from predlozka.db import connect as pred_connect
    from predlozka.config import ROOT

    try:
        conn = pred_connect()
        try:
            rows = conn.execute(
                "SELECT chat_id FROM channels WHERE status='active'"
            ).fetchall()
            ids.update(int(r["chat_id"]) for r in rows)
        finally:
            conn.close()
    except Exception:
        log.exception("predlozka channels read fail")

    app_db = ROOT / "data" / "app.db"
    if app_db.is_file():
        import sqlite3

        conn = sqlite3.connect(str(app_db))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT chat_id FROM channels
                WHERE status='active'
                  AND COALESCE(hidden, 0)=0
                  AND COALESCE(platform, 'max')='max'
                """
            ).fetchall()
            ids.update(int(r["chat_id"]) for r in rows)
        except Exception:
            log.exception("app.db channels read fail")
        finally:
            conn.close()
    return sorted(ids, reverse=True)


def bot_can_moderate(client: PredlozkaClient, chat_id: int, bot_user_id: int) -> tuple[bool, str]:
    try:
        chat = client.get_chat(int(chat_id))
    except Exception as exc:
        return False, f"get_chat:{exc}"
    title = str(chat.get("title") or chat_id)
    ctype = str(chat.get("type") or chat.get("chat_type") or "")
    status = str(chat.get("status") or "active")
    if status and status not in {"active", ""}:
        return False, f"status:{status}:{title}"
    if ctype and ctype not in {"channel", "chat", ""}:
        return False, f"type:{ctype}:{title}"
    try:
        admins = client.get_admins(int(chat_id))
    except Exception as exc:
        return False, f"admins:{exc}:{title}"
    mine = None
    for a in admins:
        try:
            if int(a.get("user_id") or 0) == int(bot_user_id):
                mine = a
                break
        except (TypeError, ValueError):
            continue
    if not mine:
        return False, f"not_admin:{title}"
    perms = {str(p) for p in (mine.get("permissions") or [])}
    need = {"delete", "read_all_messages"}
    if not need.issubset(perms):
        return False, f"no_perms:{sorted(need - perms)}:{title}"
    return True, title


def sweep_all_admin_channels(
    client: PredlozkaClient,
    *,
    bot_user_id: int,
    delete: bool = True,
) -> list[dict[str, Any]]:
    chat_ids = known_channel_ids()
    log.info("comment sweep-all candidates=%s", len(chat_ids))
    results: list[dict[str, Any]] = []
    for i, chat_id in enumerate(chat_ids, start=1):
        ok, title = bot_can_moderate(client, chat_id, bot_user_id)
        if not ok:
            log.info("comment sweep skip %s/%s chat=%s %s", i, len(chat_ids), chat_id, title)
            results.append({"chat_id": chat_id, "skip": title})
            continue
        log.info(
            "comment sweep run %s/%s chat=%s title=%r",
            i,
            len(chat_ids),
            chat_id,
            title,
        )
        try:
            stats = sweep_channel_comments(
                client,
                chat_id,
                bot_user_id=bot_user_id,
                delete=delete,
            )
            results.append({"chat_id": chat_id, "title": title, **stats})
        except Exception as exc:
            log.exception("comment sweep fail chat=%s", chat_id)
            results.append({"chat_id": chat_id, "title": title, "error": str(exc)})
    return results


def main() -> None:
    import argparse

    from predlozka.config import api_base, bot_token, load_dotenv

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    load_dotenv()
    parser = argparse.ArgumentParser(description="Обход комментариев предложки")
    parser.add_argument("--all", action="store_true", help="все каналы, где бот админ")
    parser.add_argument("--chat-id", type=int, action="append", default=[])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.all and not args.chat_id:
        parser.error("нужен --all или --chat-id")

    client = PredlozkaClient(token=bot_token(), base=api_base())
    try:
        me = client.me()
        bot_id = int(me.get("user_id") or 0)
        if args.all:
            rows = sweep_all_admin_channels(
                client, bot_user_id=bot_id, delete=not args.dry_run
            )
        else:
            rows = []
            for cid in args.chat_id:
                rows.append(
                    sweep_channel_comments(
                        client,
                        int(cid),
                        bot_user_id=bot_id,
                        delete=not args.dry_run,
                    )
                    | {"chat_id": int(cid)}
                )
        deleted = sum(int(r.get("ok") or 0) for r in rows)
        skipped = sum(1 for r in rows if r.get("skip"))
        print(
            f"done channels={len(rows)} skipped={skipped} deleted_ok={deleted}",
            flush=True,
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()

