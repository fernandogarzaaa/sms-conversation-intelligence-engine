"""Real (network) LLM providers behind the `LLMProvider` interface.

    LLMProvider
    ├── MockProvider                deterministic rules (tests, demo, default)
    └── HTTPLLMProvider             shared: retries, JSON extraction, usage, cost
        ├── OpenAICompatibleProvider  POST {base_url}/chat/completions
        │     presets: OpenAIProvider, OpenRouterProvider, LocalProvider (Ollama / vLLM / LM Studio)
        └── AnthropicProvider         POST {base_url}/v1/messages

Every call returns a `ProviderResponse` with the parsed payload, the raw model
text, the model id, measured latency, token usage, and `cost_usd` (reported by
the API when available, otherwise computed from an optional per-MTok price).

Errors raise `LLMError`. The wrappers (`MultiIntentClassifier`,
`EntityExtractor`, `ResponseGenerator`) catch them and fail safe, so an outage
or a malformed answer turns into "human review", never an unsafe send.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from sms_engine.ai import prompts
from sms_engine.ai.provider import GenerateSpec, LLMProvider, ProviderResponse

log = logging.getLogger("sms_engine.ai")

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}

# Carrier-level opt-out keywords (CTIA). When the whole message is one of these,
# STOP_OPTOUT is guaranteed regardless of what the model says.
_CARRIER_STOP = re.compile(r"^\s*(stop|stopall|unsubscribe|cancel|end|quit|optout|opt out|opt-out|revoke)\s*[.!]*\s*$", re.I)


class LLMError(RuntimeError):
    """A provider call failed (network, HTTP status, timeout, unparseable output)."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False, raw: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable
        self.raw = raw


# --- JSON extraction ---------------------------------------------------------------

_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)


def extract_json_object(text: str) -> dict:
    """Pull one JSON object out of model output: bare JSON, ```json fences```,
    or JSON preceded/followed by prose. Raises ValueError if none is found."""
    if text is None:
        raise ValueError("empty model output")
    s = text.strip()
    if not s:
        raise ValueError("empty model output")
    candidates: list[str] = [s]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(s)]
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError):
            pass
    # scan for the first balanced {...} that parses
    dec = json.JSONDecoder()
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        try:
            obj, _end = dec.raw_decode(s[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError(f"no JSON object in model output: {s[:120]!r}")


# --- payload normalisation -------------------------------------------------------------

def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "1", "y"}
    return False


def _as_opt_str(v: Any) -> str | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (list, dict)):
        return None
    s = str(v).strip()
    if not s or s.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return s[:200]


def _as_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if x is not None and str(x).strip()][:10]
    return []


def normalize_classification(obj: dict, text: str) -> dict:
    raw_intents = obj.get("intents", [])
    if isinstance(raw_intents, dict):  # {"PURE_POSITIVE": 0.9, ...}
        raw_intents = [{"type": k, "confidence": v} for k, v in raw_intents.items()]
    intents = []
    for it in raw_intents if isinstance(raw_intents, list) else []:
        if isinstance(it, str):
            it = {"type": it, "confidence": 0.8}
        if not isinstance(it, dict):
            continue
        intents.append({
            "type": str(it.get("type") or it.get("label") or "").strip().upper(),
            "confidence": it.get("confidence", 0.0),
            "rationale": str(it.get("rationale", ""))[:200],
        })
    injected = _as_bool(obj.get("possible_prompt_injection"))
    flags = obj.get("flags")
    if isinstance(flags, dict):
        injected = injected or _as_bool(flags.get("possible_prompt_injection"))
    # deterministic backstops (defence in depth; the model is not trusted alone)
    from sms_engine.ai.mock_provider import PROMPT_INJECTION

    if PROMPT_INJECTION.search(text or ""):
        injected = True
    if _CARRIER_STOP.match(text or "") and not any(i["type"] == "STOP_OPTOUT" for i in intents):
        intents.append({"type": "STOP_OPTOUT", "confidence": 0.99, "rationale": "carrier opt-out keyword"})
    return {"intents": intents, "entities": {}, "flags": {"possible_prompt_injection": bool(injected)}}


