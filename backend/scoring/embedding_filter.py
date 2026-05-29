"""Embedding-based pre-filter — kills the bottom 60% before any LLM call.

Mechanism:
  1. Embed the candidate profile once (one API call)
  2. Embed every scraped role's title + summary in batches
  3. Cosine similarity between profile and each role
  4. Keep top N% (default 40%, configurable)

Cost: ~$0.005 per 1,000 roles. Effectively free.
Quality: more accurate than Flash on this binary keep/reject task because
embeddings capture semantic similarity, not just lexical matches.
"""
from __future__ import annotations

import asyncio
from typing import Iterable

import numpy as np

from backend.config import config
from backend.models import CandidateProfile, Role
from backend.scoring.llm_client import LLMClient, get_llm_client


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine similarity between vector a and matrix b (one row per role)."""
    a_norm = a / (np.linalg.norm(a) + 1e-12)
    b_norms = np.linalg.norm(b, axis=1, keepdims=True) + 1e-12
    b_normalized = b / b_norms
    return b_normalized @ a_norm


def _profile_to_text(profile: CandidateProfile) -> str:
    """Render a candidate profile as a single text blob for embedding."""
    parts = []
    if profile.headline:
        parts.append(profile.headline)
    if profile.target_functions:
        parts.append("Target functions: " + ", ".join(profile.target_functions))
    if profile.target_industries:
        parts.append("Target industries: " + ", ".join(profile.target_industries))
    if profile.target_seniority:
        parts.append("Seniority: " + profile.target_seniority)
    if profile.technical_skills:
        parts.append("Technical skills: " + ", ".join(profile.technical_skills[:20]))
    if profile.domain_expertise:
        parts.append("Domain expertise: " + ", ".join(profile.domain_expertise[:20]))
    return ". ".join(parts) or "Candidate profile."


def _role_to_text(role: Role) -> str:
    """Render a role for embedding — title + company + first ~500 chars of JD."""
    parts = [f"{role.job_title} at {role.company}"]
    if role.seniority_level:
        parts.append(f"Seniority: {role.seniority_level}")
    if role.industry:
        parts.append(f"Industry: {role.industry}")
    jd = role.job_description_essence or role.job_description_full or ""
    if jd:
        parts.append(jd[:500])
    return ". ".join(parts)


async def filter_roles_by_embedding(
    *,
    profile: CandidateProfile,
    roles: list[Role],
    keep_fraction: float = 0.60,
    min_similarity: float = 0.45,
    max_roles: int | None = None,
    client: LLMClient | None = None,
    batch_size: int = 100,
) -> list[Role]:
    """Return roles whose embeddings are most similar to the candidate profile.

    keep_fraction: keep top N% of roles (default 40%)
    min_similarity: hard floor — drop anything below this regardless of fraction
    max_roles: hard cap on output count (v0.3.5). Applied AFTER fraction +
               min_similarity filtering. Caps Stage 1's input at 500 in the
               default orchestrator wiring — at 250-300 qualifying scale,
               this halves Stage 1+2 token spend without hurting recall
               (the bottom-similarity tail rarely produces qualifying roles).
    batch_size: how many role texts to embed per API call

    Mutates each input role's `embedding_similarity` field for inspection.
    """
    if not roles:
        return []

    client = client or get_llm_client()

    # Embed profile (one call)
    profile_emb_list = await client.embed(
        model=config.EMBEDDING_MODEL,
        texts=[_profile_to_text(profile)],
    )
    if not profile_emb_list:
        return roles  # fail-open: skip filter if embedding fails
    profile_emb = np.array(profile_emb_list[0], dtype=np.float32)

    # Embed roles in batches.
    # We pace the calls to stay under Gemini embedding RPM limits. The
    # AI Studio dashboard shows a 5K RPM ceiling on gemini-embedding-001,
    # but the *actual* enforced quota is
    # `BatchEmbedContentsRequestsPerMinutePerProjectPerRegion` — much
    # tighter (~15-200 RPM, regional, undocumented in the dashboard, but
    # confirmed via Google AI Developer Forum reports + repeated 429s in
    # our own audits on 600+ role runs). 5s between batches = 12 RPM,
    # safely under the regional cap. On 429, retry up to 3 times with
    # 65s / 130s / 195s exponential backoff before raising.
    all_role_embeddings: list[list[float]] = []
    n_batches = (len(roles) + batch_size - 1) // batch_size
    for batch_idx, i in enumerate(range(0, len(roles), batch_size)):
        if batch_idx > 0:
            await asyncio.sleep(5.0)  # 12 RPM — under regional batchEmbed cap
        batch_texts = [_role_to_text(r) for r in roles[i : i + batch_size]]
        emb = None
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                emb = await client.embed(
                    model=config.EMBEDDING_MODEL,
                    texts=batch_texts,
                )
                last_err = None
                break
            except Exception as e:
                msg = str(e)
                is_429 = (
                    "429" in msg
                    or "RESOURCE_EXHAUSTED" in msg
                    or "rate" in msg.lower()
                )
                if not is_429:
                    raise
                last_err = e
                # Skip backoff sleep after the final attempt
                if attempt < 2:
                    await asyncio.sleep(65 * (attempt + 1))
        if last_err is not None or emb is None:
            # All 3 attempts hit 429. Surface the error so the orchestrator
            # fails the run cleanly rather than continuing with partial
            # embeddings (which would silently break similarity ranking).
            raise last_err if last_err else RuntimeError(
                "embedding_filter: batch returned no embeddings"
            )
        all_role_embeddings.extend(emb)

    if len(all_role_embeddings) != len(roles):
        # Embedding API failure — fail-open and pass all roles through
        return roles

    role_matrix = np.array(all_role_embeddings, dtype=np.float32)
    similarities = _cosine_similarity(profile_emb, role_matrix)

    # Attach similarity to each role for downstream inspection
    for role, sim in zip(roles, similarities):
        role.embedding_similarity = float(sim)

    # Sort by similarity, keep top fraction (and above min threshold)
    keep_count = max(1, int(len(roles) * keep_fraction))
    indexed = sorted(
        enumerate(similarities), key=lambda x: x[1], reverse=True
    )
    kept_indices = [
        i for i, sim in indexed[:keep_count] if sim >= min_similarity
    ]
    # v0.3.5: hard cap on output. The fraction-based cap can let huge raw
    # pools (250-300 qualifying-scale runs see 8-12K raw roles → ~4K kept)
    # run away on Stage 1/2 cost. Capping at the top-N most similar keeps
    # the spend bounded without losing the high-confidence head.
    if max_roles is not None and len(kept_indices) > max_roles:
        kept_indices = kept_indices[:max_roles]
    kept_indices.sort()  # restore original ordering

    return [roles[i] for i in kept_indices]
