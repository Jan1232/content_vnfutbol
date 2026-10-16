"""Антиспам комментариев в каналах (события comment_created)."""

from __future__ import annotations

import os
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


# Ссылки / инвайты / типичный спам-мусор
_LINK_RE = re.compile(
    r"(?i)("
    r"https?://"
    r"|www\."
    r"|\bmax\.ru\b"
    r"|\bt\.me\b"
    r"|telegram\.me"
    r"|telegram\.dog"
    r"|joinchat"
    r"|/\+?[Jj]oin\b"
    r"|\bjoinchat\b"
    r"|vk\.com/(?:club|public|wall|@)"
    r"|bit\.ly"
    r"|goo\.gl"
    r"|tinyurl\.com"
    r"|clck\.ru"
    r")"
)

# Невидимый / «ломающий» мусор
_INVISIBLE_RE = re.compile(
    r"[\u200b\u200c\u200d\u2060\ufeff\u00a0\u2028\u2029\u2800\u3164]+"
)

# Голые цифры: 1, 10, №3, #7, 1., 2), 1️⃣
_DIGITS_ONLY_RE = re.compile(
    r"^[\s#№Nnoо\.\,\:\;\-\—\)\(\[\]«»\"']*"
    r"\d{1,6}"
    r"[\s#№Nnoо\.\,\:\;\-\—\)\(\[\]«»\"']*$",
    re.IGNORECASE,
)

# Числа словами / порядковые (целиком комментарий)
_NUMBER_WORDS = frozenset(
    {
        # количественные
        "ноль",
        "нуль",
        "один",
        "одна",
        "одно",
        "два",
        "две",
        "три",
        "четыре",
        "пять",
        "шесть",
        "семь",
        "восемь",
        "девять",
        "десять",
        "одиннадцать",
        "двенадцать",
        "тринадцать",
        "четырнадцать",
        "пятнадцать",
        "шестнадцать",
        "семнадцать",
        "восемнадцать",
        "девятнадцать",
        "двадцать",
        "тридцать",
        "сорок",
        "пятьдесят",
        "шестьдесят",
        "семьдесят",
        "восемьдесят",
        "девяносто",
        "сто",
        "тысяча",
        # порядковые
        "первый",
        "первая",
        "первое",
        "первым",
        "первой",
        "второй",
        "вторая",
        "второе",
        "вторым",
        "третий",
        "третья",
        "третье",
        "третьим",
        "четвёртый",
        "четвертый",
        "четвёртая",
        "четвертая",
        "четвёртое",
        "четвертое",
        "пятый",
        "пятая",
        "пятое",
        "шестой",
        "шестая",
        "шестое",
        "седьмой",
        "седьмая",
        "седьмое",
        "восьмой",
        "восьмая",
        "восьмое",
        "девятый",
        "девятая",
        "девятое",
        "десятый",
        "десятая",
        "десятое",
        "одиннадцатый",
        "двенадцатый",
        "тринадцатый",
        "двадцатый",
        "тридцатый",
        "сотый",
        # разг. / сокращения
        "перв",
        "втор",
        "трет",
        "1й",
        "1ый",
        "1-й",
        "1-ый",
        "2й",
        "2ой",
        "2-й",
        "2-ой",
        "3й",
        "3ий",
        "3-й",
        "3-ий",
        # en
        "one",
        "two",
        "three",
        "four",
        "five",
        "six",
        "seven",
        "eight",
        "nine",
        "ten",
        "first",
        "second",
        "third",
        "fourth",
        "fifth",
    }
)

_ORDINAL_SUFFIX_RE = re.compile(
    r"^(?P<n>\d{1,6})\s*[\-\.]?\s*(?:й|ый|ой|ий|я|ая|е|ое|st|nd|rd|th)$",
    re.IGNORECASE,
)

# Стены из одного символа / эмодзи: ааааааа, 💙❤💙❤…
_CHAR_REPEAT_RE = re.compile(r"(.)\1{7,}", re.DOTALL)
_PROMO_RE = re.compile(
    r"(?i)("
    r"\bподписывай(?:ся|тесь)?\b"
    r"|\bподпишись\b"
    r"|\bподписывайтесь\s+каждый\b"
    r"|канал[^\n]{0,24}профил"
    r"|профил[^\n]{0,24}канал"
    r"|цель[^\n]{0,40}подписчик"
    r"|наша\s+цель[^\n]{0,20}\d+"
    r"|\bпереходи(?:те)?\b"
    r"|\bзаработок\b"
    r"|\bказино\b"
    r"|\bставк[ауи]\b"
    r"|\bпромокод\b"
    r"|\bнакрутка\b"
    r"|бесплатн\w*\s+подпис"
    r")"
)

