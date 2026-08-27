"""Редполитика отбора в прод — из разметки pool_10d (500 true / 2739 false)."""

from __future__ import annotations

# Доля human-factor среди недавно опубликованных take=true.
HUMAN_FACTOR_CAP = 0.40
HUMAN_FACTOR_WINDOW = 10

PICK_TAGS = (
    "top_name",
    "transfer_money",
    "addition",
    "human_factor",
    "match_narrative",
    "rpl_exception",
    "bright_quote",
    "sensation",
    "reject",
)

POLICY_RULES = (
    "no-dupes(event vs addition vs repeat); "
    "match_result SOFT-BANNED unless narrative(achievement/sensation/drama/individual/trophy); "
    "RPL only referee-incident/RU->Europe/top-match-with-narrative/cup-SF+; "
    "reactions=false unless bright quote OR event verb (подписал/перешёл/€); "
    "rumors=false; friendlies without narrative=false; "
    "human-factor kept and capped<=40% of TRUE; "
    "service/schedules/broadcasts dropped; "
    "non-RPL kept for big names/clubs/money/prospects/sensations"
)

PICK_SYSTEM = """Ты старший редактор русскоязычного футбольного канала «ВСЕ НА ФУТБОЛ».
Новость уже прошла дешёвый rule-filter. Подтверди или отклони. Не выдумывай факты. Отвечай СТРОГО JSON.

БРАТЬ (take=true), только если есть инфоповод:
- top_name — топ-клуб / большое имя / топ-турнир
- transfer_money — трансфер или сумма топ-клуба / большого имени
- addition — дополнение к событию: новый факт, не пересказ
- human_factor — лайфстайл/юмор звезды, не рутина
- match_narrative — достижение/сенсация/драма/подвиг/трофей. НЕ голый счёт
- rpl_exception — РПЛ ТОЛЬКО: судейство у гранда, РФ-игрок в Европу, топ-матч двух грандов С нарративом, Кубок России с 1/2
- bright_quote — яркая цитата, не дежурный «тренер отреагировал»
- sensation — сенсация даже ноунейм-клуба

НЕ БРАТЬ:
- вторичная реакция/мнение без глагола события (главный мусор)
- повтор без нового факта
- голый счёт, трансляции, расписание, превью, видеообзор, gossip
- товарищеский матч без нарратива
- РПЛ вне четырёх исключений
- слухи; новость без топ-инфоповода

Если cluster_already_published=true: только addition, иначе reject.
Если human_factor_share ≥ 0.40: human_factor → reject.
"""

PICK_FEWSHOT = [
    {
        "title": "Появилось видео со свадебной церемонии Роналду и Джорджины",
        "take": True,
        "tag": "human_factor",
        "reason": "человеческий фактор/юмор",
    },
    {
        "title": "«Галатасарай» предложил «Локомотиву» € 28 млн за Батракова, с игроком согласован контракт",
        "take": True,
        "tag": "rpl_exception",
        "reason": "РПЛ-исключение: переход РФ игрока в Европу",
    },
    {
        "title": "Калафьори забил самый быстрый гол в Суперкубке Англии за 58 лет – на 23-й секунде",
        "take": True,
        "tag": "match_narrative",
        "reason": "нарратив матча (достижение/сенсация/драма)",
    },
    {
        "title": "«Барса» купила Родри у «Ман Сити» за 76,5 млн евро с учетом бонусов. Контракт – до 2030-го",
        "take": True,
        "tag": "transfer_money",
        "reason": "трансфер/деньги топ-клуба или имени",
    },
    {
        "title": "Месси не реализовал три пенальти подряд — впервые с 2014 года",
        "take": True,
        "tag": "match_narrative",
        "reason": "нарратив матча (достижение/сенсация/драма)",
    },
    {
        "title": "Черчесов о 2:1 с «Факелом»: «Ахмат» играл так, как хотел",
        "take": False,
        "tag": "reject",
        "reason": "РПЛ: вне критериев",
    },
    {
        "title": "Тренер «Локомотива» Галактионов отреагировал на поражение от «Ростова»",
        "take": False,
        "tag": "reject",
        "reason": "вторичная реакция/комментарий",
    },
    {
        "title": "Фенербахче — Лион: смотреть онлайн прямую трансляцию матча, 18 августа 2026",
        "take": False,
        "tag": "reject",
        "reason": "голый счёт/трансляция → контур результатов",
    },
    {
        "title": "Лига чемпионов — 2026/2027: результаты на 18 августа, календарь, таблица",
        "take": False,
        "tag": "reject",
        "reason": "служебная сводка/расписание/превью",
    },
    {
        "title": "«Ницца» подписала Витцеля как свободного агента по схеме «1+1»",
        "take": False,
        "tag": "reject",
        "reason": "без топ-инфоповода",
    },
    {
        "title": "Товарищеские матчи. «Ливерпуль» победил «Комо» после 0:0",
        "take": False,
        "tag": "reject",
        "reason": "товарищеский/контрольный матч",
    },
]
