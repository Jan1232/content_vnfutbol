"""Platform OpenAI API client for editorial. No OpenClaw, no OAuth."""

from __future__ import annotations

import base64
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from app.config import get_settings
from app.http_util import SYSTEM_CA, openai_proxy
from editorial.jsonutil import parse_json_object

_usage_news_id: ContextVar[str | None] = ContextVar("ed_usage_news_id", default=None)
_usage_task: ContextVar[str] = ContextVar("ed_usage_task", default="")
_benchmark_run_id: ContextVar[str] = ContextVar("ed_benchmark_run_id", default="")


@contextmanager
def usage_scope(
    *,
    news_id: Any = None,
    task: str | None = None,
    benchmark_run_id: str | None = None,
) -> Iterator[None]:
    t_news = _usage_news_id.set(str(news_id) if news_id is not None else _usage_news_id.get())
    t_task = _usage_task.set(task if task is not None else _usage_task.get())
    t_bench = _benchmark_run_id.set(
        benchmark_run_id if benchmark_run_id is not None else _benchmark_run_id.get()
    )
    try:
        yield
    finally:
        _usage_news_id.reset(t_news)
        _usage_task.reset(t_task)
        _benchmark_run_id.reset(t_bench)


@contextmanager
def benchmark_scope(run_id: str) -> Iterator[None]:
    with usage_scope(benchmark_run_id=str(run_id or "")):
        yield


def assert_platform_transport(base_url: str) -> None:
    raw = (base_url or "").strip().lower()
    if "18789" in raw or "openclaw" in raw:
        raise RuntimeError(
            "editorial LLM transport=OpenClaw запрещён: нужен https://api.openai.com/v1"
        )
    host = urlparse(base_url if "://" in (base_url or "") else f"https://{base_url}").netloc
    if "api.openai.com" not in host:
        raise RuntimeError(f"editorial base_url должен быть api.openai.com, сейчас {base_url}")


def _retry_wait(body: str, default: float) -> float:
    blob = body or ""
    m = re.search(r"try again in (\d+(?:\.\d+)?)s\b", blob, re.I)
    if m:
        wait = float(m.group(1)) + 0.4
    else:
        m = re.search(r"try again in (\d+(?:\.\d+)?)ms\b", blob, re.I)
        wait = float(m.group(1)) / 1000.0 + 0.4 if m else default
    if "tokens per min" in blob.lower() or "tpm" in blob.lower():
        wait = max(wait, 20.0)
    return min(70.0, wait)


def _verify() -> str | bool:
    return SYSTEM_CA if Path(SYSTEM_CA).exists() else True


def _is_model_missing(status: int, body: str) -> bool:
    blob = (body or "").lower()
    if status in {404}:
        return True
    if status != 400:
        return False
    return any(
        s in blob
        for s in (
            "model_not_found",
            "does not exist",
            "invalid model",
            "model_not_available",
        )
    )


def _unsupported_param(body: str, name: str) -> bool:
    blob = (body or "").lower()
    return "unsupported" in blob and name.lower() in blob


def _record_usage(
    *,
    task: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    ok: bool,
    note: str = "",
    news_id: str | None = None,
    cached_tokens: int = 0,
    benchmark_run_id: str | None = None,
    ms: int = 0,
    http_status: int = 0,
    request_payload: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    response_text: str = "",
    images_n: int = 0,
    image_bytes: int = 0,
) -> None:
    try:
        from editorial.live_test import is_live_test, live_test_date
        from editorial.usage import (
            _payload_for_log,
            estimate_usd,
            record_benchmark_stage,
            record_llm_call_log,
            record_llm_usage,
        )

        bench = benchmark_run_id if benchmark_run_id is not None else _benchmark_run_id.get()
        note_out = (note or "")[:400]
        if is_live_test():
            tag = f"live_test:{live_test_date()}"
            note_out = f"{tag} {note_out}".strip()[:400]
        nid = news_id if news_id is not None else _usage_news_id.get()
        usd = estimate_usd(int(prompt_tokens or 0), int(completion_tokens or 0), model)
        usage_id = record_llm_usage(
            news_id=nid,
            task=task or _usage_task.get() or "chat",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            ok=ok,
            note=note_out,
            cached_tokens=cached_tokens,
            benchmark_run_id=bench or "",
            ms=ms,
            usd=usd,
        )
        record_llm_call_log(
            usage_id=usage_id,
            news_id=nid,
            task=task or _usage_task.get() or "chat",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            ok=ok,
            note=note_out,
            benchmark_run_id=bench or "",
            ms=ms,
            usd=usd,
            http_status=http_status,
            images_n=images_n,
            image_bytes=image_bytes,
            request_json=_payload_for_log(request_payload, messages=messages),
            response_text=response_text,
        )
        if bench:
            record_benchmark_stage(
                run_id=bench,
                news_id=nid or "",
                stage=task or _usage_task.get() or "chat",
                model=model,
                p_in=prompt_tokens,
                p_out=completion_tokens,
                cached=cached_tokens,
                ms=ms,
            )
    except Exception as e:
        print(f"[editorial] usage log skip: {e}", flush=True)


