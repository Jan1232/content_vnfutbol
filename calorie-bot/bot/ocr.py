from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import asdict, dataclass
from typing import Any

import httpx
import pytesseract
from PIL import Image
from io import BytesIO

from bot.config import Settings
from bot.parsers import ParsedMeal, _num


@dataclass
class LabelData:
    name: str
    kcal: float
    protein: float
    fat: float
    carbs: float
    per: str  # 100g | 100ml | portion
    package_amount: float | None = None
    package_unit: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LabelData":
        return cls(
            name=str(data.get("name") or "Продукт")[:120],
            kcal=float(data.get("kcal") or 0),
            protein=float(data.get("protein") or 0),
            fat=float(data.get("fat") or 0),
            carbs=float(data.get("carbs") or 0),
            per=str(data.get("per") or "portion"),
            package_amount=_num(data.get("package_amount")),
            package_unit=data.get("package_unit"),
            notes=data.get("notes"),
        )


def _plausible_macro(value: float | None, *, hard_max: float) -> float | None:
    if value is None:
        return None
    if value < 0 or value > hard_max:
        return None
    # Номера заказов / даты вида 260806
    if value >= 500 and abs(value - round(value)) < 1e-6:
        return None
    return value


def sanitize_label(label: LabelData) -> LabelData | None:
    """Отбрасываем OCR-мусор (номер заказа как белок, кДж как ккал и т.п.)."""
    # Явный мусор — не пытаемся «починить» частично
    if (
        label.protein > 100
        or label.fat > 100
        or label.carbs > 120
        or label.kcal > 5000
    ):
        return None

    name = (label.name or "").strip() or "Продукт"
    name = re.sub(r"^\W+", "", name)[:120]

    protein = _plausible_macro(label.protein, hard_max=100)
    fat = _plausible_macro(label.fat, hard_max=100)
    carbs = _plausible_macro(label.carbs, hard_max=120)
    kcal = label.kcal if label.kcal and label.kcal > 0 else 0.0

    # кДж ошибочно записали как ккал (типично 800–5000 при нормальных БЖУ)
    if kcal >= 700 and protein is not None and fat is not None and carbs is not None:
        est = protein * 4 + fat * 9 + carbs * 4
        if est > 0 and kcal > est * 2.5:
            kcal = round(kcal / 4.184, 1)

    if kcal > 1200:
        if protein is not None and fat is not None and carbs is not None:
            est = protein * 4 + fat * 9 + carbs * 4
            if est > 0 and kcal > est * 2:
                kcal = round(est, 1)
            else:
                return None
        else:
            return None

    kcal_was_missing = kcal <= 0
    if kcal_was_missing and any(v is not None and v > 0 for v in (protein, fat, carbs)):
        # Оценка только если есть хотя бы два макроса
        present = sum(1 for v in (protein, fat, carbs) if v is not None and v > 0)
        if present < 2:
            return None
        kcal = round(
            (protein or 0) * 4 + (fat or 0) * 9 + (carbs or 0) * 4,
            1,
        )

    protein_f = float(protein or 0)
    fat_f = float(fat or 0)
    carbs_f = float(carbs or 0)

    if kcal <= 0 and protein_f <= 0 and fat_f <= 0 and carbs_f <= 0:
        return None
    if protein_f > 100 or fat_f > 100 or carbs_f > 120:
        return None

    pkg = label.package_amount
    if pkg is not None and (pkg <= 0 or pkg > 5000 or pkg >= 10000):
        pkg = None

    per = label.per if label.per in {"100g", "100ml", "portion"} else "portion"

    return LabelData(
        name=name,
        kcal=float(kcal),
        protein=protein_f,
        fat=fat_f,
        carbs=carbs_f,
        per=per,
        package_amount=pkg,
        package_unit=label.package_unit if pkg else None,
        notes=label.notes,
    )


def label_is_sane(label: LabelData) -> bool:
    return sanitize_label(label) is not None


