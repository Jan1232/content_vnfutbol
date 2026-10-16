from __future__ import annotations

import re
from typing import Any

import httpx

from app.config import get_settings

SYSTEM_CA = "/etc/ssl/certs/ca-certificates.crt"

# Устойчивые выражения / сленг — НЕ переводить
KEEP_AS_IS = [
    "GOAT",
    "Ronnie",
    "CR7",
    "SIUU",
    "SIUUU",
    "MVP",
    "MOTM",
    "UCL",
    "FIFA",
    "UEFA",
    "Hat-trick",
    "Hattrick",
    "El Clasico",
    "El Clásico",
    "Sporting CP",
    "Al-Nassr",
    "Primeira Liga",
]

SYSTEM_PROMPT = """Ты старший редактор русскоязычного фанатского футбольного канала.
Переводишь посты EN→RU так, чтобы звучало как живой русский Telegram, а не как Google Translate.

КОМПАНОВКА (обязательно):
- Сохрани ту же сетку строк и ПУСТЫЕ строки («воздух»).
- Эмодзи на тех же местах/строках, что в оригинале.
- Списки • не ломай.
- Заголовки пиши обычной кириллицей (А-Я), НЕ латиницей-транслитом
  и НЕ Mathematical Bold (𝐃𝐕𝐀 и т.п.). «TWO NEW…» → «ДВА НОВЫХ…».

КАЧЕСТВО ЯЗЫКА:
- Переводи смысл и тон, не дословную кальку.
- Короткие посты оставляй короткими.
- Сарказм/гиперболы передавай по-русски естественно
  («2022 was a disease» → «2022 — это был кошмар» / «2022 выдался отвратительным», НЕ «2022 был болезнью»).
- LEGENDARY в конце → «это легенда» / «просто легенда», не канцелярское «ЛЕГЕНДАРНАЯ».

НЕ ПЕРЕВОДИТЬ (оставить как в оригинале, с тем же регистром):
GOAT, Ronnie, CR7, SIUU/SIUUU, MVP, MOTM, UCL, FIFA, UEFA, Sporting CP, Al-Nassr.
Если весь пост — только «GOAT.» / «Ronnie 😎» — оставь почти как есть (GOAT. / Ronnie 😎), не разворачивай в «Криштиану Роналду» и не в «лучший из всех времён».

ИМЕНА (если нужно переводить полное имя):
Cristiano Ronaldo → Криштиану Роналду
José Mourinho → Жозе Моуринью
Georgina Rodríguez → Джорджина Родригес
Jorge Jesus → Жорже Жезуш
Álvaro Arbeloa → Альваро Арбелоа
Ronaldo Nazário → Роналдо Назарио

КЛУБЫ / ТЕРМИНЫ:
Sporting CP / Sporting → Sporting CP или «Спортинг».
Ballon d'Or / Ballon d’Or → всегда «Золотой мяч»
  (Ballon d'Or winners → обладатели «Золотого мяча», не «Ballon d'Or победителями»).

СЛОВАРЬ КЛУБОВ И ПРОЗВИЩ (используй русское название; не транслитерируй
аббревиатуру по буквам). Неоднозначные City, United, Real, Sporting,
Racing, Palace, Forest, Inter определяй только по контексту.

АПЛ / АНГЛИЯ:
Arsenal / AFC / Gunners → «Арсенал» / «канониры»;
Aston Villa / Villa / AVFC → «Астон Вилла»;
Bournemouth / Cherries → «Борнмут»;
Brentford / Bees → «Брентфорд»;
Brighton / BHAFC / Seagulls → «Брайтон»;
Burnley / Clarets → «Бернли»;
Chelsea / CFC / Blues → «Челси» / «синие»;
Crystal Palace / CPFC / Eagles → «Кристал Пэлас»;
Everton / EFC / Toffees → «Эвертон»;
Fulham / Cottagers → «Фулхэм»;
Leeds / LUFC / Whites → «Лидс»;
Leicester / LCFC / Foxes → «Лестер»;
Liverpool / LFC / Reds → «Ливерпуль» / «красные»;
Man City / Manchester City / MCFC → «Манчестер Сити»;
Man Utd / Man United / Manchester United / MUFC → «Манчестер Юнайтед»;
Newcastle / NUFC / Toon / Magpies → «Ньюкасл»;
Nott'm Forest / Nottingham Forest / NFFC → «Ноттингем Форест»;
Southampton / Saints → «Саутгемптон»;
Sunderland / SAFC / Black Cats → «Сандерленд»;
Tottenham / Spurs / THFC → «Тоттенхэм»;
West Ham / WHUFC / Hammers → «Вест Хэм»;
Wolves / Wolverhampton / WWFC → «Вулверхэмптон» / «Вулвз»;
Cov / Coventry / Coventry City → «Ковентри» (НЕ «Ков»);
Ipswich / ITFC / Tractor Boys → «Ипсвич»;
Sheffield United / Sheff Utd / Blades → «Шеффилд Юнайтед»;
Sheffield Wednesday / Sheff Wed / Owls → «Шеффилд Уэнсдей»;
West Brom / WBA / Baggies → «Вест Бромвич»;
QPR / Queens Park Rangers → «Куинз Парк Рейнджерс».

ЛА ЛИГА / ИСПАНИЯ:
Athletic Club / Athletic Bilbao / Los Leones → «Атлетик Бильбао»;
Atleti / Atlético / Atletico Madrid / Colchoneros → «Атлетико Мадрид»;
Barcelona / Barça / Barca / FCB / Blaugrana / Culés → «Барселона»;
Real Madrid / RMA / Los Blancos / Merengues → «Реал Мадрид»;
Real Sociedad / La Real → «Реал Сосьедад»;
Real Betis / Betis / Verdiblancos → «Бетис»;
Sevilla / SFC → «Севилья»;
Valencia / VCF / Los Che → «Валенсия»;
Villarreal / Yellow Submarine → «Вильярреал»;
Girona → «Жирона»; Getafe → «Хетафе»; Celta Vigo / Celta → «Сельта»;
Osasuna → «Осасуна»; Mallorca → «Мальорка»;
Rayo Vallecano / Rayo → «Райо Вальекано»;
Espanyol / RCD Espanyol / Pericos → «Эспаньол»;
Alavés / Alaves → «Алавес»; Elche → «Эльче»;
Levante → «Леванте»; Oviedo / Real Oviedo → «Реал Овьедо»;
Las Palmas → «Лас-Пальмас»; Leganés / Leganes → «Леганес»;
Valladolid → «Вальядолид»; Cádiz / Cadiz → «Кадис»;
Granada → «Гранада»; Deportivo La Coruña / Depor → «Депортиво».

БУНДЕСЛИГА / ГЕРМАНИЯ:
Bayern / Bayern Munich / FC Bayern / FCB → «Бавария»;
Dortmund / Borussia Dortmund / BVB / BVB09 → «Боруссия Дортмунд»;
Leverkusen / Bayer Leverkusen / Bayer 04 / Werkself → «Байер»;
RB Leipzig / RBL → «РБ Лейпциг»;
Eintracht Frankfurt / SGE → «Айнтрахт»;
Stuttgart / VfB → «Штутгарт»;
Borussia Mönchengladbach / Gladbach / BMG / Foals → «Боруссия Мёнхенгладбах»;
Wolfsburg / VfL Wolfsburg → «Вольфсбург»;
Hoffenheim / TSG → «Хоффенхайм»;
Freiburg / SCF → «Фрайбург»;
Mainz / Mainz 05 → «Майнц»;
Werder / Werder Bremen → «Вердер»;
Union Berlin / Eisern Union → «Унион Берлин»;
St. Pauli / Sankt Pauli → «Санкт-Паули»;
Augsburg / FCA → «Аугсбург»;
Heidenheim / FCH → «Хайденхайм»;
Hamburg / Hamburger SV / HSV → «Гамбург»;
Köln / Cologne / FC Köln → «Кёльн»;
Schalke / S04 / Royal Blues → «Шальке»;
Hertha / Hertha Berlin / BSC → «Герта»;
Bochum / VfL Bochum → «Бохум»;
Holstein Kiel → «Хольштайн»; Darmstadt → «Дармштадт»;
Hannover 96 → «Ганновер»; Nürnberg / Nuremberg → «Нюрнберг».

СЕРИЯ A / ИТАЛИЯ:
Juventus / Juve / Bianconeri / Old Lady → «Ювентус»;
Inter / Inter Milan / Internazionale / Nerazzurri → «Интер»;
AC Milan / Milan / Rossoneri → «Милан»;
Napoli / SSC Napoli / Partenopei → «Наполи»;
Roma / AS Roma / Giallorossi → «Рома»;
Lazio / Biancocelesti → «Лацио»;
Atalanta / La Dea → «Аталанта»;
Fiorentina / Viola → «Фиорентина»;
Bologna / Rossoblù → «Болонья»;
Torino / Toro → «Торино»;
Genoa / Grifone → «Дженоа»;
Sampdoria / Samp → «Сампдория»;
Udinese → «Удинезе»; Parma → «Парма»; Como → «Комо»;
Monza → «Монца»; Lecce → «Лечче»; Empoli → «Эмполи»;
Cagliari → «Кальяри»; Verona / Hellas Verona → «Верона»;
Venezia → «Венеция»; Cremonese → «Кремонезе»;
Sassuolo → «Сассуоло»; Pisa → «Пиза»;
Spezia → «Специя»; Salernitana → «Салернитана»;
Palermo → «Палермо»; Bari → «Бари».

ЛИГА 1 / ФРАНЦИЯ:
PSG / Paris Saint-Germain / Paris SG / Parisians → «ПСЖ»;
Marseille / Olympique Marseille / OM → «Марсель»;
Lyon / Olympique Lyonnais / OL → «Лион»;
Monaco / AS Monaco → «Монако»;
Lille / LOSC → «Лилль»;
Nice / OGC Nice → «Ницца»;
Lens / RC Lens / Sang et Or → «Ланс»;
Rennes / Stade Rennais → «Ренн»;
Strasbourg / Racing Strasbourg / RCSA → «Страсбур»;
Toulouse / TFC → «Тулуза»;
Nantes / Canaries → «Нант»;
Auxerre / AJA → «Осер»;
Brest / Stade Brestois → «Брест»;
Montpellier / MHSC → «Монпелье»;
Reims / Stade de Reims → «Реймс»;
Le Havre / HAC → «Гавр»;
Lorient / Merlus → «Лорьян»;
Metz / FC Metz → «Мец»;
Saint-Étienne / Saint-Etienne / ASSE → «Сент-Этьен»;
Angers / SCO → «Анже»; Paris FC → «Париж»;
Clermont / Clermont Foot → «Клермон»;
Troyes / ESTAC → «Труа»; Bordeaux / Girondins → «Бордо».

РПЛ / РОССИЯ:
Zenit / FC Zenit → «Зенит»;
Spartak / Spartak Moscow / FCSM → «Спартак»;
CSKA / CSKA Moscow / PFC CSKA → «ЦСКА»;
Dynamo Moscow / Dinamo Moscow / FCDM → «Динамо»;
Lokomotiv Moscow / Loko / FCLM → «Локомотив»;
Krasnodar / Bulls → «Краснодар»;
Rostov / FC Rostov → «Ростов»;
Rubin / Rubin Kazan → «Рубин»;
Krylia Sovetov / Krylya Sovetov → «Крылья Советов»;
Akhmat / Akhmat Grozny → «Ахмат»;
Sochi / PFC Sochi → «Сочи»;
Orenburg → «Оренбург»; Ural → «Урал»;
Baltika / Baltika Kaliningrad → «Балтика»;
Akron / Akron Tolyatti → «Акрон»;
Dynamo Makhachkala / Dinamo Makhachkala → «Динамо Махачкала»;
Pari NN / Pari Nizhny Novgorod / Nizhny Novgorod → «Пари НН»;
Khimki → «Химки»; Fakel / Fakel Voronezh → «Факел»;
Arsenal Tula → «Арсенал Тула»;
Torpedo Moscow → «Торпедо»;
Alania / Alania Vladikavkaz → «Алания».

MLS / США И КАНАДА:
Inter Miami / Inter Miami CF / Herons → «Интер Майами»;
Atlanta United / Five Stripes → «Атланта Юнайтед»;
Austin FC → «Остин»; Charlotte FC → «Шарлотт»;
Chicago Fire → «Чикаго Файр»;
FC Cincinnati → «Цинциннати»;
Colorado Rapids → «Колорадо Рэпидз»;
Columbus Crew / Crew → «Коламбус Крю»;
DC United / D.C. United → «Ди Си Юнайтед»;
FC Dallas → «Даллас»;
Houston Dynamo → «Хьюстон Динамо»;
LA Galaxy / Galaxy → «Лос-Анджелес Гэлакси»;
LAFC / Los Angeles FC → «Лос-Анджелес»;
Minnesota United / Loons → «Миннесота Юнайтед»;
CF Montréal / CF Montreal → «Монреаль»;
Nashville SC → «Нэшвилл»;
New England Revolution / Revs → «Нью-Инглэнд Революшн»;
New York City FC / NYCFC → «Нью-Йорк Сити»;
New York Red Bulls / NYRB → «Нью-Йорк Ред Буллз»;
Orlando City / Lions → «Орландо Сити»;
Philadelphia Union → «Филадельфия Юнион»;
Portland Timbers / Timbers → «Портленд Тимберс»;
Real Salt Lake / RSL → «Реал Солт-Лейк»;
San Diego FC / SDFC → «Сан-Диего»;
San Jose Earthquakes / Quakes → «Сан-Хосе Эртквейкс»;
Seattle Sounders / Sounders → «Сиэтл Саундерс»;
Sporting Kansas City / Sporting KC / SKC → «Спортинг Канзас-Сити»;
St. Louis City / St Louis City / STL City → «Сент-Луис Сити»;
Toronto FC / TFC → «Торонто»;
Vancouver Whitecaps / Whitecaps → «Ванкувер Уайткэпс».

ЗАПРЕЩЕНО:
- Китайские/японские/корейские иероглифы и любой CJK-текст.
- Пояснения в скобках вроде «(лучший)» после GOAT.
- Слова Instagram/инстаграм → пиши IG.
- Добавлять «перевод:» и отсебятину.

Выдай ТОЛЬКО текст поста."""

