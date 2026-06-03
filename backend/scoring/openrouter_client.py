"""OpenRouter implementation of LLMClient — cross-family Stage-3 scorer.

Why this exists (v0.3.25 scorer migration):
  - Gemini 2.5 Pro (the current Stage-3 deep-eval model) deprecates ~2026-06-17.
  - It also over-scores: bench showed Pro at 43% exact / OVER=15 vs the human key.
  - A cross-family model (default GPT-5-mini) beats it on accuracy AND
    over-scoring at ~7x lower cost, and generalises across industries.

This client talks to OpenRouter's OpenAI-compatible chat API and plugs into
the existing `LLMClient` interface, so scoring logic doesn't change. It is
selected only when `config.STAGE3_BACKEND == "openrouter"`; the default stays
"gemini" so production behaviour is unchanged until explicitly flipped.

Notes vs the Gemini client:
  - `create_cache()` returns None (no Gemini-style explicit cache); Stage 3
    falls back to an inline system prompt automatically.
  - `embed()` is intentionally unimplemented — embeddings stay on Gemini.
  - Gemini-specific kwargs (`cached_content`, `thinking_budget`,
    `json_schema` dialect) are accepted for interface-compatibility but mapped
    to OpenRouter equivalents (json_object output + reasoning effort).
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
from typing import Any, Optional

from backend.config import config
from backend.models import LLMCallLog
from backend.scoring import cost_tracker
from backend.scoring.llm_client import LLMClient, LLMResponse, calculate_cost


class OpenRouterClient(LLMClient):
    """OpenRouter (OpenAI-compatible) implementation of LLMClient."""

    provider_name = "openrouter"

    def __init__(self, api_key: Optional[str] = None):
        # v0.3.25: in PROXY mode (tester builds — LLM_PROXY_URL set) route through
        # the Cloudflare Worker, which holds the OpenRouter key as a secret and
        # gates on X-Tester-UUID — so testers need no key of their own. In LOCAL
        # dev (no proxy) call OpenRouter directly with OPENROUTER_API_KEY.
        self._proxy = (config.LLM_PROXY_URL or "").strip().rstrip("/")
        self._tester_uuid = (config.TESTER_UUID or "").strip()
        if self._proxy:
            self._base = self._proxy + "/v1/llm/openrouter"   # Worker strips this prefix
            self._api_key = ""
        else:
            self._api_key = (api_key or config.OPENROUTER_API_KEY or "").strip()
            self._base = (config.OPENROUTER_BASE_URL or "https://openrouter.ai/api/v1").rstrip("/")
            if not self._api_key:
                raise RuntimeError(
                    "OPENROUTER_API_KEY not configured. Set it in .env (local dev), "
                    "or set LLM_PROXY_URL to route through the Worker."
                )
        # Cost-tracking context — set by the orchestrator/stage caller, mirrors
        # the GeminiClient attributes so cost_tracker logging works identically.
        self.current_run_id: Optional[str] = None
        self.current_license_key: Optional[str] = None
        self.current_stage: str = "stage3"
        self._call_log: list[LLMCallLog] = []

    async def complete(
        self,
        *,
        model: str,
        system: Optional[str],
        user: str,
        max_output_tokens: int = 1024,
        temperature: float = 0.2,
        json_schema: Optional[dict] = None,
        cached_content: Optional[Any] = None,   # ignored (no Gemini cache)
        is_batch: bool = False,
        thinking_budget: Optional[int] = None,   # ignored (mapped to reasoning effort)
    ) -> LLMResponse:
        start = time.perf_counter()

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            # GPT-5-mini etc. spend tokens on reasoning before the JSON; give
            # generous headroom so the structured output is never truncated.
            "max_tokens": max(max_output_tokens, 5000),
            # Cap reasoning so cost stays low and the JSON isn't crowded out.
            "reasoning": {"effort": "low"},
        }
        if json_schema:
            # OpenRouter accepts OpenAI-style json_object; the Stage-3 system
            # prompt already enumerates the required fields, so the model emits
            # them and _score_one parses via data.get(...).
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Content-Type": "application/json",
            # v0.3.27 FIX: Cloudflare's WAF on the Worker domain bans the default
            # urllib User-Agent ("Python-urllib/x") with a 1010 "browser signature
            # banned" 403 — which silently killed EVERY Stage-3 call in proxy mode
            # (2/2 tester runs: 0/332 succeeded). Local/direct OpenRouter has no
            # such WAF, so Stage 3 worked there and masked the bug. Send a normal
            # browser UA so proxied Stage-3 calls aren't bot-blocked.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        if self._proxy:
            # Worker injects the OpenRouter key + referer; authenticate as a tester.
            headers["X-Tester-UUID"] = self._tester_uuid
        else:
            headers["Authorization"] = "Bearer " + self._api_key
            headers["HTTP-Referer"] = "https://localhost"
            headers["X-Title"] = "job-search-stage3"

        def _call() -> dict:
            req = urllib.request.Request(
                self._base + "/chat/completions",
                data=json.dumps(body).encode(),
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.loads(r.read().decode())

        max_attempts = 4
        last_exc: Optional[Exception] = None
        data: Optional[dict] = None
        for attempt in range(max_attempts):
            attempt_start = time.perf_counter()
            try:
                data = await asyncio.to_thread(_call)
                break
            except Exception as e:
                last_exc = e
                code = getattr(e, "code", None)
                retryable = code in (429, 500, 502, 503, 504)
                self._log(
                    model, 0, 0, 0.0,
                    int((time.perf_counter() - attempt_start) * 1000),
                    success=False,
                    error=f"attempt={attempt + 1} code={code} {repr(e)[:180]}",
                )
                if not retryable or attempt >= max_attempts - 1:
                    raise
                await asyncio.sleep(min(30, 5 * (2 ** attempt)))

        if data is None:
            if last_exc is not None:
                raise last_exc
            raise RuntimeError("OpenRouter call exhausted retries without response")

        latency_ms = int((time.perf_counter() - start) * 1000)
        choice = (data.get("choices") or [{}])[0]
        text = ((choice.get("message") or {}).get("content")) or ""
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens", 0) or 0)
        output_tokens = int(usage.get("completion_tokens", 0) or 0)

        parsed_json: Optional[Any] = None
        if text:
            try:
                parsed_json = json.loads(text)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", text, re.S)
                if m:
                    try:
                        parsed_json = json.loads(m.group(0))
                    except Exception:
                        parsed_json = None

        cost = calculate_cost(model=model, input_tokens=input_tokens, output_tokens=output_tokens)
        self._log(model, input_tokens, output_tokens, cost, latency_ms, success=True, error=None)

        return LLMResponse(
            text=text,
            parsed_json=parsed_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            model=model,
            provider=self.provider_name,
            is_batch=is_batch,
        )

    def _log(self, model, it, ot, cost, latency, *, success, error):
        try:
            entry = LLMCallLog(
                run_id=self.current_run_id,
                license_key=self.current_license_key,
                stage=self.current_stage if success else f"{self.current_stage}_retry",
                provider=self.provider_name,
                model=model,
                is_batch=False,
                input_tokens=it,
                output_tokens=ot,
                cost_usd=cost,
                latency_ms=latency,
                success=success,
                error_message=error,
            )
            self._call_log.append(entry)
            cost_tracker.log_call(entry)
        except Exception:
            pass  # cost logging must never break scoring

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError(
            "OpenRouterClient does not implement embeddings — the embedding "
            "pre-filter stays on Gemini (gemini-embedding-001)."
        )

    async def create_cache(self, *, model: str, system: str, ttl_seconds: int = 3600) -> Any:
        # No OpenRouter equivalent of Gemini's explicit context cache handle.
        # Returning None makes Stage 3 fall back to an inline system prompt.
        return None
