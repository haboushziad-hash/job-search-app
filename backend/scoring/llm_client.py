"""Provider-agnostic LLM client abstraction.

Why this exists: the scoring stages should not care which provider serves
the model. This abstraction lets us swap Gemini for Claude/OpenAI/etc.
without touching scoring logic, and routes every call through a single
cost-tracking pipeline.

Today this file ships a `GeminiClient`. Future: `CloudflareAIClient`,
`AnthropicClient`, etc., behind the same `LLMClient` interface.
"""
from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from google import genai
from google.genai import types as genai_types
from tenacity import (
    retry, stop_after_attempt, wait_exponential, retry_if_exception_type,
    retry_if_exception, before_sleep_log,
)

from backend.config import config
from backend.models import LLMCallLog
from backend.scoring import cost_tracker


# ----------------------------------------------------------------------------
# Result types
# ----------------------------------------------------------------------------

@dataclass
class LLMResponse:
    """Normalized response from any provider."""
    text: str
    parsed_json: Optional[Any] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    model: str = ""
    provider: str = ""
    is_batch: bool = False


# ----------------------------------------------------------------------------
# Cost calculator — single source of truth for "what did this call cost?"
# ----------------------------------------------------------------------------

def calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int = 0,
    is_batch: bool = False,
) -> float:
    """Return USD cost for a single LLM call.

    Cached tokens get a 75% discount (charged at 25% of input rate).
    Batch calls get 50% off both input and output.
    """
    pricing = config.PRICING.get(model)
    if not pricing:
        return 0.0

    if is_batch:
        in_rate = pricing.get("batch_input", pricing["input"] / 2)
        out_rate = pricing.get("batch_output", pricing["output"] / 2)
    else:
        in_rate = pricing["input"]
        out_rate = pricing["output"]

    # Cached tokens charged at 25% of input rate (Gemini context cache)
    fresh_input = max(0, input_tokens - cached_tokens)
    cost = (fresh_input * in_rate / 1_000_000) + (cached_tokens * in_rate * 0.25 / 1_000_000)
    cost += output_tokens * out_rate / 1_000_000
    return round(cost, 6)


# ----------------------------------------------------------------------------
# Abstract interface
# ----------------------------------------------------------------------------

class LLMClient(ABC):
    """Provider-agnostic LLM interface."""

    provider_name: str = "abstract"

    @abstractmethod
    async def complete(
        self,
        *,
        model: str,
        system: Optional[str],
        user: str,
        max_output_tokens: int = 1024,
        temperature: float = 0.2,
        json_schema: Optional[dict] = None,
        cached_content: Optional[Any] = None,
        is_batch: bool = False,
        thinking_budget: Optional[int] = None,
    ) -> LLMResponse:
        """Single-turn completion with optional structured output.

        thinking_budget: For Gemini 2.5 thinking models, set to 0 to disable
        chain-of-thought (cheapest), or a positive integer to cap thought
        tokens. None = model default (full thinking enabled).
        """
        ...

    @abstractmethod
    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
        ...

    @abstractmethod
    async def create_cache(
        self,
        *,
        model: str,
        system: str,
        ttl_seconds: int = 3600,
    ) -> Any:
        """Create a context cache for repeated system prompts. Returns a cache handle."""
        ...


# ----------------------------------------------------------------------------
# Gemini implementation
# ----------------------------------------------------------------------------