VISION_PROMPT = """Ты читаешь этикетку продукта с фото (может быть несколько фото одного товара: КБЖУ и масса нетто).
Верни ТОЛЬКО JSON без markdown:
{
  "name": "понятное короткое название продукта",
  "kcal": число,
  "protein": число,
  "fat": число,
  "carbs": число,
  "per": "100g" | "100ml" | "portion",
  "package_amount": число или null,
  "package_unit": "г"|"мл"|"л"|"шт"|null,
  "notes": "кратко"
}
Правила:
- Бери КБЖУ ТОЛЬКО из блока «пищевая ценность» / «на 100 г» / «на 100 мл» / «на порцию».
- Числа белков/жиров/углеводов обычно 0–80 на 100 г (редко до ~90). НЕ бери номер заказа, дату, штрихкод, артикул (например 260806-111-7247).
- Если есть и кДж, и ккал — в kcal пиши именно ккал (не кДж). Пример: «1172,3 кДж / 280,0 ккал» → kcal=280.
- package_amount — масса/объём нетто упаковки (например 192 г), не номер заказа.
- name — человекочитаемое (например «Наггетсы Есть горячее»), не обрывок «ЕЛИЯ КУЛИНАРНЫЕ».
- Если несколько фото — один продукт: объедини данные.
- Если этикетка нечитаема или КБЖУ не видно уверенно — {"error":"unreadable"}.
"""

ALBUM_VISION_PROMPT = """На фото — этикетки еды. Это может быть:
1) РАЗНЫЕ СТОРОНЫ ОДНОЙ упаковки (бренд / состав / КБЖУ / масса нетто), ИЛИ
2) НЕСКОЛЬКО РАЗНЫХ продуктов (например курица + макароны) — типично для заготовки.

Верни ТОЛЬКО JSON без markdown:
{
  "products": [
    {
      "name": "понятное название",
      "kcal": число,
      "protein": число,
      "fat": число,
      "carbs": число,
      "per": "100g" | "100ml" | "portion",
      "package_amount": число или null,
      "package_unit": "г"|"мл"|"л"|"шт"|null,
      "notes": "кратко"
    }
  ]
}

Правила:
- Разные продукты с явно разным КБЖУ / названиями = ОТДЕЛЬНЫЕ элементы в products. Не склеивай курицу и макароны.
- Две стороны одной банки/пачки = ОДИН элемент в products.
- КБЖУ только из таблицы пищевой ценности; не путай с номером заказа/датой/штрихкодом.
- Если текст повёрнут / боком — всё равно прочитай.
- Если есть кДж и ккал — бери ккал.
- package_amount — нетто (г/мл), не артикул. Если нетто не видно — null.
- Если ничего не разобрал: {"products":[]}.
"""


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _image_content_part(image_bytes: bytes) -> dict[str, Any]:
    mime = "image/jpeg"
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        mime = "image/png"
    b64 = base64.b64encode(image_bytes).decode()
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{b64}"},
    }


