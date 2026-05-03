r"""Smoke test — verifies imports work and Gemini auth is alive.

Run from project root:
  backend\venv\Scripts\python.exe -m backend.tests.smoke_test
"""
from __future__ import annotations

import asyncio
import sys

from backend.config import config
from backend.scoring import cost_tracker
from backend.scoring.llm_client import GeminiClient, calculate_cost


async def test_completion() -> None:
    """One real Gemini call to verify auth + plumbing."""
    print("Running smoke test against Gemini...")
    client = GeminiClient()
    client.current_run_id = "smoke_test"
    client.current_stage = "misc"

    response = await client.complete(
        model=config.STAGE1_MODEL,
        system="You are a JSON-only assistant. Respond with exactly: {\"ok\": true}",
        user="Reply now.",
        max_output_tokens=64,
        temperature=0.0,
        thinking_budget=0,  # Disable thinking for cheapest possible Stage 1 call
    )

    print(f"  Model:          {response.model}")
    print(f"  Provider:       {response.provider}")
    print(f"  Latency:        {response.latency_ms} ms")
    print(f"  Input tokens:   {response.input_tokens}")
    print(f"  Output tokens:  {response.output_tokens}")
    print(f"  Cost:           ${response.cost_usd:.6f}")
    print(f"  Response text:  {response.text[:100]!r}")


async def test_embedding() -> None:
    print("\nTesting embedding...")
    client = GeminiClient()
    client.current_run_id = "smoke_test"

    embeddings = await client.embed(
        model=config.EMBEDDING_MODEL,
        texts=["AI strategy consultant role", "Software engineer ML platform"],
    )
    print(f"  Got {len(embeddings)} embeddings, dim={len(embeddings[0]) if embeddings else 0}")


def test_cost_calc() -> None:
    print("\nTesting cost calculator...")
    # Stage 2 example: 2K input + 200 output, batch
    cost = calculate_cost("gemini-2.5-pro", 2000, 200, is_batch=True)
    expected = (2000 * 0.625 / 1_000_000) + (200 * 2.50 / 1_000_000)
    print(f"  Stage 2 batch (2K in, 200 out): ${cost:.6f}  (expected ~${expected:.6f})")
    assert abs(cost - expected) < 1e-9, "cost calc mismatch"
    print("  OK")


def test_cost_tracker_summary() -> None:
    print("\nCost log summary:")
    print(f"  Total today:       ${cost_tracker.cost_today():.6f}")
    print(f"  Total this month:  ${cost_tracker.cost_this_month():.6f}")
    runs = cost_tracker.recent_runs(5)
    print(f"  Recent runs:       {len(runs)}")
    for r in runs:
        print(f"    {r['run_id']}: status={r['status']}, cost=${r.get('cost_total_usd', 0):.6f}")


async def main() -> int:
    errors = config.validate()
    if errors:
        print("Config errors:")
        for e in errors:
            print(f"  - {e}")
        return 1

    test_cost_calc()
    await test_completion()
    await test_embedding()
    test_cost_tracker_summary()

    print("\nSmoke test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