# Короткие живые реакции — не трогаем.
_SHORT_OK = frozenset(
    {
        "ок",
        "окей",
        "да",
        "нет",
        "неа",
        "ага",
        "угу",
        "ого",
        "вау",
        "лол",
        "кек",
        "го",
        "топ",
        "имба",
        "жиза",
        "жесть",
        "огонь",
        "красава",
        "мощно",
        "факты",
        "гол",
        "мяч",
        "огонь",
        "суета",
        "база",
        "кринж",
        "рофл",
        "хах",
        "ахах",
        "хаха",
        "лолс",
        "ппц",
        "пипец",
        "ну",
        "эм",
        "хм",
        "мм",
        "мда",
        "норм",
        "ясно",
        "понял",
        "плюс",
        "минус",
        "yes",
        "no",
        "ok",
        "lol",
        "omg",
        "wtf",
        "gg",
        "wp",
        "ez",
        "rip",
    }
)


@dataclass(frozen=True)
class ParsedComment:
    post_mid: str
    comment_mid: str
    chat_id: int | None
    user_id: int
    text: str
    is_bot: bool
    timestamp_ms: int
    is_admin: bool = False
    is_owner: bool = False


@dataclass
class GuardDecision:
    action: str  # ok | delete
    reason: str = ""
    delete_mids: tuple[str, ...] = ()  # comment mids to remove (same post)


def comment_guard_enabled() -> bool:
    return _env_bool("PREDLOZKA_COMMENT_GUARD", True)


def dup_limit() -> int:
    return max(2, _env_int("PREDLOZKA_COMMENT_DUP_LIMIT", 5))


def dup_window_sec() -> float:
    return max(10.0, _env_float("PREDLOZKA_COMMENT_DUP_WINDOW_SEC", 60.0))


def near_limit() -> int:
    """Срабатывает при count >= этого числа за окно."""
    return max(3, _env_int("PREDLOZKA_COMMENT_NEAR_LIMIT", 5))


def near_dist() -> int:
    return max(1, _env_int("PREDLOZKA_COMMENT_NEAR_DIST", 4))


def near_min_len() -> int:
    return max(3, _env_int("PREDLOZKA_COMMENT_NEAR_MIN_LEN", 4))


def burst_limit() -> int:
    """Сколько комментов одного юзера под одним постом за окно → бан-пачка."""
    return max(3, _env_int("PREDLOZKA_COMMENT_BURST_LIMIT", 4))


def burst_window_sec() -> float:
    return max(8.0, _env_float("PREDLOZKA_COMMENT_BURST_WINDOW_SEC", 30.0))


def sweep_post_limit() -> int:
    return max(1, _env_int("PREDLOZKA_COMMENT_SWEEP_POSTS", 50))


def sweep_on_join_enabled() -> bool:
    return _env_bool("PREDLOZKA_COMMENT_SWEEP_ON_JOIN", True)


def max_len() -> int:
    return max(50, _env_int("PREDLOZKA_COMMENT_MAX_LEN", 800))


def blank_ratio_limit() -> float:
    return min(0.95, max(0.5, _env_float("PREDLOZKA_COMMENT_BLANK_RATIO", 0.75)))


def privileged_user_ids() -> set[int]:
    ids: set[int] = set()
    for key in (
        "PREDLOZKA_GUARD_OWNER",
        "DIGEST_OWNER_USER_ID",
        "PREDLOZKA_ERID_OWNER",
    ):
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        for part in raw.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                uid = int(part)
            except ValueError:
                continue
            if uid:
                ids.add(uid)
    return ids