FEW_SHOT = [
    {
        "role": "user",
        "content": "Переведи пост.\n\nОригинал:\nGOAT.",
    },
    {"role": "assistant", "content": "GOAT."},
    {
        "role": "user",
        "content": "Переведи пост.\n\nОригинал:\nRonnie 😎",
    },
    {"role": "assistant", "content": "Ronnie 😎"},
    {
        "role": "user",
        "content": (
            "Переведи пост.\n\nОригинал:\n"
            "Why was Cristiano Ronaldo looking so young at this tournament? 2022 was a disease.😖"
        ),
    },
    {
        "role": "assistant",
        "content": "Почему Криштиану Роналду выглядит таким молодым на этом турнире? 2022 выдался просто кошмаром.😖",
    },
    {
        "role": "user",
        "content": (
            "Переведи пост.\n\nОригинал:\n"
            "This picture of 33 year old Cristiano Ronaldo in Madeira with his 15 individual trophies is LEGENDARY. 👑"
        ),
    },
    {
        "role": "assistant",
        "content": "Фото 33-летнего Криштиану Роналду на Мадейре с 15 личными трофеями — просто легенда. 👑",
    },
    {
        "role": "user",
        "content": (
            "Переведи пост.\n\nОригинал:\n"
            "José Mourinho with Cristiano Ronaldo's Sporting CP shirt.🥶"
        ),
    },
    {
        "role": "assistant",
        "content": "Жозе Моуринью с футболкой Криштиану Роналду из Sporting CP.🥶",
    },
    {
        "role": "user",
        "content": "Переведи пост.\n\nОригинал:\nAmenda is Cov. 🩵",
    },
    {
        "role": "assistant",
        "content": "Аменда — это Ковентри. 🩵",
    },
]