def _cached_tokens_from_usage(usage: dict[str, Any]) -> int:
    try:
        details = usage.get("prompt_tokens_details") or {}
        return int(details.get("cached_tokens") or usage.get("cached_tokens") or 0)
    except (TypeError, ValueError):
        return 0


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        proxy: str | None = None,
        timeout: float = 60.0,
        max_retry: int = 4,
    ) -> None:
        key = (api_key or "").strip()
        if not key:
            raise RuntimeError("OPENAI_API_KEY не задан")
        self.api_key = key
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        assert_platform_transport(self.base_url)
        self.proxy = (proxy or "").strip() or None
        if not self.proxy:
            raise RuntimeError(
                "OPENAI_HTTP_PROXY / SCRAPER_HTTP_PROXY не задан: "
                "Platform API с VPS режется как unsupported_country"
            )
        self.timeout = timeout
        self.max_retry = max(1, int(max_retry))
        self._omit_temperature: set[str] = set()
        self._prefer_max_tokens: set[str] = set()

    @classmethod
    def from_settings(cls) -> "OpenAIClient":
        settings = get_settings()
        transport = (settings.editorial_llm_transport or "openai").strip().lower()
        if transport != "openai":
            raise RuntimeError(
                f"EDITORIAL_LLM_TRANSPORT={transport}: для editorial разрешён только openai"
            )
        base = (
            (settings.editorial_openai_base_url or "").strip()
            or (settings.openai_base_url or "").strip()
            or "https://api.openai.com/v1"
        )
        return cls(
            api_key=settings.openai_api_key,
            base_url=base,
            proxy=openai_proxy(),
            timeout=float(settings.editorial_llm_timeout or 60),
            max_retry=int(settings.editorial_llm_max_retry or 4),
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _model_chain(self, model: str, fallback: str | list[str] | None) -> list[str]:
        names: list[str] = []
        extra = fallback if isinstance(fallback, (list, tuple)) else ([fallback] if fallback else [])
        for name in (model, *extra):
            name = (name or "").strip()
            if name and name not in names:
                names.append(name)
        if not names:
            raise RuntimeError("не задана модель OpenAI")
        return names

    def _post(self, path: str, payload: dict[str, Any], *, timeout: float | None = None) -> httpx.Response:
        url = f"{self.base_url}{path}"
        last: httpx.Response | None = None
        wait = 2.0
        for attempt in range(self.max_retry):
            with httpx.Client(timeout=timeout or self.timeout, verify=_verify(), proxy=self.proxy) as client:
                r = client.post(url, headers=self._headers(), json=payload)
            last = r
            if r.status_code in {429, 500, 502, 503, 504}:
                wait = _retry_wait(r.text or "", wait)
                print(
                    f"[editorial] openai {r.status_code} {path} retry {attempt + 1}/{self.max_retry} in {wait:.1f}s",
                    flush=True,
                )
                time.sleep(wait)
                wait = min(70.0, max(wait * 1.5, 2.0))
                continue
            return r
        assert last is not None
        return last

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        temperature: float | None = None,
        fallback: str | list[str] | None = None,
        task: str = "chat",
        extra: dict[str, Any] | None = None,
        timeout: float | None = None,
        vision_images_n: int = 0,
        vision_image_bytes: int = 0,
    ) -> str:
        errors: list[str] = []
        for name in self._model_chain(model, fallback):
            payload: dict[str, Any] = {
                "model": name,
                "messages": messages,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            if extra:
                payload.update(extra)
            token_key = "max_tokens" if name in self._prefer_max_tokens else "max_completion_tokens"
            if max_tokens:
                payload[token_key] = int(max_tokens)
            if temperature is not None and name not in self._omit_temperature:
                payload["temperature"] = temperature

            t0 = time.monotonic()
            switched = True
            while switched:
                switched = False
                r = self._post("/chat/completions", payload, timeout=timeout)
                body = r.text or ""
                if r.status_code == 400 and max_tokens and _unsupported_param(body, "max_tokens"):
                    payload.pop("max_tokens", None)
                    payload["max_completion_tokens"] = int(max_tokens)
                    switched = True
                    continue
                if r.status_code == 400 and max_tokens and _unsupported_param(body, "max_completion_tokens"):
                    payload.pop("max_completion_tokens", None)
                    payload["max_tokens"] = int(max_tokens)
                    self._prefer_max_tokens.add(name)
                    switched = True
                    continue
                if r.status_code == 400 and "temperature" in payload and _unsupported_param(body, "temperature"):
                    payload.pop("temperature", None)
                    self._omit_temperature.add(name)
                    switched = True
                    continue
            ms = int((time.monotonic() - t0) * 1000)

            usage = {}
            try:
                data = r.json()
                usage = data.get("usage") or {}
            except Exception:
                data = {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cached_tokens = _cached_tokens_from_usage(usage if isinstance(usage, dict) else {})

            log_kw = dict(
                task=task,
                model=name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                ms=ms,
                http_status=r.status_code,
                request_payload=payload,
                messages=messages,
                images_n=vision_images_n,
                image_bytes=vision_image_bytes,
            )

            if r.status_code >= 400:
                note = f"{r.status_code}: {body[:240]}"
                _record_usage(
                    **log_kw,
                    ok=False,
                    note=note,
                    response_text=body[:8000],
                )
                if _is_model_missing(r.status_code, body):
                    print(f"[editorial] openai model miss {name}: {note}", flush=True)
                    errors.append(note)
                    continue
                raise RuntimeError(f"OpenAI {r.status_code} {name}: {body[:400]}")

            try:
                text = (data["choices"][0]["message"].get("content") or "").strip()
            except (KeyError, IndexError, TypeError) as e:
                _record_usage(
                    **log_kw,
                    ok=False,
                    note=str(e),
                    response_text=str(data)[:8000],
                )
                raise RuntimeError(f"Некорректный ответ OpenAI: {data}") from e
            if not text:
                _record_usage(
                    **log_kw,
                    ok=False,
                    note="empty content",
                    response_text=str(data)[:8000],
                )
                errors.append(f"{name}: empty content")
                continue
            _record_usage(
                **log_kw,
                ok=True,
                response_text=text,
            )
            print(
                f"[editorial] openai {task} model={name} "
                f"in={prompt_tokens} out={completion_tokens} cached={cached_tokens}",
                flush=True,
            )
            return text

        raise RuntimeError("OpenAI chat недоступен: " + " | ".join(errors)[:800])

    def vision(
        self,
        model: str,
        images: list[bytes],
        prompt: str,
        *,
        json_mode: bool = True,
        max_tokens: int | None = 700,
        fallback: str | list[str] | None = None,
        task: str = "image_vision",
    ) -> dict[str, Any]:
        """Один vision-вызов: несколько JPEG ≤512px + текстовый промпт. Возвращает JSON."""
        if not images:
            return {}
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for raw in images:
            if not raw:
                continue
            b64 = base64.b64encode(raw).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
                }
            )
        if len(content) < 2:
            return {}
        messages = [
            {"role": "system", "content": "Отвечай только валидным JSON-объектом, без markdown."},
            {"role": "user", "content": content},
        ]
        img_list = [x for x in images if x]
        return self.chat_json(
            model,
            messages,
            max_tokens=max_tokens,
            fallback=fallback,
            task=task or "image_vision",
            vision_images_n=len(img_list),
            vision_image_bytes=sum(len(x) for x in img_list),
        )

    def chat_json(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        fallback: str | list[str] | None = None,
        task: str = "chat",
        vision_images_n: int = 0,
        vision_image_bytes: int = 0,
    ) -> dict[str, Any]:
        raw = self.chat(
            model,
            messages,
            json_mode=True,
            max_tokens=max_tokens,
            temperature=temperature,
            fallback=fallback,
            task=task,
            vision_images_n=vision_images_n,
            vision_image_bytes=vision_image_bytes,
        )
        return parse_json_object(raw)

    def web_search(
        self,
        model: str,
        query: str,
        *,
        max_results: int = 8,
        task: str = "search",
    ) -> list[dict[str, Any]]:
        q = (query or "").strip()[:160]
        if not q:
            return []
        # Наш prompt крошечный. 16–32k billed prompt — это search_context_size
        # (OpenAI подмешивает страницы). low — минимум Chat Completions.
        messages = [{"role": "user", "content": q}]
        our_chars = len(q)
        payload_extra = {"web_search_options": {"search_context_size": "low"}}
        # Search model: get raw response to harvest annotations even if JSON is messy.
        errors: list[str] = []
        for name in self._model_chain(model, None):
            payload: dict[str, Any] = {
                "model": name,
                "messages": messages,
                "max_completion_tokens": 160,
                **payload_extra,
            }
            t0 = time.monotonic()
            r = self._post("/chat/completions", payload, timeout=max(self.timeout, 120.0))
            ms = int((time.monotonic() - t0) * 1000)
            body = r.text or ""
            try:
                data = r.json()
            except Exception:
                data = {}
            usage = data.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cached_tokens = _cached_tokens_from_usage(usage if isinstance(usage, dict) else {})
            log_kw = dict(
                task=task,
                model=name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                ms=ms,
                http_status=r.status_code,
                request_payload=payload,
                messages=messages,
            )
            if r.status_code >= 400:
                note = f"{r.status_code}: {body[:240]}"
                _record_usage(
                    **log_kw,
                    ok=False,
                    note=note,
                    response_text=body[:8000],
                )
                if _is_model_missing(r.status_code, body):
                    errors.append(note)
                    continue
                raise RuntimeError(f"OpenAI search {r.status_code} {name}: {body[:400]}")
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            text = str(msg.get("content") or "")
            hits = _hits_from_search(text, msg.get("annotations") or [], limit=max_results)
            _record_usage(
                **log_kw,
                ok=bool(hits),
                note="" if hits else "empty search",
                response_text=text[:8000],
            )
            print(
                f"[editorial] openai search model={name} hits={len(hits)} "
                f"our_chars={our_chars} billed_in={prompt_tokens} out={completion_tokens}",
                flush=True,
            )
            return hits
        raise RuntimeError("OpenAI search недоступен: " + " | ".join(errors)[:800])

    def generate_image(
        self,
        model: str,
        prompt: str,
        *,
        size: str = "1024x1536",
        task: str = "image",
    ) -> bytes:
        payload = {
            "model": model,
            "prompt": prompt[:3000],
            "size": size,
            "n": 1,
        }
        t0 = time.monotonic()
        r = self._post("/images/generations", payload, timeout=max(self.timeout, 90.0))
        ms = int((time.monotonic() - t0) * 1000)
        body = r.text or ""
        try:
            data = r.json()
        except Exception:
            data = {}
        log_kw = dict(
            task=task,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            ms=ms,
            http_status=r.status_code,
            request_payload=payload,
        )
        if r.status_code >= 400:
            _record_usage(**log_kw, ok=False, note=body[:240], response_text=body[:4000])
            raise RuntimeError(f"OpenAI image {r.status_code}: {body[:400]}")
        items = data.get("data") or []
        if not items:
            _record_usage(**log_kw, ok=False, note="empty image", response_text=str(data)[:4000])
            raise RuntimeError("OpenAI image: пустой ответ")
        b64 = items[0].get("b64_json")
        if b64:
            raw = base64.b64decode(b64)
            _record_usage(
                **log_kw,
                ok=True,
                note=size,
                response_text=f"[image {len(raw)} bytes b64_json, not stored]",
            )
            return raw
        url = items[0].get("url")
        if not url:
            raise RuntimeError("OpenAI image: нет b64_json/url")
        with httpx.Client(timeout=60.0, verify=_verify(), proxy=self.proxy) as client:
            img = client.get(url)
        if img.status_code >= 400:
            raise RuntimeError(f"OpenAI image download {img.status_code}")
        _record_usage(
            **log_kw,
            ok=True,
            note=size,
            response_text=f"[image {len(img.content)} bytes from url, not stored]",
        )
        return img.content