# «Чё мои комментарии удаляют», «комменты сносят», «бот чистит коммы»
_COMMENT_DELETE_WHINE_RE = re.compile(
    r"(?iu)("
    r"(?:коммент\w*|комм(?:ентари\w*|енты|ы|ов)?|коменты|комы)\s+"
    r"(?:удаля\w*|удалил\w*|снос\w*|чист\w*|бан\w*)"
    r"|"
    r"(?:удаля\w*|удалил\w*|снос\w*|чист\w*)\s+"
    r"(?:мои\s+|наши\s+|все\s+)?"
    r"(?:коммент\w*|комм(?:ентари\w*|енты|ы)?|коменты|комы)"
    r"|"
    r"(?:чё|что|почему|зачем)\s+(?:вы\s+|тут\s+)?"
    r"(?:мои\s+)?(?:коммент\w*|коммы|коменты)\s+удал"
    r"|"
    r"бот\s+(?:удаля\w*|снос\w*|чист\w*)\s+ком"
    r")"
)

# «суки» не покрывается корнем «сука»
_COMMENT_INSULT_RE = re.compile(
    r"(?iu)(?<![а-яёa-z])сук[аиеуоы](?![а-яёa-z])"
)


def has_profanity(text: str) -> bool:
    from app.filter import contains_profanity

    raw = text or ""
    if not raw.strip():
        return False
    if contains_profanity(raw):
        return True
    return bool(_COMMENT_INSULT_RE.search(raw))


def is_delete_complaint(text: str) -> bool:
    return bool(_COMMENT_DELETE_WHINE_RE.search(text or ""))


def normalize_text(text: str) -> str:
    t = _INVISIBLE_RE.sub("", text or "")
    t = re.sub(r"\s+", " ", t, flags=re.UNICODE).strip().lower()
    return t


def has_spam_link(text: str) -> bool:
    return bool(_LINK_RE.search(text or ""))


def is_number_only_spam(text: str) -> bool:
    """
    Комментарий целиком — число или числительное:
    «1», «10», «№3», «первый», «второй», «2-й».
    Обычные фразы вроде «1 место» / «второй тайм» не режем.
    """
    norm = normalize_text(text)
    if not norm:
        return False
    # убрать типичную обёртку эмодзи/точек вокруг числа
    compact = re.sub(r"^[\.\!\?…❤️🔥👑✅️\s]+|[\.\!\?…❤️🔥👑✅️\s]+$", "", norm)
    compact = compact.strip(" .,-—–")
    if not compact:
        return False
    if _DIGITS_ONLY_RE.match(compact):
        return True
    if _ORDINAL_SUFFIX_RE.match(compact.replace(" ", "")):
        return True
    # одно-два слова из словаря числительных
    parts = re.split(r"[\s\-]+", compact)
    if 1 <= len(parts) <= 2 and all(p in _NUMBER_WORDS for p in parts):
        return True
    # «номер 1» / «num 5» без другого смысла
    if re.fullmatch(r"(?:номер|num|n|no|№)\s*\d{1,6}", compact, flags=re.I):
        return True
    return False


def is_emoji_only_spam(text: str) -> bool:
    """Только эмодзи/знаки без букв и цифр: ☠☠☠, 🥺🥺🥺, !!!"""
    raw = text or ""
    if not raw.strip():
        return False
    # убрать whitespace
    compact = re.sub(r"\s+", "", raw)
    if len(compact) < 1:
        return False
    letters_digits = re.sub(
        r"[^\w]",
        "",
        compact,
        flags=re.UNICODE,
    )
    # \w включает цифры и буквы; если ничего не осталось — только эмодзи/пунктуация
    if letters_digits:
        return False
    # один-два символа пунктуации типа «?» можно оставить; стены — нет
    if len(compact) >= 2 or (len(compact) == 1 and compact not in ".!?"):
        return True
    return False


def is_char_wall_spam(text: str) -> bool:
    """Длинный повтор одного символа или чередование 2 эмодзи-стены."""
    n = normalize_text(text).replace(" ", "")
    if len(n) < 8:
        return False
    if _CHAR_REPEAT_RE.search(n):
        return True
    # чередование двух символов на длинной строке: абababab / 💙❤💙❤
    if len(n) >= 16:
        a, b = n[0], n[1] if len(n) > 1 else ""
        if a and b and a != b and all(n[i] == (a if i % 2 == 0 else b) for i in range(len(n))):
            return True
    return False


def has_promo_spam(text: str) -> bool:
    return bool(_PROMO_RE.search(text or ""))