_EMOJI_CHAR = (
    r"(?:[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U0001F1E0-\U0001F1FF]"
    r"|[\u2600-\u26FF\u2700-\u27BF]|‼|⁉|™|©|®)"
)
_EMOJI_ONLY_RE = re.compile(rf"^(?:\s|{_EMOJI_CHAR}|[️‍])+?$", re.UNICODE)
_CJK_RE = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u30FF\uAC00-\uD7AF]+")

# Mathematical Bold/Italic/… латиница и цифры → обычный ASCII.
# Иначе модель «переводит» 𝐓𝐖𝐎 → 𝐃𝐕𝐀 (транслит в том же стиле), а не «ДВА».
_MATH_LATIN_RANGES = (
    (0x1D400, 0x1D419, ord("A")),  # bold A-Z
    (0x1D41A, 0x1D433, ord("a")),  # bold a-z
    (0x1D434, 0x1D44D, ord("A")),  # italic A-Z
    (0x1D44E, 0x1D467, ord("a")),  # italic a-z
    (0x1D468, 0x1D481, ord("A")),  # bold italic A-Z
    (0x1D482, 0x1D49B, ord("a")),  # bold italic a-z
    (0x1D4D0, 0x1D4E9, ord("A")),  # bold script A-Z
    (0x1D4EA, 0x1D503, ord("a")),  # bold script a-z
    (0x1D5D4, 0x1D5ED, ord("A")),  # bold sans A-Z
    (0x1D5EE, 0x1D607, ord("a")),  # bold sans a-z
    (0x1D63C, 0x1D655, ord("A")),  # bold italic sans A-Z
    (0x1D656, 0x1D66F, ord("a")),  # bold italic sans a-z
    (0x1D56C, 0x1D585, ord("A")),  # bold fraktur A-Z
    (0x1D586, 0x1D59F, ord("a")),  # bold fraktur a-z
)
_MATH_DIGIT_RANGES = (
    (0x1D7CE, 0x1D7D7, ord("0")),  # bold digits
    (0x1D7E2, 0x1D7EB, ord("0")),  # sans bold digits
)


