# -*- coding: utf-8 -*-
"""Чистка сырья: метаданные канала не должны попадать в экстрактор."""

from __future__ import annotations

import re

from src.ingest.sources import source_meta_phrases

_FORWARD_RE = re.compile(
    r"^\s*(?:forwarded from|переслано от|переслано из)\s*[:\-]?\s*",
    re.IGNORECASE,
)
_HEADER_LABEL_RE = re.compile(
    r"^\s*(?:жизнь футбол)\s*:\s*",
    re.IGNORECASE,
)
_TME_LINE_RE = re.compile(
    r"^\s*(?:https?://)?(?:t\.me|telegram\.me)/\S+\s*$",
    re.IGNORECASE,
)
_AT_USER_RE = re.compile(r"^@[\w\d_]{3,}$")


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def _title_case_variants(title: str) -> list[str]:
    """«Лига Чемпионов» → лига/лиге/лиги чемпионов."""
    words = title.split()
    if not words:
        return []
    variants = [title]
    w0 = words[0]
    tail = " ".join(words[1:])
    if w0.endswith("а") and tail:
        variants.append(f"{w0[:-1]}е {tail}")
        variants.append(f"{w0[:-1]}и {tail}")
    return variants


def clean_source_body(text: str, source: str = "") -> str:
    """Оставить только тело сообщения: без хедеров, форварда, подписей канала."""
    if not text:
        return ""
    phrases = source_meta_phrases(source)
    phrases_l = {_fold(p) for p in phrases}

    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        if _FORWARD_RE.match(line):
            rest = _FORWARD_RE.sub("", line).strip()
            if not rest or _fold(rest) in phrases_l:
                continue
            line = rest
        if _HEADER_LABEL_RE.match(line):
            line = _HEADER_LABEL_RE.sub("", line).strip()
            if not line:
                continue
        # «Лига Чемпионов» / «🏆 Лига Чемпионов» — хедер или подпись канала
        fold = _fold(line)
        fold_bare = _fold(re.sub(r"^[^\wА-Яа-яЁё]+", "", line))
        if (
            fold in phrases_l
            or fold.rstrip(":").strip() in phrases_l
            or fold_bare in phrases_l
            or fold_bare.rstrip(":").strip() in phrases_l
        ):
            continue
        if _AT_USER_RE.match(line):
            continue
        if _TME_LINE_RE.match(line):
            continue
        for p in phrases:
            prefix = f"{p}:"
            if fold.startswith(_fold(prefix)):
                line = line[len(p) :].lstrip(":").strip()
                break
        if line:
            out.append(line)
    cleaned = "\n".join(out).strip()
    return re.sub(r"\n{3,}", "\n\n", cleaned)


def strip_source_leak_from_fact(fact: str, body: str, source: str) -> str:
    """Убрать из факта название канала, которого нет в теле новости."""
    if not fact:
        return fact
    body_l = body.casefold()
    result = fact
    for phrase in source_meta_phrases(source):
        if len(phrase) < 5:
            continue
        for variant in _title_case_variants(phrase):
            if variant.casefold() in body_l:
                continue
            pat = re.compile(
                rf"(?:,)?\s*(?:\b(?:в|во|на)\s+)?{re.escape(variant)}",
                re.IGNORECASE,
            )
            result = pat.sub("", result)
    result = re.sub(r" {2,}", " ", result)
    result = re.sub(r"\s+([:,.])", r"\1", result)
    return result.strip(" \t-–—:")