def normalize_entities(obj: dict) -> dict:
    out: dict = {}
    for key in prompts.ENTITY_FIELD_NAMES:
        v = obj.get(key)
        if key in ("referral_mentioned", "repeat_business_mentioned"):
            out[key] = _as_bool(v)
        elif key in ("urls", "money_terms"):
            out[key] = _as_str_list(v)
        else:
            s = _as_opt_str(v)
            if s is not None:
                out[key] = s
    if "corrected_name" in out:
        out["corrected_name"] = out["corrected_name"].split()[0].strip(".,!").title()
    if "stated_wrong_name" in out:
        out["stated_wrong_name"] = out["stated_wrong_name"].split()[0].strip(".,!").title()
    return out


def normalize_generation(obj: dict) -> dict:
    text = obj.get("text") or obj.get("reply") or obj.get("message") or ""
    text = re.sub(r"\s+", " ", str(text)).strip()
    actions = []
    for a in _as_str_list(obj.get("actions")):
        a = a.strip().lower()
        if a and a not in actions:
            actions.append(a)
    return {"text": text, "actions": actions or (["acknowledgement"] if text else [])}


# --- base HTTP provider ---------------------------------------------------------------

class HTTPLLMProvider(LLMProvider):
    """Shared transport: timeouts, bounded retries with jittered backoff, JSON
    extraction, usage/cost accounting. Subclasses implement `_request`/`_parse`."""

    name = "http"
    default_base_url = ""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 20.0,
        max_retries: int = 2,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
        temperature: float | None = None,
        max_tokens_classify: int = 1024,
        max_tokens_extract: int = 1024,
        max_tokens_generate: int = 1024,
        price_per_mtok: tuple[float, float] | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict | None = None,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        name: str | None = None,
    ) -> None:
        if not model:
            raise ValueError("model is required")
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.temperature = temperature
        self.max_tokens = {
            "classify": max_tokens_classify,
            "extract": max_tokens_extract,
            "generate": max_tokens_generate,
        }
        self.price_per_mtok = price_per_mtok
        self.extra_headers = dict(extra_headers or {})
        self.extra_body = dict(extra_body or {})
        self._sleep = sleep
        if name:
            self.name = name
        self._client = client or httpx.Client(timeout=timeout, transport=transport)
        # running totals, handy for eval reports
        self.calls = 0
        self.total_cost_usd = 0.0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    # -- LLMProvider ----------------------------------------------------------------

    def classify_intents(self, text: str, state_summary: dict) -> ProviderResponse:
        return self._json_call(
            "classify", prompts.CLASSIFY_SYSTEM, prompts.classify_user(text, state_summary),
            lambda obj: normalize_classification(obj, text),
        )

    def extract_entities(self, text: str, intents: list[str]) -> ProviderResponse:
        return self._json_call(
            "extract", prompts.EXTRACT_SYSTEM, prompts.extract_user(text, intents), normalize_entities,
        )

    def generate_reply(self, spec: GenerateSpec) -> ProviderResponse:
        return self._json_call(
            "generate", prompts.generate_system(spec), prompts.generate_user(spec), normalize_generation,
        )

    def classify_batch(self, texts: list[str], *, max_tokens: int | None = None) -> list[dict]:
        """Classify several messages in ONE request (same label definitions).

        Evaluation helper for rate-limited or per-request-billed endpoints; the
        live pipeline always classifies one message per call. Returns one
        normalised payload per input, in order (missing items -> no intents).
        """
        content, _usage, _cost, _model = self._complete(
            prompts.CLASSIFY_SYSTEM + "\n\n" + prompts.CLASSIFY_BATCH_ADDENDUM,
            prompts.classify_batch_user(texts),
            max_tokens or max(4096, 400 * len(texts)),
        )
        try:
            obj = extract_json_object(content)
        except ValueError as exc:
            raise LLMError(f"{self.name} classify_batch: unparseable output: {exc}", raw=content) from exc
        results = obj.get("results")
        if not isinstance(results, list):
            raise LLMError(f"{self.name} classify_batch: no 'results' list (truncated output?)", raw=content)
        by_id = {}
        for item in results:
            if isinstance(item, dict):
                key = str(item.get("id", "")).strip().lower().lstrip("m")
                by_id[key] = item
        return [normalize_classification(by_id.get(str(i + 1), {}), t) for i, t in enumerate(texts)]

    def close(self) -> None:
        self._client.close()

    # -- internals ----------------------------------------------------------------------

    def _json_call(self, task: str, system: str, user: str, normalize: Callable[[dict], dict]) -> ProviderResponse:
        t0 = time.perf_counter()
        content, usage, cost, model = self._complete(system, user, self.max_tokens[task])
        latency_ms = (time.perf_counter() - t0) * 1000.0
        try:
            obj = extract_json_object(content)
        except ValueError as exc:
            raise LLMError(f"{self.name} {task}: unparseable output: {exc}", raw=content) from exc
        payload = normalize(obj)
        return ProviderResponse(
            payload=payload,
            raw=content,
            model=f"{self.name}:{model}",
            latency_ms=latency_ms,
            cost_usd=cost,
            input_tokens=usage.get("input", 0),
            output_tokens=usage.get("output", 0),
        )

    def _complete(self, system: str, user: str, max_tokens: int) -> tuple[str, dict, float, str]:
        url, headers, body = self._request(system, user, max_tokens)
        headers = {"content-type": "application/json", **headers, **self.extra_headers}
        body = {**body, **self.extra_body}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.post(url, headers=headers, json=body, timeout=self.timeout)
            except httpx.TimeoutException as exc:
                err = LLMError(f"{self.name}: timeout after {self.timeout}s", retryable=True)
                err.__cause__ = exc
            except httpx.TransportError as exc:
                err = LLMError(f"{self.name}: network error: {exc}", retryable=True)
                err.__cause__ = exc
            else:
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise LLMError(f"{self.name}: non-JSON response body", status=resp.status_code,
                                       raw=resp.text[:500]) from exc
                    content, usage, cost, model = self._parse(data)
                    if cost == 0.0 and self.price_per_mtok and usage:
                        pin, pout = self.price_per_mtok
                        cost = usage.get("input", 0) * pin / 1e6 + usage.get("output", 0) * pout / 1e6
                    self.calls += 1
                    self.total_cost_usd += cost
                    self.total_input_tokens += usage.get("input", 0)
                    self.total_output_tokens += usage.get("output", 0)
                    return content, usage, cost, model
                retryable = resp.status_code in _RETRY_STATUS
                err = LLMError(
                    f"{self.name}: HTTP {resp.status_code}: {resp.text[:300]}",
                    status=resp.status_code, retryable=retryable, raw=resp.text[:500],
                )
                if retryable and attempt <= self.max_retries:
                    delay = self._retry_after(resp)
                    if delay is not None:
                        log.warning("%s; retrying in %.2fs (attempt %d)", err, delay, attempt)
                        self._sleep(delay)
                        continue
            if not err.retryable or attempt > self.max_retries:
                raise err
            delay = self._backoff(attempt)
            log.warning("%s; retrying in %.2fs (attempt %d)", err, delay, attempt)
            self._sleep(delay)

    def _backoff(self, attempt: int) -> float:
        base = self.backoff_base * (2 ** (attempt - 1))
        return min(self.backoff_max, base + random.uniform(0, self.backoff_base))

    def _retry_after(self, resp: httpx.Response) -> float | None:
        h = resp.headers.get("retry-after")
        if h:
            try:
                return min(self.backoff_max, max(0.0, float(h)))
            except ValueError:
                pass
        return None

    # subclasses
    def _request(self, system: str, user: str, max_tokens: int) -> tuple[str, dict, dict]:  # pragma: no cover
        raise NotImplementedError

    def _parse(self, data: dict) -> tuple[str, dict, float, str]:  # pragma: no cover
        raise NotImplementedError


