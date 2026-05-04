"""Anthropic Claude provider for the LLM abstraction.

Used for the critical profile/keyword generation step where cross-model
consensus produces meaningfully better output than a single provider.

For all other stages (cascade scoring) we stay on Gemini for cost reasons.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional

from anthropic import AsyncAnthropic
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from backend.config import config
from backend.models import LLMCallLog
from backend.scoring import cost_tracker


class AnthropicClient:
    """Minimal Anthropic API client. Same surface as GeminiClient.complete()."""

    provider_name = "anthropic"

    def __init__(self, api_key: Optional[str] = None):
        # Proxy mode (production): if config.LLM_PROXY_URL is set, route
        # through the Worker. The Worker holds the real Anthropic key
        # server-side and substitutes it on each forwarded request.
        # Tester ships with no Anthropic key — it never touches their machine.
        proxy_url = (config.LLM_PROXY_URL or "").rstrip("/")
        if proxy_url:
            tester_uuid = (config.TESTER_UUID or "").strip() or "00000000-0000-0000-0000-000000000000"
            self._client = AsyncAnthropic(
                api_key="proxy-mode",  # placeholder — Worker substitutes its real key
                base_url=f"{proxy_url}/v1/llm/anthropic",
                default_headers={"X-Tester-UUID": tester_uuid},
            )
        else:
            api_key = api_key or config.ANTHROPIC_API_KEY
            if not api_key or api_key.startswith("PASTE_"):
                raise RuntimeError(
                    "ANTHROPIC_API_KEY not configured. Set it in .env, "
                    "or set LLM_PROXY_URL to route through a Worker."
                )
            self._client = AsyncAnthropic(api_key=api_key)
        self.current_run_id: Optional[str] = None
        self.current_license_key: Optional[str] = None
        self.current_stage: str = "misc"

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=20),
        retry=retry_if_exception(
            lambda e: any(s in str(e) for s in ("429", "500", "502", "503", "504", "overloaded"))
        ),
        reraise=True,
    )
    async def complete(
        self,
        *,
        model: str,
        system: Optional[str],
        user: str,
        max_output_tokens: int = 4096,
        temperature: float = 0.3,
        thinking_budget: Optional[int] = None,
        json_schema: Optional[dict] = None,
        # Unused — kept for API compatibility with GeminiClient
        cached_content: Optional[Any] = None,
        is_batch: bool = False,
    ) -> "_AnthropicResponse":
        start = time.perf_counter()

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_output_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": user}],
        }
        if system:
            kwargs["system"] = system

        # Extended thinking — Anthropic's reasoning mode
        if thinking_budget and thinking_budget > 0:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": thinking_budget,
            }
            # Anthropic requires temperature=1 when thinking is enabled
            kwargs["temperature"] = 1.0

        try:
            response = await self._client.messages.create(**kwargs)
        except Exception as e:
            # Log the failed call so cost tracker shows attempts
            self._log_call(
                stage=self.current_stage,
                model=model,
                input_tokens=0,
                output_tokens=0,
                cost_usd=0.0,
                latency_ms=int((time.perf_counter() - start) * 1000),
                success=False,
                error_message=str(e)[:500],
            )
            raise

        latency_ms = int((time.perf_counter() - start) * 1000)

        # Extract text from content blocks (skip thinking blocks)
        text_parts: list[str] = []
        for block in response.content:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(block.text)
        text = "\n".join(text_parts).strip()

        # Token usage + cost
        usage = response.usage
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cost = self._calculate_cost(model, input_tokens, output_tokens)

        # Try to parse JSON if it looks like JSON output
        parsed_json: Optional[Any] = None
        if text:
            # Strip markdown code fences if present
            cleaned = text.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned.rsplit("```", 1)[0]
                cleaned = cleaned.strip()
                if cleaned.startswith("json\n"):
                    cleaned = cleaned[5:]
            try:
                parsed_json = json.loads(cleaned)
            except json.JSONDecodeError:
                pass

        # Log the call
        self._log_call(
            stage=self.current_stage,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            success=True,
        )

        return _AnthropicResponse(
            text=text,
            parsed_json=parsed_json,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            latency_ms=latency_ms,
            model=model,
            provider=self.provider_name,
        )

    def _calculate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        pricing = config.PRICING.get(model)
        if not pricing:
            return 0.0
        return round(
            (input_tokens * pricing["input"] / 1_000_000)
            + (output_tokens * pricing["output"] / 1_000_000),
            6,
        )

    def _log_call(
        self,
        *,
        stage: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: int,
        success: bool,
        error_message: Optional[str] = None,
    ) -> None:
        log_entry = LLMCallLog(
            run_id=self.current_run_id,
            license_key=self.current_license_key,
            stage=stage,
            provider=self.provider_name,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            success=success,
            error_message=error_message,
        )
        cost_tracker.log_call(log_entry)


class _AnthropicResponse:
    """Mirrors LLMResponse from llm_client.py — same fields the caller expects."""

    def __init__(
        self,
        *,
        text: str,
        parsed_json: Optional[Any],
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        latency_ms: int,
        model: str,
        provider: str,
    ):
        self.text = text
        self.parsed_json = parsed_json
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = 0
        self.cost_usd = cost_usd
        self.latency_ms = latency_ms
        self.model = model
        self.provider = provider
        self.is_batch = False


# Module-level singleton (lazy)
_default_client: Optional[AnthropicClient] = None


def get_anthropic_client() -> Optional[AnthropicClient]:
    """Return an AnthropicClient if ANTHROPIC_API_KEY is configured, else None."""
    global _default_client
    if _default_client is not None:
        return _default_client
    key = config.ANTHROPIC_API_KEY
    if not key or key.startswith("PASTE_"):
        return None
    try:
        _default_client = AnthropicClient()
        return _default_client
    except Exception:
        return None


def is_available() -> bool:
    """Check if cross-model consensus can run."""
    return get_anthropic_client() is not None
