"""Cross-family STACK Stage-3 scorer (v0.3.25, de-Gemini'd).

Bench (Ziad human key, +guards): single GPT-5-mini 46% -> stack-3 54% ->
stack-4 (+Selene) 57% exact / 100% within-1, matching the old Gemini-decomp
stack but fully Gemini-free.

How it works: each member model scores the role with ITS winning prompt; the
numeric scores are AVERAGED into the final stage-3 score (then the orchestrator's
guards + score<->tier reconciliation apply as usual). GPT-5-mini's reasoning is
carried through for the dashboard (match_analysis / application_strategy / summary).

Members (config.STAGE3_STACK_MODELS, default "gpt5mini,deepseek,qwen"):
  - gpt5mini  openai/gpt-5-mini      (ref-anchored, rich output)  via OpenRouter
  - deepseek  deepseek/deepseek-chat (balanced)                   via OpenRouter
  - qwen      qwen/qwen3.6-flash     (gate-first)                 via OpenRouter
  - selene    atla/selene-mini       (loosened 1-5)               via local Ollama
Add "selene" to STAGE3_STACK_MODELS for the local-best cheap4 variant.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.request
from typing import Any, Optional

from backend.config import config
from backend.scoring.llm_client import LLMClient, LLMResponse

# ---- shared calibration blocks (profile-driven; profile arrives in the user turn) ----
_TIERS = "Tiers by 0-100 score: STRONG>=85, GOOD 70-84, MAYBE 55-69, STRETCH 40-54, SKIP<40."
_DISQ = ("DISQUALIFIER RULE: infer the candidate's TARGET FUNCTIONS from the profile. If the role's CORE "
         "duties fall clearly OUTSIDE those target functions (e.g. hands-on software/ML engineering or "
         "sales/GTM/quota work when those are not targets), cap below 40 (SKIP) no matter how senior or well-paid.")
_CALIB = ("CALIBRATION — be decisive and use the FULL range; do NOT cluster borderline roles at the 55-60 floor. "
          "Gap handling: if the role's CORE duties map to the candidate's target functions at attainable seniority, "
          "it is a GOOD (70-84) AT MINIMUM even with SECONDARY gaps (industry/domain inexperience, a missing "
          "nice-to-have skill, unproven scale) — a secondary gap is a small deduction WITHIN GOOD, never a drop to "
          "MAYBE. Reserve MAYBE (55-69) for genuine doubt about the CORE-function fit (core duties only PARTIALLY "
          "overlap the target functions); reserve STRETCH/SKIP (<55) for core duties largely outside them. "
          "CONSISTENCY LOCK: if your reasoning says the role 'maps closely', 'aligns well', or 'is a strong match' "
          "for the target functions, your score MUST be >=78 — never write positive-fit language and then score 55-69.")

_GPT5_SYS = ("You are a STRICT but FAIR candidate-to-job FIT judge. Score on this anchor ladder and use its full height: "
    "ANCHOR~96 (STRONG, near-ideal)=core duties match the candidate's target functions closely at ideal seniority; "
    "ANCHOR~86 (STRONG)=clear match with only minor/secondary gaps; "
    "ANCHOR~74 (GOOD)=core duties map to target functions but with a notable secondary gap; "
    "ANCHOR~45 (STRETCH)=core duties only partially overlap; "
    "ANCHOR~20 (SKIP)=core duties clearly outside the target functions. Place THIS role on the ladder. "
    + _DISQ + " " + _TIERS + " " + _CALIB +
    '\n\nReturn ONLY JSON: {"match_analysis":"<2-4 sentences: core duties vs target functions, then the biggest gap>",'
    '"application_strategy":"<1-2 sentences>","best_resume_match":"<short or empty>",'
    '"summary":"<2 sentences for a dashboard card>","score":<0-100 integer>}.')
_BALANCED_SYS = ("You are a fair candidate-to-job FIT judge. " + _DISQ + " " + _TIERS + " " + _CALIB +
    " Use the top of the range for the best roles: a near-ideal match (core duties closely match target functions at "
    "ideal seniority) scores 92-97 and a clear match 85-91 — do not compress all strong roles to 88-90." +
    '\n\nReason briefly, then return ONLY JSON: {"reasoning":"<=40 words","score":<0-100 integer>}.')
_GATE_SYS = ("You are a candidate-to-job FIT judge using TWO STEPS. STEP 1 GATE: list the role's 3-5 CORE duties; "
    "if most fall OUTSIDE the candidate's target functions, cap below 40 (SKIP). STEP 2: otherwise score on "
    "target-function overlap + attainable seniority. " + _TIERS + " " + _CALIB +
    '\n\nReturn ONLY JSON: {"reasoning":"<=40 words","score":<0-100 integer>}.')
_SELENE_PROMPT_HEAD = ("You are evaluating how well a job CANDIDATE fits a JOB. Write brief reasoning, then an "
    "integer 1-5 on its own line. Format EXACTLY:\n**Reasoning:** <text>\n**Result:** <1-5>\n\nInstruction:\n"
    "Judge how well the CANDIDATE fits the JOB. Infer the candidate's target functions from the profile; "
    "disqualify roles whose core duties fall clearly outside them. Use the full 1-5 range.\n\n")
_SELENE_RUBRIC = ("\n\nScore Rubric:\nScore 5: Excellent - core duties match target functions, seniority attainable.\n"
    "Score 4: Good - clear overlap, minor gaps.\nScore 3: Partial - some overlap, notable gaps.\n"
    "Score 2: Weak - little overlap or hard requirement unmet.\nScore 1: Off-target - core duties outside target functions.")
_SELENE_1_5_TO_SCORE = {5: 95, 4: 80, 3: 62, 2: 46, 1: 30}

# member registry: key -> (kind, slug, system_prompt)
_MEMBERS = {
    "gpt5mini": ("api", "openai/gpt-5-mini", _GPT5_SYS),
    "deepseek": ("api", "deepseek/deepseek-chat", _BALANCED_SYS),
    "qwen":     ("api", "qwen/qwen3.6-flash", _GATE_SYS),
    "selene":   ("selene", "atla/selene-mini", None),
}

# v0.3.25 member weights for the blended Stage-3 score. deepseek is the STABLE
# anchor (low run-to-run variance); gpt5mini is DE-WEIGHTED because its variance
# is high (~33% tier-flips, e.g. the same role swinging 89->62 between runs).
# Unlisted members get _DEFAULT_WEIGHT. Weights are normalized by their sum, so
# any subset/superset of members still yields a proper weighted mean.
# 70/30 deep/gpt chosen via a weight sweep vs 30 human labels (real votes, 3
# runs): best exact (62%), lowest over-scoring (0.7), fewest tier-flips (19%),
# best STRONG recall (89%) — beat 65/35, 60/40, 75/25, and equal-weight on every
# axis. (75/25 falls off, so this is a true optimum, not "more deepseek=better".)
_DEFAULT_WEIGHT = 0.5
_MEMBER_WEIGHTS = {"deepseek": 0.70, "gpt5mini": 0.30}


def _parse_score(txt: str) -> Optional[int]:
    txt = re.sub(r"<think>.*?</think>", "", txt or "", flags=re.S)
    m = re.search(r"\{.*\}", txt, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if d.get("score") is not None:
                return int(d["score"])
        except Exception:
            pass
    return None


class StackClient(LLMClient):
    """Averages several cross-family judges into one Stage-3 score."""

    provider_name = "stack"

    def __init__(self):
        from backend.scoring.openrouter_client import OpenRouterClient
        self._or = OpenRouterClient()  # reuses OPENROUTER_API_KEY + per-call cost logging
        wanted = [m.strip() for m in (config.STAGE3_STACK_MODELS or "gpt5mini,deepseek,qwen").split(",") if m.strip()]
        self._members = [(k, *_MEMBERS[k]) for k in wanted if k in _MEMBERS]
        self.current_run_id: Optional[str] = None
        self.current_license_key: Optional[str] = None
        self.current_stage: str = "stage3"
        self._call_log: list = []

    def _sync_ctx(self):
        # propagate cost-tracking context to the reused OpenRouter client
        self._or.current_run_id = self.current_run_id
        self._or.current_license_key = self.current_license_key
        self._or.current_stage = self.current_stage

    async def _selene(self, user: str) -> Optional[int]:
        prompt = _SELENE_PROMPT_HEAD + user + _SELENE_RUBRIC + "\n\n"
        body = json.dumps({"model": "atla/selene-mini", "prompt": prompt, "stream": False,
                           "options": {"temperature": 0, "num_predict": 350, "num_ctx": 8192}}).encode()
        def _call():
            rq = urllib.request.Request("http://localhost:11434/api/generate", data=body,
                                        headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(rq, timeout=240) as r:
                return json.loads(r.read().decode()).get("response", "")
        try:
            txt = await asyncio.to_thread(_call)
            m = re.search(r"\*\*Result:\*\*\s*([1-5])", txt) or re.search(r"\b([1-5])\b\s*$", txt.strip())
            return _SELENE_1_5_TO_SCORE.get(int(m.group(1))) if m else None
        except Exception:
            return None

    async def complete(self, *, model: str, system: Optional[str], user: str,
                       max_output_tokens: int = 4096, temperature: float = 0.0,
                       json_schema: Optional[dict] = None, cached_content: Optional[Any] = None,
                       is_batch: bool = False, thinking_budget: Optional[int] = None) -> LLMResponse:
        start = time.perf_counter()
        self._sync_ctx()
        scores: list[int] = []
        rich: Optional[dict] = None
        used = []
        # v0.3.38: score all members CONCURRENTLY. Was a sequential await-loop
        # (per-role latency = SUM of member latencies); now max() instead, ~3x
        # faster Stage-3 with IDENTICAL results — members are independent and
        # their scores are averaged. Results are collected in member order so
        # the weighted blend + gpt5mini rich output stay deterministic.
        async def _score_member(key, kind, slug, sys_prompt):
            try:
                if kind == "selene":
                    return (key, await self._selene(user), None)
                resp = await self._or.complete(model=slug, system=sys_prompt, user=user,
                                               max_output_tokens=4000, temperature=0.0,
                                               json_schema={"type": "object"})
                rich_j = resp.parsed_json if (key == "gpt5mini" and isinstance(resp.parsed_json, dict)) else None
                return (key, _parse_score(resp.text), rich_j)
            except Exception:
                return (key, None, None)

        member_results = await asyncio.gather(
            *(_score_member(*m) for m in self._members)
        )
        for key, sc, rich_j in member_results:
            if rich_j is not None:
                rich = rich_j
            if sc is not None:
                scores.append(sc); used.append(key)
        latency_ms = int((time.perf_counter() - start) * 1000)
        if not scores:
            return LLMResponse(text="", parsed_json=None, model="stack", provider=self.provider_name,
                               latency_ms=latency_ms)
        # v0.3.25 deepseek-weighted blend (see _MEMBER_WEIGHTS): de-weight noisy
        # gpt5mini relative to the stable deepseek anchor.
        weights = [_MEMBER_WEIGHTS.get(k, _DEFAULT_WEIGHT) for k in used]
        wsum = sum(weights) or float(len(scores))
        m = sum(w * s for w, s in zip(weights, scores)) / wsum
        # ceiling fix: when ALL members agree the role is strong (each member
        # >= 78, i.e. GOOD+), lean toward the top score instead of diluting it.
        # Plain averaging buried genuinely-strong roles that one conservative
        # voter pulled below the STRONG line; this recovers them. Gated on
        # agreement — any member < 78 keeps the conservative (weighted) mean, so
        # a disputed role can never be inflated.
        if len(scores) >= 2 and min(scores) >= 78:
            avg = int(round(0.5 * m + 0.5 * max(scores)))
        else:
            avg = int(round(m))
        pj = dict(rich) if rich else {}
        pj["score"] = avg
        pj.setdefault("match_analysis", "[stack] averaged cross-family score from %d judges (%s)" % (len(scores), ",".join(used)))
        pj["stack_members"] = used
        pj["stack_scores"] = scores
        return LLMResponse(text=json.dumps(pj), parsed_json=pj, cost_usd=0.0, latency_ms=latency_ms,
                           model="stack:" + "+".join(used), provider=self.provider_name)

    async def embed(self, *, model: str, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError("StackClient does not implement embeddings.")

    async def create_cache(self, *, model: str, system: str, ttl_seconds: int = 3600) -> Any:
        return None