def is_keyboard_mash(text: str) -> bool:
    """Короткий набор без смысла: еипи, ипипип, п п п."""
    norm = normalize_text(text)
    if not norm or norm in _SHORT_OK:
        return False
    letters = re.findall(r"[a-zа-яё]", norm, flags=re.IGNORECASE)
    compact = "".join(letters).lower()
    if len(compact) < 2 or len(compact) > 24:
        return False
    if compact in _SHORT_OK:
        return False
    uniq = len(set(compact))
    if uniq == 1 and len(compact) >= 2:
        return True
    if uniq <= 2 and len(compact) >= 3:
        return True
    if uniq <= 3 and len(compact) >= 4:
        return True
    if len(compact) >= 6 and uniq / len(compact) <= 0.4:
        return True
    return False


def is_break_length_spam(text: str) -> tuple[bool, str]:
    raw = text or ""
    if len(raw) > max_len():
        return True, f"too_long:{len(raw)}"
    cleaned = _INVISIBLE_RE.sub("", raw)
    stripped = cleaned.strip()
    if len(raw) >= 40 and not stripped:
        return True, "empty_long"
    if len(cleaned) >= 40:
        non_ws = re.sub(r"\s+", "", cleaned, flags=re.UNICODE)
        if non_ws and (1.0 - len(non_ws) / max(1, len(cleaned))) >= blank_ratio_limit():
            return True, "blank_ratio"
        # почти только невидимые/пробелы
        if len(stripped) <= 2 and len(raw) >= 40:
            return True, "invisible_spam"
    return False, ""


def edit_distance(a: str, b: str, cap: int = 4) -> int:
    """Расстояние Левенштейна с отсечением, если уже больше cap."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > cap:
        return cap + 1
    if la < lb:
        a, b = b, a
        la, lb = lb, la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        row_min = i
        for j, cb in enumerate(b, start=1):
            ins = cur[j - 1] + 1
            delete = prev[j] + 1
            sub = prev[j - 1] + (0 if ca == cb else 1)
            val = min(ins, delete, sub)
            cur.append(val)
            if val < row_min:
                row_min = val
        if row_min > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def parse_api_comment(msg: dict[str, Any], post_mid: str, chat_id: int | None = None) -> ParsedComment | None:
    """Разбор объекта комментария из GET /messages/{mid}/comments."""
    if not isinstance(msg, dict):
        return None
    body = msg.get("body") if isinstance(msg.get("body"), dict) else {}
    sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
    text = str(body.get("text") or "")
    cid = str(body.get("mid") or msg.get("mid") or "").strip()
    try:
        uid = int(sender.get("user_id") or sender.get("id") or 0)
    except (TypeError, ValueError):
        uid = 0
    recipient = msg.get("recipient") if isinstance(msg.get("recipient"), dict) else {}
    pmid = str(recipient.get("post_id") or post_mid or "").strip()
    rid = chat_id
    if recipient.get("chat_id") is not None:
        try:
            rid = int(recipient["chat_id"])
        except (TypeError, ValueError):
            pass
    try:
        ts = int(msg.get("timestamp") or 0)
    except (TypeError, ValueError):
        ts = 0
    if not cid or not pmid or uid <= 0:
        return None
    return ParsedComment(
        post_mid=pmid,
        comment_mid=cid,
        chat_id=rid,
        user_id=uid,
        text=text,
        is_bot=bool(sender.get("is_bot")),
        timestamp_ms=ts,
        is_admin=bool(sender.get("is_admin")),
        is_owner=bool(sender.get("is_owner")),
    )


def parse_comment_update(update: dict[str, Any]) -> ParsedComment | None:
    """Достаёт post_mid / comment_mid / text / sender из Update comment_*."""
    msg = update.get("message")
    if not isinstance(msg, dict):
        msg = update.get("comment") if isinstance(update.get("comment"), dict) else {}
    body = msg.get("body") if isinstance(msg.get("body"), dict) else {}
    if not body and isinstance(update.get("body"), dict):
        body = update["body"]

    text = str(body.get("text") or msg.get("text") or update.get("text") or "")
    comment_mid = str(
        body.get("mid")
        or body.get("comment_id")
        or msg.get("mid")
        or update.get("comment_id")
        or ""
    ).strip()

    recipient = msg.get("recipient") if isinstance(msg.get("recipient"), dict) else {}
    post_mid = str(
        recipient.get("post_id")
        or body.get("post_id")
        or update.get("post_id")
        or ""
    ).strip()

    sender = msg.get("sender") if isinstance(msg.get("sender"), dict) else {}
    if not sender and isinstance(update.get("user"), dict):
        sender = update["user"]
    try:
        user_id = int(sender.get("user_id") or sender.get("id") or 0)
    except (TypeError, ValueError):
        user_id = 0

    chat_id = None
    for raw in (recipient.get("chat_id"), update.get("chat_id"), msg.get("chat_id")):
        if raw is None:
            continue
        try:
            chat_id = int(raw)
            break
        except (TypeError, ValueError):
            continue

    try:
        ts = int(msg.get("timestamp") or update.get("timestamp") or 0)
    except (TypeError, ValueError):
        ts = 0

    if not comment_mid or not post_mid or user_id <= 0:
        return None
    return ParsedComment(
        post_mid=post_mid,
        comment_mid=comment_mid,
        chat_id=chat_id,
        user_id=user_id,
        text=text,
        is_bot=bool(sender.get("is_bot")),
        timestamp_ms=ts,
        is_admin=bool(sender.get("is_admin")),
        is_owner=bool(sender.get("is_owner")),
    )


class CommentFloodTracker:
    """Скользящее окно одинаковых комментариев user+text."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[int, str], deque[tuple[float, str, str]]] = defaultdict(
            deque
        )

    def _prune(self, key: tuple[int, str], now: float, window: float) -> None:
        q = self._buckets[key]
        while q and now - q[0][0] > window:
            q.popleft()
        if not q and key in self._buckets:
            del self._buckets[key]

    def add(
        self,
        *,
        user_id: int,
        text_norm: str,
        comment_mid: str,
        post_mid: str,
        now: float | None = None,
        window: float | None = None,
        limit: int | None = None,
    ) -> tuple[int, list[tuple[str, str]]]:
        """
        Добавляет событие. Если после добавления count > limit —
        возвращает все (post_mid, comment_mid) в окне для удаления.
        """
        now = time.time() if now is None else now
        window = dup_window_sec() if window is None else window
        limit = dup_limit() if limit is None else limit
        key = (int(user_id), text_norm)
        self._prune(key, now, window)
        self._buckets[key].append((now, post_mid, comment_mid))
        q = self._buckets[key]
        count = len(q)
        if count >= limit:
            return count, [(p, c) for _, p, c in list(q)]
        return count, []