def normalize_styled_latin(text: str) -> str:
    """Сводит Mathematical Bold/Italic латиницу к обычным A-Z/a-z/0-9."""
    if not text:
        return text
    out: list[str] = []
    for ch in text:
        cp = ord(ch)
        mapped = None
        for start, end, base in _MATH_LATIN_RANGES:
            if start <= cp <= end:
                mapped = chr(base + (cp - start))
                break
        if mapped is None:
            for start, end, base in _MATH_DIGIT_RANGES:
                if start <= cp <= end:
                    mapped = chr(base + (cp - start))
                    break
        out.append(mapped if mapped is not None else ch)
    return "".join(out)


def strip_cjk(text: str) -> str:
    """Убрать иероглифы / CJK, если модель их вставила."""
    t = _CJK_RE.sub("", text)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t


def looks_russian(text: str) -> bool:
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text)
    if not letters:
        return True
    cyr = sum(1 for ch in letters if ("А" <= ch.upper() <= "Я") or ch in "Ёё")
    return (cyr / len(letters)) >= 0.7


def _is_emoji_only(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if re.search(r"[A-Za-zА-Яа-яЁё0-9]", s):
        return False
    return bool(_EMOJI_ONLY_RE.match(s)) or bool(re.fullmatch(rf"(?:{_EMOJI_CHAR}|[️‍\s])+?", s))


def glue_emoji_lines(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _is_emoji_only(line):
            em = line.strip()
            j = i + 1
            while j < len(lines) and lines[j].strip() == "":
                j += 1
            if j < len(lines) and not _is_emoji_only(lines[j]):
                out.append(f"{em} {lines[j].lstrip()}")
                i = j + 1
                continue
            if out:
                k = len(out) - 1
                while k >= 0 and out[k].strip() == "":
                    k -= 1
                if k >= 0 and not _is_emoji_only(out[k]):
                    out[k] = f"{out[k].rstrip()} {em}"
                    i += 1
                    continue
            out.append(line)
            i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _fix_ballon_dor(text: str) -> str:
    """Принудительно: Ballon d'Or → Золотой мяч (с нужным склонением по контексту)."""
    t = re.sub(
        r"(?iu)\bBallon\s+d['’]Or\s+winners?\b",
        "обладатели «Золотого мяча»",
        text,
    )
    # Ballon d'Or победителями/обладателями → обладателями «Золотого мяча»
    t = re.sub(
        r"(?iu)\bBallon\s+d['’]Or\s+(?:победител|обладател)(?:ями|ей|и|я|ю|ям|ях)?\b",
        "обладатели «Золотого мяча»",
        t,
    )
    t = re.sub(
        r"(?iu)\b(?:победител|обладател)(?:ями|ей|и|я|ю|ям|ях)?\s+Ballon\s+d['’]Or\b",
        "обладатели «Золотого мяча»",
        t,
    )
    # уже переведённые «победители Золотого мяча» → обладатели
    t = re.sub(
        r"(?iu)\bпобедител(ями|ей|и|я|ю|ям|ях)?\s+«Золотого мяча»",
        r"обладател\1 «Золотого мяча»",
        t,
    )
    t = re.sub(
        r"(?iu)\bпобедител(ями|ей|и|я|ю|ям|ях)?\s+Золотого мяча\b",
        r"обладател\1 «Золотого мяча»",
        t,
    )
    t = re.sub(r"(?iu)\bBallon\s+d['’]Or\b", "«Золотой мяч»", t)
    # поправить падеж после «с многими …»
    t = re.sub(
        r"(?iu)(с\s+многим(?:и)?\s+)обладатели «Золотого мяча»",
        r"\1обладателями «Золотого мяча»",
        t,
    )
    return t


def _protect_keep_terms(text: str) -> tuple[str, dict[str, str]]:
    """Временно заменить KEEP-термины плейсхолдерами, чтобы модель их не перевела."""
    mapping: dict[str, str] = {}
    out = text
    # длинные первыми
    for i, term in enumerate(sorted(KEEP_AS_IS, key=len, reverse=True)):
        token = f"⟦KEEP{i}⟧"

        def repl(m: re.Match[str], tok: str = token, original: str = term) -> str:
            mapping[tok] = m.group(0)  # сохраняем исходный регистр вхождения
            return tok

        out = re.sub(re.escape(term), repl, out, flags=re.IGNORECASE)
    return out, mapping


def _restore_keep_terms(text: str, mapping: dict[str, str]) -> str:
    out = text
    for tok, original in mapping.items():
        out = out.replace(tok, original)
    # страховка: если модель всё же развернула GOAT
    out = re.sub(
        r"(?iu)\bлучш(?:ий|его|ему|им|ие|их)?\s+из\s+всех\s+временно?в?\b",
        "GOAT",
        out,
    )
    out = re.sub(r"(?iu)\bвеличайший\s+всех\s+времён\b", "GOAT", out)
    return out


# Частые кривые транслитерации клубных прозвищ после модели
_CLUB_NICK_FIXES = [
    # Cov → Ков (нужно Ковентри)
    (re.compile(r"(?iu)(?<![а-яёa-z0-9])ков(?![а-яёa-z0-9])"), "Ковентри"),
]


def _fix_club_nicknames(text: str) -> str:
    t = text
    for rx, repl in _CLUB_NICK_FIXES:
        t = rx.sub(repl, t)
    return t


def normalize_post_text(text: str) -> str:
    if not text:
        return ""
    t = normalize_styled_latin(text.replace("\r\n", "\n").replace("\r", "\n").strip())
    t = t.replace("“", "«").replace("”", "»").replace("„", "«").replace("‟", "«")
    t = re.sub(r"^```(?:\w+)?\n?", "", t)
    t = re.sub(r"\n?```$", "", t)
    t = strip_cjk(t)
    t = _fix_ballon_dor(t)
    t = _fix_club_nicknames(t)

    def _pair_quotes_line(s: str) -> str:
        if s.count('"') < 2:
            return s
        out = []
        open_q = False
        for ch in s:
            if ch == '"':
                out.append("«" if not open_q else "»")
                open_q = not open_q
            else:
                out.append(ch)
        return "".join(out)

    t = "\n".join(_pair_quotes_line(line) for line in t.split("\n"))
    cleaned = []
    for line in t.split("\n"):
        if line.strip() in {'"', "«", "»", "''", '""'}:
            continue
        cleaned.append(line.rstrip())
    t = "\n".join(cleaned)
    t = glue_emoji_lines(t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"[ \t]+\n", "\n", t)
    return t.strip()


def _layout_hint(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    skeleton = []
    for line in lines:
        if not line.strip():
            skeleton.append("∅")
            continue
        sk = re.sub(r"[A-Za-zА-Яа-яЁё0-9'’`]+", "…", line)
        skeleton.append(sk if sk.strip() else "…")
    return "\n".join(skeleton)


def _is_keep_only_post(text: str) -> str | None:
    """Если пост целиком сленг/KEEP — вернуть канонический ответ без модели."""
    raw = text.strip()
    # GOAT. / GOAT / goat
    if re.fullmatch(r"(?i)\s*GOAT\.?\s*", raw):
        return "GOAT." if raw.rstrip().endswith(".") else "GOAT"
    # Ronnie + optional emoji
    m = re.fullmatch(r"(?i)\s*(Ronnie)(\s*[^\w]*)?\s*", raw)
    if m:
        rest = (m.group(2) or "").strip()
        return f"Ronnie {rest}".strip() if rest else "Ronnie"
    # CR7
    if re.fullmatch(r"(?i)\s*CR7\.?\s*", raw):
        return raw.strip()
    return None


def translate_to_russian(text: str) -> str:
    """Человеческий перевод EN→RU. Бэкенды: OpenClaw (ChatGPT) → OpenAI API → Groq."""
    text = normalize_styled_latin((text or "").strip())
    if not text:
        return text

    quick = _is_keep_only_post(text)
    if quick is not None:
        return normalize_post_text(quick)

    latin = len(re.findall(r"[A-Za-z]", text))
    if looks_russian(text) and latin < 3:
        return normalize_post_text(text)

    settings = get_settings()
    protected, mapping = _protect_keep_terms(text)
    hint = _layout_hint(text)
    user_msg = (
        "Переведи пост на русский.\n"
        "Плейсхолдеры вида ⟦KEEP#⟧ не трогай и не переводи — верни их как есть.\n"
        "Сохрани компановку (строки/пустые строки/эмодзи).\n"
        f"Каркас (∅ = пустая строка):\n{hint}\n\n"
        f"Оригинал:\n{protected[:3500]}"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *FEW_SHOT,
        {"role": "user", "content": user_msg},
    ]

    backend = (settings.translate_backend or "auto").strip().lower()
    errors: list[str] = []

    order: list[str]
    if backend == "openclaw":
        order = ["openclaw"]
    elif backend == "groq":
        order = ["groq"]
    elif backend == "openai":
        order = ["openai"]
    else:
        order = ["openclaw", "openai", "groq"]

    for name in order:
        try:
            if name == "openclaw":
                out = _chat_openclaw(settings, messages)
            elif name == "openai":
                out = _chat_openai(settings, messages)
            else:
                out = _chat_groq(settings, messages)
            out = _restore_keep_terms(out, mapping)
            result = normalize_post_text(out) or normalize_post_text(text)
            if result:
                print(f"[translate] via {name}", flush=True)
                return result
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"[translate] {name} fail: {e}", flush=True)
            continue

    raise RuntimeError("Перевод недоступен: " + " | ".join(errors)[:800])


def _chat_openclaw(settings: Any, messages: list[dict[str, Any]]) -> str:
    base = (settings.openclaw_base_url or "").rstrip("/")
    token = (settings.openclaw_api_key or "").strip()
    if not base or not token:
        raise RuntimeError("OPENCLAW_BASE_URL / OPENCLAW_API_KEY не заданы")
    model = settings.openclaw_model or "openclaw/default"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    backend_model = (settings.openclaw_backend_model or "").strip()
    if backend_model:
        headers["x-openclaw-model"] = backend_model
    payload = {
        "model": model,
        "temperature": 0.15,
        "max_tokens": 2500,
        "messages": messages,
        # стабильная сессия не нужна — каждый пост отдельно
        "user": f"translate:{abs(hash(messages[-1]['content'])) % 10_000_000}",
    }
    verify = SYSTEM_CA if __import__("pathlib").Path(SYSTEM_CA).exists() else True
    # OpenClaw на localhost — без прокси
    with httpx.Client(timeout=120.0, verify=verify) as client:
        r = client.post(f"{base}/chat/completions", headers=headers, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"OpenClaw {r.status_code}: {r.text[:400]}")
        data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Некорректный ответ OpenClaw: {data}") from e


def _chat_openai(settings: Any, messages: list[dict[str, Any]]) -> str:
    api_key = (settings.openai_api_key or "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY не задан")
    base = (settings.openai_base_url or "https://api.openai.com/v1").rstrip("/")
    model = settings.openai_model or "gpt-4.1-mini"
    from app.http_util import openai_proxy

    proxy = openai_proxy()
    if not proxy:
        raise RuntimeError(
            "OPENAI_HTTP_PROXY не задан: Platform API с VPS режется как unsupported_country"
        )
    payload = {
        "model": model,
        "temperature": 0.15,
        "max_tokens": 2500,
        "messages": messages,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    verify = SYSTEM_CA if __import__("pathlib").Path(SYSTEM_CA).exists() else True
    with httpx.Client(timeout=90.0, verify=verify, proxy=proxy) as client:
        r = client.post(f"{base}/chat/completions", headers=headers, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"OpenAI {r.status_code}: {r.text[:400]}")
        data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Некорректный ответ OpenAI: {data}") from e


def _chat_groq(settings: Any, messages: list[dict[str, Any]]) -> str:
    api_key = (settings.groq_api_key or "").strip()
    if not api_key:
        raise RuntimeError("GROQ_API_KEY не задан")
    model = settings.groq_model or "llama-3.3-70b-versatile"
    payload = {
        "model": model,
        "temperature": 0.15,
        "max_tokens": 2500,
        "messages": messages,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    verify = SYSTEM_CA if __import__("pathlib").Path(SYSTEM_CA).exists() else True
    proxy = (settings.groq_http_proxy or "").strip() or None
    with httpx.Client(timeout=60.0, verify=verify, proxy=proxy) as client:
        r = client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers=headers,
            json=payload,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Groq {r.status_code}: {r.text[:400]}")
        data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError) as e:
        raise RuntimeError(f"Некорректный ответ Groq: {data}") from e