async def analyze_label_vision(
    image_bytes: bytes | list[bytes], settings: Settings
) -> LabelData | None:
    if not settings.openclaw_api_key:
        return None
    images = image_bytes if isinstance(image_bytes, list) else [image_bytes]
    if not images:
        return None
    content: list[dict[str, Any]] = [{"type": "text", "text": VISION_PROMPT}]
    content.extend(_image_content_part(img) for img in images[:4])
    payload = {
        "model": settings.openclaw_model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.1,
        "max_tokens": 600,
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                f"{settings.openclaw_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openclaw_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            content_text = r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None

    data = _extract_json(content_text)
    if not data or data.get("error"):
        return None
    return sanitize_label(LabelData.from_dict(data))


def analyze_label_tesseract(image_bytes: bytes) -> LabelData | None:
    try:
        img = Image.open(BytesIO(image_bytes))
        text = pytesseract.image_to_string(img, lang="rus+eng")
    except Exception:
        return None
    if not text.strip():
        return None

    # Убираем блоки заказа/даты — там числа вроде 260806
    clean = re.sub(
        r"(?im)^.*(?:заказ|штрих|barcode|дата\s+изготов|годен).*$",
        " ",
        text,
    )

    def find_macro(patterns: list[str]) -> float | None:
        for p in patterns:
            for m in re.finditer(p, clean, re.I):
                val = _num(m.group(1))
                if val is not None and 0 <= val <= 100:
                    return val
        return None

    # Сначала явное «280 ккал», не путать с кДж
    kcal = None
    m_kcal = re.search(r"(\d+[.,]\d+|\d+)\s*ккал\b", clean, re.I)
    if m_kcal:
        kcal = _num(m_kcal.group(1))
    if kcal is None:
        m_kcal = re.search(
            r"(?:энергетическая\s+ценность|калорийность)[^\d]{0,30}(\d+[.,]\d+|\d+)\s*ккал",
            clean,
            re.I,
        )
        if m_kcal:
            kcal = _num(m_kcal.group(1))

    # Макросы: только 1–2 цифры (+ десятичная), с «г» рядом предпочтительно
    protein = find_macro(
        [
            r"белк[иа]\s*[:=]?\s*(\d{1,2}(?:[.,]\d+)?)\s*г?",
            r"protein[s]?\s*[:=]?\s*(\d{1,2}(?:[.,]\d+)?)",
        ]
    )
    fat = find_macro(
        [
            r"жир[ыа]\s*[:=]?\s*(\d{1,2}(?:[.,]\d+)?)\s*г?",
            r"fat[s]?\s*[:=]?\s*(\d{1,2}(?:[.,]\d+)?)",
        ]
    )
    carbs = find_macro(
        [
            r"углевод[ыа]\s*[:=]?\s*(\d{1,2}(?:[.,]\d+)?)\s*г?",
            r"carb(?:ohydrate)?s?\s*[:=]?\s*(\d{1,2}(?:[.,]\d+)?)",
        ]
    )
    if kcal is None and all(v is None for v in (protein, fat, carbs)):
        return None

    per = "portion"
    if re.search(r"(?:на\s*)?100\s*(?:г|g)\b", clean, re.I):
        per = "100g"
    if re.search(r"(?:на\s*)?100\s*(?:мл|ml)\b", clean, re.I):
        per = "100ml"

    pkg_amount = None
    pkg_unit = None
    pm = re.search(
        r"(?:масса\s+нетто|нетто|масса)[^\d]{0,20}(\d{1,4}(?:[.,]\d+)?)\s*(кг|г|g|мл|ml|л|l)\b",
        clean,
        re.I,
    )
    if pm:
        pkg_amount = _num(pm.group(1))
        u = pm.group(2).lower()
        pkg_unit = {"l": "л", "ml": "мл", "g": "г", "кг": "кг"}.get(u, u)
        if pkg_amount and pkg_amount >= 10000:
            pkg_amount = None

    name = "Продукт с этикетки"
    for line in clean.splitlines():
        line = line.strip()
        if len(line) < 8:
            continue
        if re.search(
            r"белк|жир|углевод|ккал|кдж|состав|заказ|изготов|годен|адрес|ооо",
            line,
            re.I,
        ):
            continue
        if re.search(r"наггет|котлет|йогурт|напиток|есть\s+горяч", line, re.I):
            name = line[:100]
            break
        if name == "Продукт с этикетки":
            name = line[:100]

    return sanitize_label(
        LabelData(
            name=name,
            kcal=float(kcal or 0),
            protein=float(protein or 0),
            fat=float(fat or 0),
            carbs=float(carbs or 0),
            per=per,
            package_amount=pkg_amount,
            package_unit=pkg_unit,
        )
    )


def _encode_image(img: Image.Image, *, quality: int = 90) -> bytes:
    buf = BytesIO()
    rgb = img.convert("RGB")
    rgb.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def _rotated_image_bytes(image_bytes: bytes, angle: int) -> bytes:
    """angle — против часовой (PIL rotate)."""
    img = Image.open(BytesIO(image_bytes))
    return _encode_image(img.rotate(angle, expand=True))


async def analyze_label(image_bytes: bytes, settings: Settings) -> LabelData | None:
    """Читает этикетку; параллельно пробует повороты (текст часто боком)."""
    # Оригинал + типичные повороты боком сразу — иначе боковые пачки «теряются»
    angles = (0, 270, 90, 180)

    async def _try(angle: int) -> LabelData | None:
        try:
            data = (
                image_bytes
                if angle == 0
                else _rotated_image_bytes(image_bytes, angle)
            )
        except Exception:
            return None
        return await analyze_label_vision(data, settings)

    results = await asyncio.gather(
        *[_try(a) for a in angles],
        return_exceptions=True,
    )
    # Предпочитаем оригинал, потом 270/90/180
    for r in results:
        if isinstance(r, LabelData):
            return r

    for angle in angles:
        try:
            data = (
                image_bytes
                if angle == 0
                else _rotated_image_bytes(image_bytes, angle)
            )
            tess = analyze_label_tesseract(data)
        except Exception:
            tess = None
        if tess:
            return tess
    return None


async def analyze_labels(
    images: list[bytes], settings: Settings
) -> LabelData | None:
    """Разбор фото одного продукта (1–2 кадра). Для альбома используй analyze_album."""
    products = await analyze_album(images, settings)
    if not products:
        return None
    if len(products) == 1:
        return products[0]
    # Несколько продуктов в одном вызове — не склеиваем
    return products[0]


async def analyze_album_vision(
    images: list[bytes], settings: Settings
) -> list[LabelData] | None:
    """Один запрос по всему альбому: сколько уникальных продуктов."""
    if not settings.openclaw_api_key or not images:
        return None
    content: list[dict[str, Any]] = [{"type": "text", "text": ALBUM_VISION_PROMPT}]
    content.extend(_image_content_part(img) for img in images[:6])
    payload = {
        "model": settings.openclaw_model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.1,
        "max_tokens": 1200,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(
                f"{settings.openclaw_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.openclaw_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            content_text = r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None

    data = _extract_json(content_text)
    if not data:
        return None
    raw_products = data.get("products")
    if not isinstance(raw_products, list):
        # модель могла вернуть один объект как раньше
        if data.get("error"):
            return []
        if data.get("kcal") is not None or data.get("name"):
            one = sanitize_label(LabelData.from_dict(data))
            return [one] if one else []
        return None
    out: list[LabelData] = []
    for item in raw_products:
        if isinstance(item, dict) and not item.get("error"):
            clean = sanitize_label(LabelData.from_dict(item))
            if clean:
                out.append(clean)
    return out


async def analyze_album(
    images: list[bytes], settings: Settings
) -> list[LabelData]:
    """Разбор альбома: по кадрам + склейка дублей; для 2–4 фото — сверка vision."""
    if not images:
        return []
    if len(images) == 1:
        one = await analyze_label(images[0], settings)
        return [one] if one else []

    results = await asyncio.gather(
        *[analyze_label(img, settings) for img in images],
        return_exceptions=True,
    )
    labels: list[LabelData] = []
    for r in results:
        if isinstance(r, LabelData):
            labels.append(r)
        elif isinstance(r, BaseException):
            continue

    clustered = cluster_album_labels(labels)

    # Два+ кадра всё ещё выглядят как разные продукты — спросим vision целиком
    # Важно: НЕ схлопывать к меньшему числу, если локально уже есть разные КБЖУ.
    if 2 <= len(images) <= 4 and len(clustered) >= 2:
        multi = await analyze_album_vision(images, settings)
        multi_ok = [p for p in (multi or []) if label_has_macros(p)] if multi is not None else None
        if multi_ok is not None and len(multi_ok) > len(clustered):
            return multi_ok
        if multi_ok is not None and len(multi_ok) == len(clustered):
            # Одинаковое число — оставим локальный кластер (цифры уже сверены по кадрам)
            return clustered
        # multi вернул меньше: доверяем локальным кадрам, если продукты явно разные
        if len(clustered) >= 2:
            distinct = True
            for i in range(len(clustered)):
                for j in range(i + 1, len(clustered)):
                    if likely_same_product(clustered[i], clustered[j]):
                        distinct = False
                        break
                if not distinct:
                    break
            if distinct:
                return clustered
        if multi_ok:
            return multi_ok

    # Альбом из ровно 2 фото: сильный prior «один товар с двух сторон»
    if len(images) == 2 and len(clustered) == 2:
        a, b = clustered[0], clustered[1]
        if likely_same_product(a, b) or (
            macros_similar(a, b, kcal_tol=25, prot_tol=5) and package_compatible(a, b)
        ):
            return [merge_labels(a, b)]

    return clustered


def _name_tokens(name: str) -> set[str]:
    stop = {
        "с",
        "и",
        "из",
        "на",
        "в",
        "со",
        "для",
        "без",
        "вкусом",
        "вкус",
        "готовая",
        "еда",
        "лавка",
        "продукт",
        "этикетки",
        "упаковка",
        "масса",
        "нетто",
        "порц",
        "порция",
        "напиток",
        "кисломолочный",
        "обезжиренный",
        "содержанием",
        "высоким",
        "высокий",
        "белка",
        "белком",
        "жирностью",
        "high",
        "pro",
        "хай",
        "про",
    }
    tokens = re.findall(r"[а-яёa-z0-9]+", (name or "").lower().replace("ё", "е"))
    return {t for t in tokens if len(t) > 2 and t not in stop}


def _name_overlap_ratio(a: LabelData, b: LabelData) -> float:
    ta, tb = _name_tokens(a.name), _name_tokens(b.name)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def names_compatible(a: LabelData, b: LabelData) -> bool:
    """Совместимы ли названия для склейки двух фото одного товара."""
    generic = {"продукт", "продукт с этикетки", ""}
    na = (a.name or "").strip().lower().replace("ё", "е")
    nb = (b.name or "").strip().lower().replace("ё", "е")
    if na in generic or nb in generic:
        return True
    if na in nb or nb in na:
        return True
    if _name_overlap_ratio(a, b) >= 0.2:
        return True
    ta, tb = _name_tokens(na), _name_tokens(nb)
    if ta & tb:
        return True
    return False


def macros_similar(
    a: LabelData,
    b: LabelData,
    *,
    kcal_tol: float = 18,
    prot_tol: float = 3.5,
    carb_tol: float = 4.0,
) -> bool:
    if not (label_has_macros(a) and label_has_macros(b)):
        return False
    return (
        abs(a.kcal - b.kcal) <= kcal_tol
        and abs(a.protein - b.protein) <= prot_tol
        and abs(a.carbs - b.carbs) <= carb_tol
    )


def package_compatible(a: LabelData, b: LabelData) -> bool:
    if not a.package_amount or not b.package_amount:
        return True
    tol = max(5.0, 0.08 * max(a.package_amount, b.package_amount))
    return abs(a.package_amount - b.package_amount) <= tol


def likely_same_product(a: LabelData, b: LabelData) -> bool:
    """
    Один товар с разных ракурсов / сторон этикетки.
    Вредных продуктов нет — но дублировать одну упаковку дважды нельзя.
    """
    # Классика: КБЖУ на одном фото, масса на другом
    if labels_complementary(a, b):
        if label_has_macros(a) and label_has_macros(b) and not macros_similar(a, b):
            return False
        return names_compatible(a, b) or not (
            label_has_macros(a) and label_has_macros(b)
        )

    # Оба кадра «полные», но это одна банка: похожие КБЖУ + масса
    if macros_similar(a, b) and package_compatible(a, b):
        if names_compatible(a, b) or _name_overlap_ratio(a, b) >= 0.15:
            return True
        # Очень близкие цифры и одинаковая нетто — одна SKU, даже если
        # на сторонах разные маркетинговые названия вкуса
        if (
            abs(a.kcal - b.kcal) <= 10
            and abs(a.protein - b.protein) <= 2
            and a.package_amount
            and b.package_amount
            and abs(a.package_amount - b.package_amount) <= 1
        ):
            return True

    return False


def should_pair_photos(a: LabelData, b: LabelData) -> bool:
    """Можно ли склеить два кадра в один продукт."""
    return likely_same_product(a, b)


def _pair_score(a: LabelData, b: LabelData) -> float:
    """Чем выше — тем увереннее, что это один продукт."""
    if not likely_same_product(a, b):
        return -1.0
    score = 0.0
    if macros_similar(a, b):
        score += 3.0
        score += max(0.0, 2.0 - abs(a.kcal - b.kcal) / 10.0)
        score += max(0.0, 1.5 - abs(a.protein - b.protein) / 2.0)
    if package_compatible(a, b) and a.package_amount and b.package_amount:
        score += 2.0
    if labels_complementary(a, b):
        score += 2.5
    score += _name_overlap_ratio(a, b) * 2.0
    if names_compatible(a, b):
        score += 1.0
    return score


def cluster_album_labels(labels: list[LabelData]) -> list[LabelData]:
    """Сгруппировать кадры альбома в отдельные продукты."""
    if len(labels) <= 1:
        return list(labels)

    n = len(labels)
    used = [False] * n
    products: list[LabelData] = []

    # Жадное спаривание по убыванию уверенности
    pairs: list[tuple[float, int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            sc = _pair_score(labels[i], labels[j])
            if sc >= 0:
                pairs.append((sc, i, j))
    pairs.sort(reverse=True)

    for _, i, j in pairs:
        if used[i] or used[j]:
            continue
        products.append(merge_labels(labels[i], labels[j]))
        used[i] = used[j] = True

    for i in range(n):
        if not used[i]:
            products.append(labels[i])

    # Отбрасываем одиночные кадры только с массой (без КБЖУ) — это хвосты пар
    return [p for p in products if label_has_macros(p)]


def label_has_macros(label: LabelData) -> bool:
    return label.kcal > 0 or label.protein > 0 or label.fat > 0 or label.carbs > 0


def label_ready_to_save(label: LabelData) -> bool:
    """Можно записать: есть КБЖУ и либо порция целиком, либо известна масса упаковки."""
    if not label_has_macros(label):
        return False
    if label.per in {"100g", "100ml"}:
        return label.package_amount is not None and label.package_amount > 0
    return True


def needs_package_weight(label: LabelData) -> bool:
    """Нужна масса/объём упаковки (или второе фото / подпись)."""
    return label.per in {"100g", "100ml"} and not (
        label.package_amount and label.package_amount > 0
    )


def labels_complementary(a: LabelData, b: LabelData) -> bool:
    """Два фото одного продукта: одно даёт КБЖУ, другое — массу (или дополняет)."""
    a_m, b_m = label_has_macros(a), label_has_macros(b)
    a_p = bool(a.package_amount and a.package_amount > 0)
    b_p = bool(b.package_amount and b.package_amount > 0)
    if a_m and not a_p and b_p:
        return True
    if b_m and not b_p and a_p:
        return True
    if a_m and b_p and not a_p:
        return True
    if b_m and a_p and not b_p:
        return True
    if (a_m and not b_m and b_p) or (b_m and not a_m and a_p):
        return True
    return False


def merge_labels(a: LabelData, b: LabelData) -> LabelData:
    """Склеить данные с двух фото одного продукта."""
    a_m, b_m = label_has_macros(a), label_has_macros(b)

    # КБЖУ: предпочитаем кадр с более полными/согласованными цифрами
    if a_m and b_m:
        # Если оба на порцию/упаковку с близкими цифрами — усредним слабо, возьмём более «полный» name
        macros_src = a
        # Предпочитаем per 100g/100ml если один так размечен
        if b.per in {"100g", "100ml"} and a.per == "portion":
            macros_src = b
        elif a.per in {"100g", "100ml"} and b.per == "portion":
            macros_src = a
        elif abs(b.protein - a.protein) < 0.5 and b.kcal >= a.kcal:
            # чуть более полные ккал при том же белке
            macros_src = b if (b.fat + b.carbs) >= (a.fat + a.carbs) else a
    elif b_m:
        macros_src = b
    else:
        macros_src = a

    name = a.name
    for candidate in (a.name, b.name):
        if candidate and candidate not in {"Продукт", "Продукт с этикетки"}:
            if name in {"Продукт", "Продукт с этикетки"} or len(candidate) > len(name):
                name = candidate

    package_amount = a.package_amount or b.package_amount
    package_unit = a.package_unit if a.package_amount else (b.package_unit or a.package_unit)
    if not a.package_amount and b.package_amount:
        package_amount = b.package_amount
        package_unit = b.package_unit

    per = macros_src.per
    if per == "portion" and package_amount and (
        a.per in {"100g", "100ml"} or b.per in {"100g", "100ml"}
    ):
        per = a.per if a.per in {"100g", "100ml"} else b.per

    # Если оба кадра дали КБЖУ «на всю упаковку» (per=portion) с одной массой —
    # оставляем как portion, не удваиваем.
    notes_parts = [n for n in (a.notes, b.notes) if n]
    return LabelData(
        name=(name or "Продукт")[:120],
        kcal=macros_src.kcal,
        protein=macros_src.protein,
        fat=macros_src.fat,
        carbs=macros_src.carbs,
        per=per,
        package_amount=package_amount,
        package_unit=package_unit,
        notes="; ".join(notes_parts) if notes_parts else None,
    )


def scale_label_to_meal(
    label: LabelData,
    *,
    amount: float | None = None,
    amount_unit: str | None = None,
    use_full_package: bool = False,
) -> ParsedMeal:
    """Пересчёт КБЖУ с этикетки на съеденную порцию."""
    kcal, protein, fat, carbs = label.kcal, label.protein, label.fat, label.carbs
    final_amount = amount
    final_unit = amount_unit

    def to_ml(value: float, unit: str | None) -> float | None:
        if not unit:
            return None
        u = unit.lower()
        if u in {"мл", "ml"}:
            return value
        if u in {"л", "l"}:
            return value * 1000
        return None

    def to_g(value: float, unit: str | None) -> float | None:
        if not unit:
            return None
        u = unit.lower()
        if u in {"г", "гр", "g"}:
            return value
        if u in {"кг", "kg"}:
            return value * 1000
        return None

    if label.per in {"100g", "100ml"}:
        if use_full_package and label.package_amount:
            amount = label.package_amount
            amount_unit = label.package_unit
            final_amount = amount
            final_unit = amount_unit

        factor = 1.0
        if amount is not None:
            if label.per == "100ml":
                ml = to_ml(amount, amount_unit)
                if ml is not None:
                    factor = ml / 100.0
                    final_amount, final_unit = ml, "мл"
                elif label.package_amount and amount_unit in {None, "шт"}:
                    # N штук упаковки
                    pkg_ml = to_ml(label.package_amount, label.package_unit)
                    if pkg_ml:
                        factor = (pkg_ml * amount) / 100.0
                        final_amount, final_unit = pkg_ml * amount, "мл"
            else:
                grams = to_g(amount, amount_unit or "г")
                if grams is not None:
                    factor = grams / 100.0
                    final_amount, final_unit = grams, "г"
                elif label.package_amount and amount_unit in {None, "шт"}:
                    pkg_g = to_g(label.package_amount, label.package_unit)
                    if pkg_g:
                        factor = (pkg_g * amount) / 100.0
                        final_amount, final_unit = pkg_g * amount, "г"

        kcal *= factor
        protein *= factor
        fat *= factor
        carbs *= factor
    elif use_full_package:
        final_amount = label.package_amount
        final_unit = label.package_unit
    elif amount is not None and amount_unit == "шт" and amount != 1:
        kcal *= amount
        protein *= amount
        fat *= amount
        carbs *= amount
        final_amount, final_unit = amount, "шт"

    return ParsedMeal(
        name=label.name,
        kcal=round(kcal, 1),
        protein=round(protein, 1),
        fat=round(fat, 1),
        carbs=round(carbs, 1),
        amount=final_amount,
        amount_unit=final_unit,
    )


def needs_portion(label: LabelData) -> bool:
    """Устарело: порцию больше не спрашиваем — по умолчанию вся упаковка."""
    return needs_package_weight(label)