class UserBurstTracker:
    """Пачка коротких комментов одного человека под одним постом."""

    def __init__(self) -> None:
        self._buckets: dict[tuple[int, str], deque[tuple[float, str]]] = defaultdict(deque)

    def add(
        self,
        *,
        user_id: int,
        post_mid: str,
        comment_mid: str,
        now: float | None = None,
        window: float | None = None,
        limit: int | None = None,
    ) -> tuple[int, list[str]]:
        now = time.time() if now is None else now
        window = burst_window_sec() if window is None else window
        limit = burst_limit() if limit is None else limit
        key = (int(user_id), (post_mid or "").strip())
        if not key[1]:
            return 0, []
        q = self._buckets[key]
        while q and now - q[0][0] > window:
            q.popleft()
        q.append((now, comment_mid))
        if len(q) >= limit:
            return len(q), [cid for _ts, cid in list(q)]
        return len(q), []


class NearDupTracker:
    """Похожие комментарии к одному посту: >N за минуту, расстояние ≤ K символов."""

    def __init__(self) -> None:
        self._by_post: dict[str, deque[tuple[float, str, str, int]]] = defaultdict(deque)

    def add(
        self,
        *,
        post_mid: str,
        comment_mid: str,
        text_norm: str,
        user_id: int = 0,
        now: float | None = None,
        window: float | None = None,
        limit: int | None = None,
        dist: int | None = None,
        min_len: int | None = None,
    ) -> tuple[int, list[str]]:
        now = time.time() if now is None else now
        window = dup_window_sec() if window is None else window
        limit = near_limit() if limit is None else limit
        dist = near_dist() if dist is None else dist
        min_len = near_min_len() if min_len is None else min_len
        pmid = (post_mid or "").strip()
        if not pmid:
            return 0, []
        q = self._by_post[pmid]
        while q and now - q[0][0] > window:
            q.popleft()
        q.append((now, text_norm, comment_mid, int(user_id)))
        if len(text_norm) < min_len:
            return 1, []
        cluster = [
            cid
            for ts, txt, cid, _uid in q
            if len(txt) >= min_len and edit_distance(text_norm, txt, cap=dist) <= dist
        ]
        if len(cluster) >= limit:
            return len(cluster), cluster
        return len(cluster), []