_client: OpenAIClient | None = None


def get_client() -> OpenAIClient:
    global _client
    if _client is None:
        _client = OpenAIClient.from_settings()
    return _client


def reset_client() -> None:
    global _client
    _client = None


def _domain(url: str) -> str:
    host = (urlparse(url or "").netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def _hits_from_search(text: str, annotations: list[Any], *, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(item: dict[str, Any]) -> None:
        url = str(item.get("url") or "").strip()
        dom = str(item.get("domain") or _domain(url))
        if not url and not item.get("snippet"):
            return
        key = dom or url
        if not key or key in seen:
            return
        seen.add(key)
        out.append(
            {
                "title": str(item.get("title") or "")[:240],
                "url": url,
                "snippet": str(item.get("snippet") or "")[:500],
                "domain": dom,
                "published_at": str(item.get("published_at") or ""),
            }
        )

    try:
        data = parse_json_object(text)
        results = data.get("results")
        if isinstance(results, list):
            for row in results:
                if isinstance(row, dict):
                    add(row)
    except Exception:
        pass

    for ann in annotations:
        if not isinstance(ann, dict):
            continue
        uc = ann.get("url_citation") if ann.get("type") == "url_citation" else None
        if not isinstance(uc, dict):
            continue
        start = int(uc.get("start_index") or 0)
        end = int(uc.get("end_index") or 0)
        snippet = (text[start:end] if text and end > start else "")[:400]
        add(
            {
                "title": uc.get("title") or "",
                "url": uc.get("url") or "",
                "snippet": snippet,
                "domain": _domain(str(uc.get("url") or "")),
            }
        )
    return out[:limit]
