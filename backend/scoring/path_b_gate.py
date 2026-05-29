"""Path B — LightGBM Stage 3 gate (Wave-1, 2026-05-11).

Pre-Stage-3 LightGBM gate that predicts likely Stage 3 score from Stage 2
features. Skips Stage 3 for confidently-LOW predictions (saves ~$0.014 per
skipped call). 0 STRONG loss empirically validated on 18-audit corpus (8.6%
gate fire rate, 0/41 confirmed STRONGs demoted per Section 14.11.4).

Cross-archetype validation (round3_research/_path_c_v1_5_results.json):
    rho_v15_cross_archetype = 0.9726 on Zach (Business Admin) n=78.
    stage2_score is the dominant feature (15× the next feature in gain).

Failsafe: if model load fails for ANY reason (missing file, version
mismatch, lightgbm import error, corrupt artifact), `decide()` returns
("keep", "failsafe:<reason>") and Stage 3 fires for that role. NEVER blocks
the pipeline on gate failure.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from backend.models import CandidateProfile, Role


# ============================================================
# Featurizer (must match training-time exactly — feature names and order
# are written into the model artifact metadata as `feature_names`).
# ============================================================


def featurize_v1_5(role: Role, profile: CandidateProfile) -> dict[str, float]:
    """Path B v1.5 feature extraction.

    Restricted featurizer (Section 15.11.1): drops hardcoded Ziad-keyword
    features, keeps structural + profile-conditional features computed
    from the candidate profile.

    Feature naming is FROZEN. Adding/dropping features requires bumping
    model version (path_b_gate_v1_6 etc.) and retraining.
    """
    title = (role.job_title or "").lower()
    jd = (role.job_description_full or "").lower()
    location = (role.location or "").lower()
    loc_type = (role.location_type or "").lower()

    feats: dict[str, float] = {}

    # ---- Structural (profile-independent) ----
    feats["title_len_chars"] = float(len(title))
    feats["title_word_count"] = float(len(title.split()))
    feats["jd_len_chars"] = float(len(jd))
    feats["jd_word_count"] = float(len(jd.split()))
    feats["has_full_jd"] = 1.0 if len(jd) > 100 else 0.0

    feats["title_seniority_lead"] = 1.0 if any(
        s in title for s in ["lead", "principal", "staff", "head ", "vp ", "chief"]
    ) else 0.0
    feats["title_seniority_manager"] = 1.0 if any(
        s in title for s in ["manager", "director"]
    ) else 0.0
    feats["title_seniority_jr"] = 1.0 if any(
        s in title for s in ["junior", "entry", "associate", "intern"]
    ) else 0.0

    smin = float(role.salary_min or 0)
    smax = float(role.salary_max or 0)
    smid = (smin + smax) / 2 if smin and smax else (smin or smax or 0)
    feats["salary_min"] = smin
    feats["salary_max"] = smax
    feats["salary_log"] = math.log1p(smid)
    feats["salary_disclosed"] = 1.0 if (smin or smax) else 0.0

    feats["loc_remote"] = 1.0 if "remote" in loc_type or "remote" in location else 0.0
    feats["loc_hybrid"] = 1.0 if "hybrid" in loc_type or "hybrid" in location else 0.0
    feats["loc_onsite"] = 1.0 if "onsite" in loc_type or "on-site" in loc_type else 0.0

    src = (role.source or "").lower()
    feats["src_is_workday"] = 1.0 if "workday" in src else 0.0
    feats["src_is_lever"] = 1.0 if "lever" in src else 0.0
    feats["src_is_greenhouse"] = 1.0 if "greenhouse" in src else 0.0
    feats["src_is_indeed"] = 1.0 if "indeed" in src else 0.0
    feats["src_is_googlejobs"] = 1.0 if "google" in src else 0.0
    feats["src_is_linkedin"] = 1.0 if "linkedin" in src else 0.0

    feats["embedding_sim"] = float(role.embedding_similarity or 0)
    feats["emb_high"] = 1.0 if feats["embedding_sim"] > 0.65 else 0.0
    feats["emb_low"] = 1.0 if feats["embedding_sim"] < 0.60 else 0.0

    # ---- Profile-conditional ----
    s2 = role.stage2_score
    feats["stage2_score"] = float(s2) if s2 is not None else 50.0
    feats["stage2_score_missing"] = 1.0 if s2 is None else 0.0

    # Stage 2 reasoning length proxy for confidence
    stage2_reasoning = getattr(role, "stage2_reasoning", "") or ""
    feats["stage2_reasoning_len"] = float(len(stage2_reasoning))

    # Stage 1 reason length — not preserved on Role; treat as 0
    feats["stage1_reason_len"] = 0.0

    profile_smin = float(profile.salary_minimum or 0)
    feats["salary_meets_user_minimum"] = 1.0 if (
        smin and profile_smin and smin >= profile_smin
    ) else 0.0
    feats["salary_gap_pct"] = (
        (smin - profile_smin) / max(1, profile_smin) if smin else 0.0
    )

    headline_text = (profile.headline or "").lower()
    headline_tokens = set(re.findall(r"[a-z]{3,}", headline_text))
    headline_tokens -= {"and", "with", "for", "the", "experience", "years"}
    title_tokens = set(re.findall(r"[a-z]+", title))
    jd_tokens = set(re.findall(r"[a-z]+", jd))
    feats["headline_token_in_title_pct"] = (
        len(headline_tokens & title_tokens) / max(1, len(headline_tokens))
    )
    feats["headline_token_in_jd_pct"] = (
        len(headline_tokens & jd_tokens) / max(1, len(headline_tokens))
    )

    target_functions = list(profile.target_functions or [])
    tf_tokens: set[str] = set()
    for tf in target_functions:
        tf_tokens.update(re.findall(r"[a-z]{3,}", tf.lower()))
    tf_tokens -= {"and", "the", "for"}
    feats["target_function_overlap_title"] = float(len(tf_tokens & title_tokens))
    feats["target_function_overlap_jd"] = float(len(tf_tokens & jd_tokens))

    kw_pool: list[str] = []
    keywords = getattr(profile, "keywords", None)
    if isinstance(keywords, list):
        kw_pool.extend(keywords)
    feats["profile_keyword_match_count"] = float(sum(
        1 for k in kw_pool[:30]
        if isinstance(k, str) and k.lower() in (title + " " + jd)
    ))

    excluded_patterns = getattr(profile, "excluded_title_patterns", None) or []
    feats["excluded_pattern_violations"] = float(sum(
        1 for p in excluded_patterns
        if isinstance(p, str) and p.lower() in title
    ))

    return feats


# ============================================================
# Confidence proxy
# ============================================================


def lgb_confidence(prediction: float) -> float:
    """Distance-from-mid-band confidence proxy.

    confidence = |pred - 67.5| / 32.5, clamped to [0, 1].
    Predictions far from the training mean are higher-confidence; near-mid
    predictions are lower-confidence (model genuinely uncertain).
    """
    return min(1.0, abs(prediction - 67.5) / 32.5)


# ============================================================
# Gate decision
# ============================================================


@dataclass
class Decision:
    skip: bool
    reason: str
    predicted_score: Optional[float] = None
    confidence: Optional[float] = None


# ============================================================
# Gate class — wraps loaded LightGBM model
# ============================================================


class PathBGate:
    """LightGBM-backed Stage 3 gate.

    Construct via PathBGate.load(model_path). On any load error, returns
    None (caller checks; falls back to current Skip-Above behavior).
    """

    MODEL_VERSION = "path_b_gate_v1_5"

    def __init__(self, booster: Any, feature_names: list[str]) -> None:
        self._booster = booster
        self._feature_names = list(feature_names)

    @classmethod
    def load(cls, model_path: Path) -> Optional["PathBGate"]:
        """Load model + metadata. Returns None on any failure."""
        try:
            import lightgbm as lgb  # noqa: WPS433 — runtime import
        except ImportError:
            print("[path_b] lightgbm not installed; gate disabled")
            return None

        meta_path = model_path.with_suffix(".meta.json")
        if not model_path.exists() or not meta_path.exists():
            print(f"[path_b] model artifact missing ({model_path.name}); gate disabled")
            return None

        try:
            booster = lgb.Booster(model_file=str(model_path))
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            feat_names = meta.get("feature_names")
            if not isinstance(feat_names, list) or not feat_names:
                print(f"[path_b] meta missing feature_names; gate disabled")
                return None
        except Exception as e:
            print(f"[path_b] model load failed ({type(e).__name__}: {e}); gate disabled")
            return None

        return cls(booster, feat_names)

    def predict(self, role: Role, profile: CandidateProfile) -> Optional[float]:
        """Run the LightGBM prediction. Returns predicted Stage 3 score, or None on error."""
        try:
            feats = featurize_v1_5(role, profile)
        except Exception as e:
            print(f"[path_b] featurize failed ({type(e).__name__}): {e}")
            return None
        try:
            vec = [feats.get(k, 0.0) for k in self._feature_names]
            import numpy as np
            pred_arr = self._booster.predict([vec])
            return float(pred_arr[0])
        except Exception as e:
            print(f"[path_b] predict failed ({type(e).__name__}): {e}")
            return None

    def decide(self, role: Role, profile: CandidateProfile) -> Decision:
        """Returns gate Decision per Section 15.11.1 banded approach.

        Hard rules:
          - s2 missing → keep (failsafe; cannot evaluate)
          - s2 in [60, 88): protected band → always keep (Pro uplift zone)
          - s2 >= 88: confident-STRONG band → skip (empirically 0 STRONG loss)
          - s2 in [55, 60): conditional skip on (pred < 50 AND confidence >= 0.5
            AND |pred - s2| <= 25)
          - s2 < 55: out of Stage 3 band entirely → keep (caller-side filter)
        """
        s2 = role.stage2_score
        if s2 is None:
            return Decision(skip=False, reason="s2_missing_failsafe")

        # Protected band — Pro uplift typically lands here
        if 60 <= s2 < 88:
            return Decision(skip=False, reason="protected_band")

        # Confident-STRONG band — Skip-Above 88 already covers this
        # (orchestrator filters at stage3_skip_above before we see it).
        # Defensive: still emit a Decision in case caller threshold differs.
        if s2 >= 88:
            return Decision(skip=True, reason="confident_strong_band")

        # Low band [55, 60) — conditional skip
        if 55 <= s2 < 60:
            pred = self.predict(role, profile)
            if pred is None:
                return Decision(skip=False, reason="failsafe:predict_error")
            conf = lgb_confidence(pred)
            # Triple guard: prediction must be confident, in low range, and
            # agree with s2 within 25 points (large disagreement = let Pro
            # see it, model may be wrong).
            if pred < 50 and conf >= 0.5 and abs(pred - s2) <= 25:
                return Decision(
                    skip=True,
                    reason="low_band_predicted_low",
                    predicted_score=pred,
                    confidence=conf,
                )
            return Decision(
                skip=False,
                reason="low_band_uncertain",
                predicted_score=pred,
                confidence=conf,
            )

        # Below Stage 3 band — orchestrator filters these out before gate;
        # defensive default
        return Decision(skip=False, reason="below_band")


# Convenience: default model location
DEFAULT_MODEL_PATH = Path(__file__).parent / "models" / "path_b_gate_v1_5.txt"


def load_default_or_none() -> Optional[PathBGate]:
    """Load the default Wave-1 path B model. Returns None if unavailable."""
    return PathBGate.load(DEFAULT_MODEL_PATH)
