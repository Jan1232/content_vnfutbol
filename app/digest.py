"""Суточная сводка: прирост просмотров + нетто подписки/отписки по MAX-каналам."""

from __future__ import annotations

import html as html_lib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.db import db, init_db, list_active_max_chat_ids, record_participant_snapshot
from app.max_api import MaxApiError, MaxClient

# MaxStars: сумма текущих просмотров постов, вышедших за 24/48/72 часа.
VIEW_LOOKBACK_SEC = 72 * 3600 + 3600


def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name or "Asia/Yekaterinburg")


def day_bounds(day: datetime, tz: ZoneInfo) -> tuple[float, float]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.timestamp(), end.timestamp()


def fmt_int(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def _views(msg: dict[str, Any]) -> int:
    stat = msg.get("stat") or {}
    if not isinstance(stat, dict):
        return 0
    try:
        return int(stat.get("views") or 0)
    except (TypeError, ValueError):
        return 0


def _mid(msg: dict[str, Any]) -> str:
    body = msg.get("body") or {}
    if isinstance(body, dict) and body.get("mid"):
        return str(body["mid"])
    return str(msg.get("url") or "")


def collect_channel_posts(
    client: MaxClient,
    chat_id: int,
    *,
    since_ts: float,
) -> list[dict[str, Any]]:
    """Посты новее since_ts: mid, views, post_ts."""
    since_ms = int(since_ts * 1000)
    out: list[dict[str, Any]] = []
    cursor_from: int | None = None
    seen: set[str] = set()
    for _ in range(40):
        try:
            batch = client.list_messages(chat_id, count=100, from_ms=cursor_from)
        except MaxApiError:
            break
        if not batch:
            break
        oldest = None
        progressed = False
        stop = False
        for msg in batch:
            mid = _mid(msg)
            ts_ms = int(msg.get("timestamp") or 0)
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            progressed = True
            if oldest is None or ts_ms < oldest:
                oldest = ts_ms
            if ts_ms < since_ms:
                stop = True
                continue
            if not mid:
                continue
            out.append(
                {
                    "mid": mid,
                    "views": _views(msg),
                    "post_ts": ts_ms / 1000.0,
                }
            )
        if stop or not progressed or oldest is None:
            break
        if cursor_from is not None and oldest >= cursor_from:
            break
        cursor_from = oldest
        if oldest < since_ms:
            break
    return out


def save_view_snapshot(
    conn,
    chat_id: int,
    posts: list[dict[str, Any]],
    *,
    captured_at: float,
) -> None:
    conn.executemany(
        """
        INSERT INTO post_view_snapshots(chat_id, mid, views, post_ts, captured_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                int(chat_id),
                p["mid"],
                int(p["views"] or 0),
                float(p["post_ts"] or 0),
                float(captured_at),
            )
            for p in posts
            if p.get("mid")
        ],
    )
    conn.execute(
        "DELETE FROM post_view_snapshots WHERE captured_at < ?",
        (float(captured_at) - 16 * 24 * 3600,),
    )


def previous_views_map(conn, chat_id: int, *, before_ts: float) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT MAX(captured_at) AS ts FROM post_view_snapshots
        WHERE chat_id=? AND captured_at < ?
        """,
        (int(chat_id), float(before_ts) - 1),
    ).fetchone()
    prev_at = float(row["ts"] or 0) if row else 0.0
    if not prev_at:
        return {}
    rows = conn.execute(
        """
        SELECT mid, views FROM post_view_snapshots
        WHERE chat_id=? AND captured_at=?
        """,
        (int(chat_id), prev_at),
    ).fetchall()
    return {str(r["mid"]): int(r["views"] or 0) for r in rows}


def views_gained(
    posts: list[dict[str, Any]],
    prev: dict[str, int],
) -> int:
    total = 0
    for p in posts:
        mid = p.get("mid") or ""
        cur = int(p.get("views") or 0)
        old = prev.get(mid)
        if old is None:
            total += max(cur, 0)
        else:
            total += max(cur - old, 0)
    return total


def views_of_posts_in_window(
    posts: list[dict[str, Any]],
    start_ts: float,
    end_ts: float,
) -> tuple[int, int]:
    """Как MaxStars: сумма текущих stat.views у постов с датой в окне."""
    views = 0
    n_posts = 0
    for p in posts:
        ts = float(p.get("post_ts") or 0)
        if start_ts <= ts < end_ts:
            views += max(int(p.get("views") or 0), 0)
            n_posts += 1
    return views, n_posts


def er_percent(views: int, subscribers: int) -> float:
    if subscribers <= 0:
        return 0.0
    return 100.0 * max(int(views), 0) / float(subscribers)


ERR24_SAMPLE = 20
ERR24_MIN_AGE_SEC = 24 * 3600
ERR24_LOOKBACK_SEC = 14 * 24 * 3600


def median_number(values: list[int] | list[float]) -> float:
    xs = sorted(float(v) for v in values)
    n = len(xs)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def reach_sample_24h(
    posts: list[dict[str, Any]],
    *,
    now_ts: float,
    sample: int = ERR24_SAMPLE,
    min_age_sec: int = ERR24_MIN_AGE_SEC,
) -> list[int]:
    """Просмотры последних N постов, которым уже есть полные 24 часа."""
    aged: list[dict[str, Any]] = []
    for p in posts:
        ts = float(p.get("post_ts") or 0)
        if ts and now_ts - ts >= min_age_sec:
            aged.append(p)
    aged.sort(key=lambda p: -float(p.get("post_ts") or 0))
    return [max(int(p.get("views") or 0), 0) for p in aged[: max(1, int(sample))]]


def err24_percent(avg_reach: float, subscribers: int) -> float:
    """ERR24 = средний (медианный) охват поста за 24ч ÷ подписчики × 100%."""
    if subscribers <= 0:
        return 0.0
    return 100.0 * max(float(avg_reach), 0.0) / float(subscribers)


def is_city_podslushano_channel(title: str) -> bool:
    """Городские каналы «Город подслушано» не входят в ежедневный ERR24."""
    t = " ".join((title or "").split()).casefold()
    return t.endswith(" подслушано") or t == "подслушано"


def fmt_pct(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}%"
    return f"{value:.1f}%"


def predlozka_owner_id() -> int:
    import os

    ids = parse_user_ids((os.environ.get("PREDLOZKA_GUARD_OWNER") or "").strip())
    if ids:
        return ids[0]
    ids = owner_user_ids()
    return ids[0] if ids else 0


def snapshot_at_or_before(conn, chat_id: int, ts: float) -> int | None:
    row = conn.execute(
        """
        SELECT participants_count FROM channel_count_snapshots
        WHERE chat_id=? AND captured_at<=?
        ORDER BY captured_at DESC LIMIT 1
        """,
        (int(chat_id), float(ts)),
    ).fetchone()
    if not row:
        return None
    return int(row["participants_count"] or 0)


def has_previous_view_snapshots(conn, *, before_ts: float) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM post_view_snapshots
        WHERE captured_at < ? LIMIT 1
        """,
        (float(before_ts) - 1,),
    ).fetchone()
    return bool(row)


@dataclass
class ChannelDigest:
    chat_id: int
    title: str
    link: str
    participants: int
    views: int
    posts: int
    joined: int
    left: int
    delta: int
    views_48: int = 0
    views_72: int = 0


def build_digest(
    client: MaxClient,
    *,
    start_ts: float,
    end_ts: float,
    refresh: bool = True,
) -> list[ChannelDigest]:
    init_db()
    captured_at = time.time()
    with db() as conn:
        chat_ids = list_active_max_chat_ids(conn)
        titles = {
            int(r["chat_id"]): (
                r["title"] or str(r["chat_id"]),
                r["link"] or "",
                int(r["participants_count"] or 0),
            )
            for r in conn.execute(
                """
                SELECT chat_id, title, link, participants_count
                FROM channels
                WHERE COALESCE(platform,'max')='max'
                  AND status='active'
                  AND COALESCE(hidden,0)=0
                """
            )
        }

    lookback = min(start_ts, end_ts - VIEW_LOOKBACK_SEC)
    out: list[ChannelDigest] = []
    for chat_id in chat_ids:
        title, link, db_count = titles.get(int(chat_id), (str(chat_id), "", 0))
        live_count = db_count
        if refresh:
            try:
                chat = client.get_chat(int(chat_id))
                live_count = int(chat.get("participants_count") or db_count)
                title = str(chat.get("title") or title)
                link = str(chat.get("link") or link)
                with db() as conn:
                    from app.db import upsert_channel

                    upsert_channel(conn, chat)
                    record_participant_snapshot(
                        conn, int(chat_id), live_count, now=captured_at, min_interval_sec=0
                    )
            except MaxApiError:
                pass
        try:
            posts = collect_channel_posts(client, int(chat_id), since_ts=lookback)
        except MaxApiError:
            posts = []
        with db() as conn:
            save_view_snapshot(conn, int(chat_id), posts, captured_at=captured_at)
            start_n = snapshot_at_or_before(conn, int(chat_id), start_ts)
            end_n = snapshot_at_or_before(conn, int(chat_id), end_ts)
        views, published = views_of_posts_in_window(posts, end_ts - 24 * 3600, end_ts)
        views_48, _ = views_of_posts_in_window(posts, end_ts - 48 * 3600, end_ts)
        views_72, _ = views_of_posts_in_window(posts, end_ts - 72 * 3600, end_ts)
        if end_n is None:
            end_n = live_count
        if start_n is None:
            joined = 0
            left = 0
            delta = 0
        else:
            delta = int(end_n) - int(start_n)
            joined = max(delta, 0)
            left = max(-delta, 0)
        out.append(
            ChannelDigest(
                chat_id=int(chat_id),
                title=title,
                link=link,
                participants=int(live_count),
                views=int(views),
                posts=int(published),
                joined=int(joined),
                left=int(left),
                delta=int(delta),
                views_48=int(views_48),
                views_72=int(views_72),
            )
        )
        time.sleep(0.08)
    return out


def format_digest(
    rows: list[ChannelDigest],
    *,
    day_label: str,
    deltas_ready: bool,
    views_ready: bool = True,
) -> str:
    total_views = sum(r.views for r in rows)
    total_views_48 = sum(r.views_48 for r in rows)
    total_views_72 = sum(r.views_72 for r in rows)
    total_posts = sum(r.posts for r in rows)
    total_joined = sum(r.joined for r in rows)
    total_left = sum(r.left for r in rows)
    total_subs = sum(r.participants for r in rows)
    lines = [
        f"📊 <b>Сводка сети</b> · {html_lib.escape(day_label)}",
        "",
        f"Каналов: <b>{len(rows)}</b>",
        f"Подписчиков: <b>{fmt_int(total_subs)}</b>",
        f"Постов за 24ч: <b>{fmt_int(total_posts)}</b>",
        "",
        "👁 <b>Просмотры постов за окно</b>",
        f"24ч: <b>{fmt_int(total_views)}</b>",
        f"48ч: <b>{fmt_int(total_views_48)}</b>",
        f"72ч: <b>{fmt_int(total_views_72)}</b>",
    ]
    if deltas_ready:
        lines += [
            f"Подписалось (нетто +): <b>{fmt_int(total_joined)}</b>",
            f"Отписалось (нетто −): <b>{fmt_int(total_left)}</b>",
        ]
    else:
        lines.append("Подписки/отписки: точка отсчёта снята, цифры будут со следующего дня.")

    def _name(r: ChannelDigest) -> str:
        title = html_lib.escape(r.title or str(r.chat_id))
        if r.link:
            return f'<a href="{html_lib.escape(r.link, quote=True)}">{title}</a>'
        return title

    top_views = sorted(rows, key=lambda r: -r.views)[:3]
    lines += ["", "🏆 <b>Топ-3 канала за 24ч</b>"]
    if any(r.views > 0 for r in top_views):
        for i, r in enumerate(top_views, 1):
            lines.append(
                f"{i}. {_name(r)} — {fmt_int(r.views)} просм., "
                f"{fmt_int(r.participants)} подп."
            )
    else:
        lines.append("за 24ч постов не было")
    top_in = sorted([r for r in rows if r.joined > 0], key=lambda r: -r.joined)[:3]
    top_out = sorted([r for r in rows if r.left > 0], key=lambda r: -r.left)[:3]
    if deltas_ready:
        lines += ["", "🔥 <b>Топ-3 по подпискам</b>"]
        if top_in:
            for i, r in enumerate(top_in, 1):
                lines.append(f"{i}. {_name(r)} — +{fmt_int(r.joined)}")
        else:
            lines.append("без заметного прироста")
        lines += ["", "📉 <b>Топ-3 по отпискам</b>"]
        if top_out:
            for i, r in enumerate(top_out, 1):
                lines.append(f"{i}. {_name(r)} — −{fmt_int(r.left)}")
        else:
            lines.append("без заметных отписок")
    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3890] + "…"
    return text


@dataclass
class Err24Row:
    chat_id: int
    title: str
    link: str
    participants: int
    reach: float
    posts: int
    err24: float


def build_err24(
    client: MaxClient,
    *,
    now_ts: float | None = None,
    owner_id: int | None = None,
) -> list[Err24Row]:
    """ERR24 по каналам предложки, где owner_id — владелец канала."""
    from predlozka.db import db as pdb
    from predlozka.db import list_active_channels

    owner = int(owner_id or predlozka_owner_id())
    now = float(now_ts or time.time())
    with pdb() as conn:
        channels = list_active_channels(conn)
    out: list[Err24Row] = []
    for ch in channels:
        chat_id = int(ch["chat_id"])
        title = str(ch.get("title") or chat_id)
        if is_city_podslushano_channel(title):
            continue
        link = ""
        live_count = 0
        api_owner = 0
        try:
            chat = client.get_chat(chat_id)
            title = str(chat.get("title") or title)
            link = str(chat.get("link") or "")
            live_count = int(chat.get("participants_count") or 0)
            api_owner = int(chat.get("owner_id") or 0)
        except (MaxApiError, TypeError, ValueError):
            api_owner = int(ch.get("owner_user_id") or 0)
            live_count = 0
        if is_city_podslushano_channel(title):
            continue
        if api_owner != owner:
            time.sleep(0.05)
            continue
        try:
            posts = collect_channel_posts(
                client, chat_id, since_ts=now - ERR24_LOOKBACK_SEC
            )
        except MaxApiError:
            posts = []
        sample = reach_sample_24h(posts, now_ts=now)
        reach = median_number(sample) if sample else 0.0
        err = err24_percent(reach, live_count)
        out.append(
            Err24Row(
                chat_id=chat_id,
                title=title,
                link=link,
                participants=int(live_count),
                reach=float(reach),
                posts=len(sample),
                err24=float(err),
            )
        )
        time.sleep(0.08)
    out.sort(key=lambda r: (-r.err24, (r.title or "").casefold()))
    return out


def format_err24_messages(
    rows: list[Err24Row],
    *,
    day_label: str,
    limit: int = 3900,
) -> list[str]:
    def _name(r: Err24Row) -> str:
        title = html_lib.escape(r.title or str(r.chat_id))
        if r.link:
            return f'<a href="{html_lib.escape(r.link, quote=True)}">{title}</a>'
        return title

    header = [
        f"📈 <b>ERR24</b> · {html_lib.escape(day_label)}",
        "медианный охват поста за 24ч ÷ подписчики × 100%",
        f"Каналов (предложка, вы владелец): <b>{len(rows)}</b>",
        "",
    ]
    lines = [f"{_name(r)} — <b>{fmt_pct(r.err24)}</b> (охват {fmt_int(int(round(r.reach)))}, {fmt_int(r.participants)} подп., {r.posts} постов)" for r in rows]
    if not lines:
        return ["\n".join(header + ["нет каналов"])]
    chunks: list[str] = []
    buf = list(header)
    for line in lines:
        trial = "\n".join(buf + [line])
        if buf != header and len(trial) > limit:
            chunks.append("\n".join(buf))
            buf = [
                f"📈 <b>ERR24</b> · продолжение",
                "",
                line,
            ]
        else:
            buf.append(line)
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def parse_user_ids(raw: str) -> list[int]:
    out: list[int] = []
    seen: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            uid = int(part)
        except ValueError:
            continue
        if uid and uid not in seen:
            seen.add(uid)
            out.append(uid)
    return out


def owner_user_ids() -> list[int]:
    s = get_settings()
    ids = parse_user_ids(str(s.digest_owner_user_id or ""))
    if ids:
        return ids
    import os

    return parse_user_ids((os.environ.get("PREDLOZKA_GUARD_OWNER") or "").strip())


def owner_user_id() -> int:
    ids = owner_user_ids()
    return ids[0] if ids else 0


def send_digest(text: str, *, user_id: int | None = None) -> dict[str, Any]:
    targets = [int(user_id)] if user_id else owner_user_ids()
    if not targets:
        raise RuntimeError("не задан DIGEST_OWNER_USER_ID / PREDLOZKA_GUARD_OWNER")
    last: dict[str, Any] = {}
    with MaxClient() as client:
        for uid in targets:
            last = client.send_message(
                user_id=int(uid),
                text=text,
                format="html",
                notify=True,
            )
            time.sleep(0.2)
    return last