# --- OpenAI-compatible chat completions -------------------------------------------------------

class OpenAICompatibleProvider(HTTPLLMProvider):
    """Any `/chat/completions` endpoint: OpenAI, OpenRouter, Ollama, vLLM, LM Studio, ..."""

    name = "openai-compatible"
    default_base_url = "https://api.openai.com/v1"

    def __init__(self, *, json_mode: bool = False, **kw) -> None:
        super().__init__(**kw)
        self.json_mode = json_mode

    def _request(self, system: str, user: str, max_tokens: int) -> tuple[str, dict, dict]:
        headers = {}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        body: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        return f"{self.base_url}/chat/completions", headers, body

    def _parse(self, data: dict) -> tuple[str, dict, float, str]:
        choices = data.get("choices") or []
        if not choices:
            err = data.get("error")
            raise LLMError(f"{self.name}: no choices in response{': ' + str(err) if err else ''}",
                           raw=json.dumps(data)[:500])
        msg = choices[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):  # some servers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        content = content or ""
        u = data.get("usage") or {}
        usage = {"input": int(u.get("prompt_tokens") or 0), "output": int(u.get("completion_tokens") or 0)}
        cost = 0.0
        try:
            cost = float(u.get("cost") or 0.0)  # OpenRouter reports this
        except (TypeError, ValueError):
            cost = 0.0
        return content, usage, cost, str(data.get("model") or self.model)


