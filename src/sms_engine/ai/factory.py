"""Build an `LLMProvider` from environment variables.

    SMS_ENGINE_PROVIDER     mock | openai | openrouter | local | anthropic   (default: mock)
    SMS_ENGINE_MODEL        model id (default depends on the provider, see DEFAULT_MODELS)
    SMS_ENGINE_BASE_URL     override the API base URL (e.g. a vLLM / LM Studio server)
    SMS_ENGINE_API_KEY      API key; falls back to OPENAI_API_KEY / OPENROUTER_API_KEY /
                            ANTHROPIC_API_KEY for the matching provider
    SMS_ENGINE_LLM_TIMEOUT  per-request timeout in seconds (default 20; local: 60)
    SMS_ENGINE_LLM_RETRIES  retries on 429/5xx/network errors (default 2)

The default is the deterministic MockProvider, so nothing reaches the network
unless an operator opts in explicitly.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from sms_engine.ai.mock_provider import MockProvider
from sms_engine.ai.provider import LLMProvider

DEFAULT_MODELS = {
    "openai": "gpt-4.1-mini",
    "openrouter": "anthropic/claude-haiku-5.5",
    "local": "llama3.1:8b",
    "anthropic": "claude-haiku-5-5",
}

_KEY_FALLBACK = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

PROVIDER_NAMES = ("mock", *DEFAULT_MODELS)


def provider_from_env(env: Mapping[str, str] | None = None, **overrides) -> LLMProvider:
    """Return the provider selected by `env` (defaults to `os.environ`).

    Extra keyword arguments (e.g. `transport=` in tests) are passed to the
    provider constructor.
    """
    env = os.environ if env is None else env
    kind = (env.get("SMS_ENGINE_PROVIDER") or "mock").strip().lower()
    if kind in ("", "mock"):
        return MockProvider()
    if kind not in DEFAULT_MODELS:
        raise ValueError(f"unknown SMS_ENGINE_PROVIDER {kind!r}; expected one of {', '.join(PROVIDER_NAMES)}")

    from sms_engine.ai import llm

    kw: dict = {"model": env.get("SMS_ENGINE_MODEL") or DEFAULT_MODELS[kind]}
    if env.get("SMS_ENGINE_BASE_URL"):
        kw["base_url"] = env["SMS_ENGINE_BASE_URL"]
    key = env.get("SMS_ENGINE_API_KEY") or (env.get(_KEY_FALLBACK[kind]) if kind in _KEY_FALLBACK else None)
    if key:
        kw["api_key"] = key
    if env.get("SMS_ENGINE_LLM_TIMEOUT"):
        kw["timeout"] = float(env["SMS_ENGINE_LLM_TIMEOUT"])
    if env.get("SMS_ENGINE_LLM_RETRIES"):
        kw["max_retries"] = int(env["SMS_ENGINE_LLM_RETRIES"])
    kw.update(overrides)

    cls = {
        "openai": llm.OpenAIProvider,
        "openrouter": llm.OpenRouterProvider,
        "local": llm.LocalProvider,
        "anthropic": llm.AnthropicProvider,
    }[kind]
    return cls(**kw)