_FLOOD = CommentFloodTracker()
_NEAR = NearDupTracker()
_BURST = UserBurstTracker()


def evaluate_comment(
    parsed: ParsedComment,
    *,
    bot_user_id: int = 0,
    tracker: CommentFloodTracker | None = None,
    near_tracker: NearDupTracker | None = None,
    burst_tracker: UserBurstTracker | None = None,
    now: float | None = None,
) -> GuardDecision:
    if bot_user_id and parsed.user_id == int(bot_user_id):
        return GuardDecision("ok")
    if parsed.is_admin or parsed.is_owner or parsed.user_id in privileged_user_ids():
        return GuardDecision("ok")
    # чужие боты тоже режем по ссылкам/длине/флуду

    if has_profanity(parsed.text):
        return GuardDecision(
            "delete",
            reason="profanity",
            delete_mids=(parsed.comment_mid,),
        )

    if is_delete_complaint(parsed.text):
        return GuardDecision(
            "delete",
            reason="delete_whine",
            delete_mids=(parsed.comment_mid,),
        )

    if has_spam_link(parsed.text):
        return GuardDecision(
            "delete",
            reason="link",
            delete_mids=(parsed.comment_mid,),
        )

    if is_number_only_spam(parsed.text):
        return GuardDecision(
            "delete",
            reason="number_spam",
            delete_mids=(parsed.comment_mid,),
        )

    if is_emoji_only_spam(parsed.text):
        return GuardDecision(
            "delete",
            reason="emoji_only",
            delete_mids=(parsed.comment_mid,),
        )

    if is_char_wall_spam(parsed.text):
        return GuardDecision(
            "delete",
            reason="char_wall",
            delete_mids=(parsed.comment_mid,),
        )

    if has_promo_spam(parsed.text):
        return GuardDecision(
            "delete",
            reason="promo",
            delete_mids=(parsed.comment_mid,),
        )

    if is_keyboard_mash(parsed.text):
        return GuardDecision(
            "delete",
            reason="keyboard_mash",
            delete_mids=(parsed.comment_mid,),
        )

    bad, why = is_break_length_spam(parsed.text)
    if bad:
        return GuardDecision(
            "delete",
            reason=why,
            delete_mids=(parsed.comment_mid,),
        )

    norm = normalize_text(parsed.text)
    if not norm:
        # пустой после нормализации, но короткий — ок; длинный уже поймали выше
        return GuardDecision("ok")

    tr = tracker or _FLOOD
    event_now = now
    if event_now is None and parsed.timestamp_ms > 1_000_000_000_000:
        event_now = parsed.timestamp_ms / 1000.0
    count, flood = tr.add(
        user_id=parsed.user_id,
        text_norm=norm,
        comment_mid=parsed.comment_mid,
        post_mid=parsed.post_mid,
        now=event_now,
    )
    if flood:
        mids = tuple(c for p, c in flood if p == parsed.post_mid) or (parsed.comment_mid,)
        return GuardDecision(
            "delete",
            reason=f"dup:{count}/{dup_window_sec():.0f}s",
            delete_mids=mids,
        )

    burst = burst_tracker or _BURST
    bcount, burst_mids = burst.add(
        user_id=parsed.user_id,
        post_mid=parsed.post_mid,
        comment_mid=parsed.comment_mid,
        now=event_now,
    )
    if burst_mids:
        return GuardDecision(
            "delete",
            reason=f"burst:{bcount}/{burst_window_sec():.0f}s",
            delete_mids=tuple(burst_mids),
        )

    ntr = near_tracker or _NEAR
    ncount, cluster = ntr.add(
        post_mid=parsed.post_mid,
        comment_mid=parsed.comment_mid,
        text_norm=norm,
        user_id=parsed.user_id,
        now=event_now,
    )
    if cluster:
        mids = tuple(cluster)
        return GuardDecision(
            "delete",
            reason=f"near:{ncount}/{dup_window_sec():.0f}s≤{near_dist()}",
            delete_mids=mids,
        )
    return GuardDecision("ok")