class OpenAIProvider(OpenAICompatibleProvider):
    name = "openai"
    default_base_url = "https://api.openai.com/v1"

    def __init__(self, *, model: str = "gpt-4.1-mini", json_mode: bool = True, **kw) -> None:
        super().__init__(model=model, json_mode=json_mode, **kw)


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"
    default_base_url = "https://openrouter.ai/api/v1"

    def __init__(self, *, model: str = "anthropic/claude-haiku-5.5", **kw) -> None:
        extra_body = {"usage": {"include": True}, **(kw.pop("extra_body", None) or {})}
        super().__init__(model=model, extra_body=extra_body, **kw)


class LocalProvider(OpenAICompatibleProvider):
    """A local OpenAI-compatible server (Ollama, vLLM, LM Studio, llama.cpp). No key."""

    name = "local"
    default_base_url = "http://localhost:11434/v1"

    def __init__(self, *, model: str = "llama3.1:8b", timeout: float = 60.0, **kw) -> None:
        kw.setdefault("price_per_mtok", (0.0, 0.0))
        super().__init__(model=model, timeout=timeout, **kw)


# --- Anthropic Messages API ----------------------------------------------------------------------

# $ per million tokens (input, output) — Anthropic first-party list prices.
ANTHROPIC_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


class AnthropicProvider(HTTPLLMProvider):
    name = "anthropic"
    default_base_url = "https://api.anthropic.com"

    def __init__(
        self,
        *,
        model: str = "claude-haiku-5-5",
        anthropic_version: str = "2023-06-01",
        effort: str | None = "low",
        **kw,
    ) -> None:
        kw.setdefault("price_per_mtok", ANTHROPIC_PRICES.get(model))
        super().__init__(model=model, **kw)
        self.anthropic_version = anthropic_version
        self.effort = effort

    def _request(self, system: str, user: str, max_tokens: int) -> tuple[str, dict, dict]:
        headers = {"anthropic-version": self.anthropic_version}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        body: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self.effort:
            body["output_config"] = {"effort": self.effort}
        if self.temperature is not None:
            body["temperature"] = self.temperature
        return f"{self.base_url}/v1/messages", headers, body

    def _parse(self, data: dict) -> tuple[str, dict, float, str]:
        if data.get("type") == "error":
            raise LLMError(f"anthropic: {data.get('error')}", raw=json.dumps(data)[:500])
        if data.get("stop_reason") == "refusal":
            raise LLMError("anthropic: model refused", raw=json.dumps(data)[:500])
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
        u = data.get("usage") or {}
        usage = {
            "input": int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0)
            + int(u.get("cache_creation_input_tokens") or 0),
            "output": int(u.get("output_tokens") or 0),
        }
        return text, usage, 0.0, str(data.get("model") or self.model)