class GeminiClient(LLMClient):
    """Google Gemini implementation of LLMClient."""

    provider_name = "google"

    def __init__(self, api_key: Optional[str] = None):
        # Three modes of operation:
        #
        # 1) Proxy mode (production / tester builds): if config.LLM_PROXY_URL
        #    is set, route ALL requests through that URL. The Worker holds
        #    real keys server-side, swaps them in, and forwards to Google.
        #    Tester ships with no keys — they're never on the tester's machine.
        #
        # 2) Multi-key direct mode (local dev): if .env has GOOGLE_API_KEY(s),
        #    use them directly. Multi-key rotation kicks in when one hits
        #    its daily Pro quota. Each key is a separate Google Cloud project
        #    with its own 1000/day Pro quota, all billed to the same account.
        #
        # 3) Single explicit key mode (callers passing api_key=...).
        proxy_url = (config.LLM_PROXY_URL or "").rstrip("/")
        if proxy_url:
            # Proxy mode — SDK uses our Worker as its base URL.
            # api_key here is a placeholder; the Worker substitutes its real key.
            # We keep TESTER_UUID in the X-Tester-UUID header so the Worker can
            # rate-limit per tester (cap.checkAndIncrementCallCap on the Worker).
            tester_uuid = (config.TESTER_UUID or "").strip() or "00000000-0000-0000-0000-000000000000"
            self._api_keys = ["proxy-mode"]  # placeholder — Worker substitutes
            self._key_index = 0
            self._exhausted_keys = set()
            self._client = genai.Client(
                api_key="proxy-mode",
                http_options=genai_types.HttpOptions(
                    base_url=f"{proxy_url}/v1/llm/gemini",
                    headers={"X-Tester-UUID": tester_uuid},
                ),
            )
        else:
            # Direct mode — use the configured keys.
            if api_key:
                keys = [api_key]
            else:
                keys = config.google_api_keys()
            if not keys:
                raise RuntimeError(
                    "GOOGLE_API_KEY not configured. Set it in .env, "
                    "or set LLM_PROXY_URL to route through a Worker."
                )
            self._api_keys: list[str] = keys
            self._key_index: int = 0
            self._exhausted_keys: set[int] = set()
            self._client = genai.Client(api_key=keys[0])
        self._call_log: list[LLMCallLog] = []
        # Per-call metadata that callers can set before invoking
        # (used so the cost log records run_id, license_key, stage)
        self.current_run_id: Optional[str] = None
        self.current_license_key: Optional[str] = None
        self.current_stage: str = "misc"

    def _rotate_to_next_key(self) -> bool:
        """Mark current key as exhausted and switch to the next available one.
        Returns False if no more keys available — caller should propagate the error.

        Proxy mode: the Worker handles key rotation server-side. Local
        rotation is a no-op (we only have a single placeholder "key").
        """
        if (config.LLM_PROXY_URL or "").strip():
            # Proxy mode — Worker rotates between its own keys. Nothing to
            # do here, just report that we can't help.
            return False
        self._exhausted_keys.add(self._key_index)
        for offset in range(1, len(self._api_keys) + 1):
            candidate = (self._key_index + offset) % len(self._api_keys)
            if candidate not in self._exhausted_keys:
                self._key_index = candidate
                self._client = genai.Client(api_key=self._api_keys[candidate])
                return True
        return False  # all keys exhausted

    @property
    def num_keys(self) -> int:
        return len(self._api_keys)

    # ---- core completion ----

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        reraise=True,
        retry=retry_if_exception(
            lambda e: (
                # Retry on transient Gemini errors: 429, 500, 502, 503, 504
                # Or any exception with "503", "429", "UNAVAILABLE", "quota" in str
                any(s in str(e) for s in ("503", "429", "500", "502", "504", "UNAVAILABLE", "quota", "RESOURCE_EXHAUSTED"))
            )
        ),
    )
    async def complete(
        self,
        *,
        model: str,
        system: Optional[str],
        user: str,
        max_output_tokens: int = 1024,
        temperature: float = 0.2,
        json_schema: Optional[dict] = None,
        cached_content: Optional[Any] = None,
        is_batch: bool = False,
        thinking_budget: Optional[int] = None,
    ) -> LLMResponse:
        start = time.perf_counter()

        gen_config: dict[str, Any] = {
            "max_output_tokens": max_output_tokens,
            "temperature": temperature,
        }
        if json_schema:
            gen_config["response_mime_type"] = "application/json"
            gen_config["response_schema"] = json_schema
        if system and not cached_content:
            gen_config["system_instruction"] = system
        if cached_content is not None:
            # google-genai SDK expects `cached_content` to be the cache's
            # resource-name string (e.g. "cachedContents/abc123"), NOT the
            # CachedContent object that caches.create() returns.
            # v0.3.5 ship-night live test confirmed: passing the object
            # raises Pydantic ValidationError on GenerateContentConfig.
            # Defensive: accept either form (str passed-through; object
            # → extract .name attribute).
            if isinstance(cached_content, str):
                gen_config["cached_content"] = cached_content
            elif hasattr(cached_content, "name"):
                gen_config["cached_content"] = cached_content.name
            else:
                # Unknown shape — log + fall back to inline system prompt
                # rather than risk the Pydantic crash.
                if system:
                    gen_config["system_instruction"] = system
        # Gemini 2.5 thinking budget — set to 0 to disable thinking (cheapest)
        if thinking_budget is not None:
            gen_config["thinking_config"] = genai_types.ThinkingConfig(
                thinking_budget=thinking_budget,
            )

        # google-genai SDK is sync under the hood for now; run in a thread
        def _call() -> Any:
            return self._client.models.generate_content(
                model=model,
                contents=user,
                config=genai_types.GenerateContentConfig(**gen_config),
            )

        # Try the call. If we hit per-day quota, rotate to next key and retry.
        # (Per-minute rate limits are handled by the @retry decorator above —
        # those retry on the same key. Per-day quotas need a different key.)
        try:
            response = await asyncio.to_thread(_call)
        except Exception as e:
            err_str = str(e)
            is_daily_quota = (
                "RESOURCE_EXHAUSTED" in err_str
                and "per_day" in err_str.lower()
            )
            if is_daily_quota and self.num_keys > 1:
                if self._rotate_to_next_key():
                    # Retry with the new key
                    response = await asyncio.to_thread(_call)
                else:
                    raise  # all keys exhausted
            else:
                raise
        latency_ms = int((time.perf_counter() - start) * 1000)

        # Extract usage (None-safe — Gemini returns None for unused fields)
        usage = getattr(response, "usage_metadata", None)
        input_tokens = (getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
        output_tokens = (getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
        cached_tokens = (getattr(usage, "cached_content_token_count", 0) or 0) if usage else 0

        # Extract text (None-safe — Gemini may return None when output budget exceeded)
        text = (response.text if hasattr(response, "text") else "") or ""

        # Parse JSON if requested
        parsed_json: Optional[Any] = None
        if json_schema and text:
            try:
                parsed_json = json.loads(text)
            except json.JSONDecodeError:
                parsed_json = None

        cost = calculate_cost(
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            is_batch=is_batch,
        )

        # Log the call (in-memory + persistent cost log)
        log_entry = LLMCallLog(
            run_id=self.current_run_id,
            license_key=self.current_license_key,
            stage=self.current_stage,
            provider=self.provider_name,
            model=model,
            is_batch=is_batch,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            success=True,
        )
        # Attach cached_tokens to log entry (model allows extra)
        log_entry_dict = log_entry.model_dump()
        log_entry_dict["cached_tokens"] = cached_tokens
        # Re-create with cached_tokens preserved
        try:
            log_entry.cached_tokens = cached_tokens  # type: ignore[attr-defined]
        except Exception:
            pass
        self._call_log.append(log_entry)
        cost_tracker.log_call(log_entry)

        return LLMResponse(
            text=text,
            parsed_json=parsed_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            model=model,
            provider=self.provider_name,
            is_batch=is_batch,
        )

    # ---- embeddings ----

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        start = time.perf_counter()

        def _call() -> Any:
            return self._client.models.embed_content(
                model=model,
                contents=texts,
            )

        result = await asyncio.to_thread(_call)
        latency_ms = int((time.perf_counter() - start) * 1000)

        embeddings_attr = getattr(result, "embeddings", [])
        embeddings = [list(e.values) for e in embeddings_attr]

        # Charge embedding cost: text-embedding-004 priced per 1K input chars
        total_chars = sum(len(t) for t in texts)
        # Pricing: $0.0001 per 1K chars input
        cost = total_chars * 0.0001 / 1000

        log_entry = LLMCallLog(
            run_id=self.current_run_id,
            license_key=self.current_license_key,
            stage="embedding",
            provider=self.provider_name,
            model=model,
            input_tokens=total_chars,  # chars, not tokens — embedding model bills on chars
            output_tokens=0,
            cost_usd=cost,
            latency_ms=latency_ms,
            success=True,
        )
        self._call_log.append(log_entry)
        cost_tracker.log_call(log_entry)

        return embeddings

    # ---- context cache ----

    async def create_cache(
        self,
        *,
        model: str,
        system: str,
        ttl_seconds: int = 3600,
    ) -> Any:
        """Create a Gemini context cache. Returns the cache handle (string name).

        Caches must be at least ~32K tokens to be useful with Gemini Pro;
        for smaller system prompts the SDK may decline. We catch the error
        and fall back to inline system prompt at the call site.
        """
        def _call() -> Any:
            return self._client.caches.create(
                model=model,
                config=genai_types.CreateCachedContentConfig(
                    system_instruction=system,
                    ttl=f"{ttl_seconds}s",
                ),
            )

        try:
            return await asyncio.to_thread(_call)
        except Exception as e:
            # Cache creation can fail on prompts under the minimum token threshold.
            # Caller should treat None as "no cache available, use inline system prompt".
            return None

    # ---- log access ----

    def drain_call_log(self) -> list[LLMCallLog]:
        """Return and clear the in-memory call log."""
        log, self._call_log = self._call_log, []
        return log


# ----------------------------------------------------------------------------
# Module-level singleton (lazy-init)
# ----------------------------------------------------------------------------

_default_client: Optional[GeminiClient] = None


def get_llm_client() -> LLMClient:
    """Return the configured default LLM client (Gemini for now)."""
    global _default_client
    if _default_client is None:
        _default_client = GeminiClient()
    return _default_client
