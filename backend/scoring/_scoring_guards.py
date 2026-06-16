"""Scoring guardrails (v0.3.24, FIX 29) — anti-inflation safeguards.

The 2026-05-30 adversarial scoring audit found a systematic, one-directional
over-scoring bias (56.7% tier calibration, ALL 14 disagreements "too high").
Three reproducible mechanisms, addressed here + in stage3_deep_eval.py:

  P0-1  Stage 3 hallucinated confident rationales on roles whose JD body was
        actually an SPA-redirect / "enable JavaScript" stub (zero real
        content), boosting scores +28 to +44. → jd_is_capturable() detects
        stubs so the caller can cap the tier instead of scoring fiction.

  P0-2  Large Stage2→Stage3 positive jumps with no JD evidence are a
        hallucination signal. → the caller clamps the delta when the JD is
        not capturable.

  P0-3  Excluded work (hands-on engineering, sales/GTM) sails through the
        TITLE filter because "Consultant"/"Lead"/"Enablement Manager" titles
        evade the pattern list — but the JD BODY is the excluded work. →
        scan_excluded_body_signals() flags body-level excluded duties.

These are pure, deterministic, unit-tested helpers (no LLM, no network) so
they can run inline in the scoring path with zero added cost/latency.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# P0-1: JD capturability — detect stub / redirect / bot-wall JD bodies
# ---------------------------------------------------------------------------

# Markers that indicate the "JD" is actually a JS shell, redirect, or block
# page rather than real posting content. Case-insensitive substring match.
_STUB_MARKERS = (
    "enable javascript",
    "please enable",
    "you need to enable",
    "javascript is required",
    "javascript to run",
    "redirecting",
    "please wait while",
    "loading…",
    "access denied",
    "are you a robot",
    "verify you are human",
    "captcha",
    "page not found",
    "404 not found",
    "this page isn't working",
    "temporarily unavailable",
    "enable cookies",
    "turn on javascript",
)

# Minimum real-content length. Below this, a JD can't support a confident
# deep-eval score — there isn't enough substance to judge fit. 250 chars is
# ~40 words; real JDs run 1,500-8,000 chars. Tuned to catch stubs without
# tripping on the occasional terse-but-real listing (those land 250-500).
_MIN_JD_CHARS = 250


def jd_is_capturable(jd_text: Optional[str]) -> bool:
    """Return True if jd_text looks like a REAL job description body.

    False when it is empty, a stub/redirect/bot-wall, or too short to
    support a deep-eval score. Callers should cap the tier (e.g. at MAYBE)
    and flag for re-scrape rather than let Stage 3 hallucinate a rationale.
    """
    if not jd_text:
        return False
    text = jd_text.strip()
    if len(text) < _MIN_JD_CHARS:
        return False
    low = text.lower()
    # If a stub marker dominates a short-ish body, it's a shell page.
    for marker in _STUB_MARKERS:
        if marker in low:
            # A real 5,000-char JD that merely mentions "captcha" in passing
            # shouldn't be killed — only treat the marker as fatal when the
            # body is short (stub pages are short AND contain the marker).
            if len(text) < 1200:
                return False
    return True


# ---------------------------------------------------------------------------
# P0-3: JD-body excluded-work scan (title-filter blind spot)
# ---------------------------------------------------------------------------

# Hands-on engineering / build work the candidate's profile excludes.
# Split into HIGH-confidence (a single hit is decisive — these phrases only
# appear in real build roles) and GENERIC (need >=2, since one could be a
# passing mention in an advisory role).
_ENG_HIGH = (
    r"\bsdlc\b",
    r"full[\-\s]?stack",
    r"\b(?:c#|asp\.net|\.net core|react|node\.js|angular|java\b|golang|c\+\+|typescript)\b",
    r"\bback[\-\s]?end\b|\bfront[\-\s]?end\b",
    r"writing production code",
    r"hands[\-\s]?on (?:senior |lead |principal )?(?:software )?(?:architect|engineer|developer)",
    r"\byears? (?:of )?(?:software|hands[\-\s]?on) (?:development|engineering|coding)\b",
    r"\bproficien\w+ (?:in|with) (?:python|java|c#|react|sql|azure|aws)\b",
)
_ENG_GENERIC = (
    r"hands[\-\s]?on (?:software|engineering|development|coding|ml|machine learning|expertise|experience)",
    r"\b(?:design|build|develop|implement|deliver) (?:and \w+ )?(?:ai/ml |ml |software |the )?(?:solutions|components|features|services|pipelines|models|applications)\b",
    r"\bsoftware development\b",
    r"engineering (?:practices|best practices|standards|excellence)",
    r"\bmodern (?:frameworks|tools|engineering)\b",
)

# Sales / GTM work the candidate excludes. The "enablement" false-friend:
# the title says enablement but the duties are enabling SELLERS.
_SALES_HIGH = (
    # Decisive: these only appear when the role's substance is selling.
    # NOTE: bare "sales/gtm enablement" is intentionally NOT here — it's the
    # exact false-friend word that collides with the candidate's legit "AI
    # Enablement" target, so it requires corroboration (moved to GENERIC).
    r"\b(?:quota|book of business|pipeline generation|deal[\-\s]?quality|revenue target|net new revenue)\b",
    r"\bpre[\-\s]?sales\b",
    r"\bobjection handling\b",
)
_SALES_GENERIC = (
    r"\b(?:sales|gtm|go[\-\s]?to[\-\s]?market) enablement\b",
    # "sales/solutions engineering" is often a PARTNER team in a comma-list,
    # not the role's own duty (v0.3.25) -> needs a 2nd sales signal to fire.
    r"\b(?:solutions?|sales) engineering\b",
    r"\benable (?:the |our )?(?:sales|sellers|reps|account executives|ae'?s|gtm|field)\b",
    r"\bcoach (?:reps|sellers|the sales|account)\b",
    r"\bclose deals\b|\bclosing deals\b",
    r"\bdemo(?:s|ing)? to prospects\b",
    r"\b(?:seller|rep) (?:productivity|onboarding|ramp)\b",
)

_ENG_HIGH_RE = [re.compile(p, re.IGNORECASE) for p in _ENG_HIGH]
_ENG_GEN_RE = [re.compile(p, re.IGNORECASE) for p in _ENG_GENERIC]
_SALES_HIGH_RE = [re.compile(p, re.IGNORECASE) for p in _SALES_HIGH]
_SALES_GEN_RE = [re.compile(p, re.IGNORECASE) for p in _SALES_GENERIC]


# ---------------------------------------------------------------------------
# Profile-aware gating (v0.3.25): only treat engineering / sales as EXCLUDED
# work when the candidate does NOT target it. A software engineer's profile
# targets engineering, so those roles must NOT be down-tiered for them; same
# for a salesperson. Without this, the guard (tuned for an AI-strategy
# consultant who excludes both) would bury the ideal roles of eng/sales users.
# ---------------------------------------------------------------------------
_ENG_TARGET_TERMS = (
    "software engineer", "software development engineer", "backend", "back-end",
    "front-end", "frontend", "full-stack", "full stack", "developer", "devops",
    "sre", "site reliability", "platform engineer", "ml engineer",
    "machine learning engineer", "data engineer", "programmer", "swe",
)
_SALES_TARGET_TERMS = (
    "sales", "account executive", "account manager", "business development",
    "bdr", "sdr",
)


def _profile_haystack(profile) -> str:
    """Lowercased blob of what the candidate TARGETS (functions / headline /
    keywords / seniority) — NOT their skills. (A consultant may list Python as
    a skill while targeting AI strategy; a SWE targets engineering.)"""
    if profile is None:
        return ""
    parts = []
    for attr in ("headline", "target_seniority"):
        v = getattr(profile, attr, None)
        if isinstance(v, str):
            parts.append(v)
    for attr in ("target_functions", "keywords"):
        v = getattr(profile, attr, None)
        if isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v)
    return " ".join(parts).lower()


def profile_targets_engineering(profile) -> bool:
    """True if the candidate is seeking engineering work (so eng JDs must NOT
    be treated as excluded for them)."""
    h = _profile_haystack(profile)
    return any(t in h for t in _ENG_TARGET_TERMS)


def profile_targets_sales(profile) -> bool:
    """True if the candidate is seeking sales work."""
    h = _profile_haystack(profile)
    return any(t in h for t in _SALES_TARGET_TERMS)


def scan_excluded_body_signals(jd_text: Optional[str], check_eng: bool = True, check_sales: bool = True) -> dict:
    """Scan a JD body for excluded-work signals the title filter misses.

    Returns {"engineering": [hits], "sales": [hits], "strong": bool}.
    `strong` (the down-tier trigger) is True when EITHER:
      - any HIGH-confidence signal fires (decisive phrases only seen in real
        build / sales roles, e.g. "C#/ASP.NET", "SDLC", "sales enablement",
        "quota"), OR
      - >=2 distinct GENERIC signals of one category fire.
    Scans the first ~4500 chars where core-duty language lives. One stray
    generic phrase ("we partner with engineers") will NOT down-tier.
    """
    out = {"engineering": [], "sales": [], "strong": False, "eng_high": [], "sales_high": []}
    if not jd_text:
        return out
    head = jd_text[:4500]

    def hits(regexes):
        found = []
        for rx in regexes:
            m = rx.search(head)
            if m:
                found.append(m.group(0)[:60])
        return sorted(set(found))

    eng_high = hits(_ENG_HIGH_RE) if check_eng else []
    eng_gen = hits(_ENG_GEN_RE) if check_eng else []
    sales_high = hits(_SALES_HIGH_RE) if check_sales else []
    sales_gen = hits(_SALES_GEN_RE) if check_sales else []

    out["engineering"] = sorted(set(eng_high + eng_gen))
    out["sales"] = sorted(set(sales_high + sales_gen))
    eng_strong = bool(eng_high) or len(eng_gen) >= 2
    sales_strong = bool(sales_high) or len(sales_gen) >= 2
    out["strong"] = eng_strong or sales_strong
    # Decisive (HIGH) hits — let callers distinguish hard dev/sales (REJECT)
    # from generic build/advisory language (STRETCH). (v0.3.25)
    out["eng_high"] = eng_high
    out["sales_high"] = sales_high
    return out


# ---------------------------------------------------------------------------
# P0-4 (v0.3.25): PRECISE off-target TITLE screen. Titles whose FUNCTION is
# clearly outside an AI-strategy/enablement/governance/PM candidate's targets —
# legal/counsel, partnerships/BD, revenue-ops, pre-sales/solutions-consulting,
# recruiting, market research, finance/accounting. These are high-precision
# (the off-target function is NAMED in the title) and validated to catch the
# Opus-SKIP junk with zero false-rejects on this profile. The caller gates on
# the profile (apply only for users who do NOT target these areas) and caps at
# SKIP. TODO before general ship: per-category profile gating (a recruiter
# targets "recruiter", a lawyer "counsel", etc.).
# ---------------------------------------------------------------------------
_OFF_TITLE_RE = re.compile(
    r"\b(counsel|attorney|paralegal|lawyer|"
    r"account executive|business development|\bbdr\b|\bsdr\b|partnerships?|partner solutions|"
    r"revenue (?:operations|strategy|technology)|field cto|pre[\-\s]?sales|sales engineer|"
    r"solutions consultant|recruiter|talent acquisition|sourcer|market research|"
    r"\baccountant\b|controller|auditor|finance & strategy)\b",
    re.IGNORECASE,
)


def title_clearly_off_target(title: Optional[str]) -> bool:
    """True if the role TITLE names a function clearly outside the candidate's
    targets (legal / BD / revenue-ops / pre-sales / recruiting / market research
    / finance). Profile-agnostic union; prefer title_off_target_for_profile()."""
    return bool(title and _OFF_TITLE_RE.search(title))


# v0.3.25 P0-4 GENERALIZED: per-CATEGORY off-target screen. Each category lists
# (a) the title patterns that NAME that function and (b) the profile-target terms
# that mean the CANDIDATE wants it. A title is off-target only when it names a
# category the candidate does NOT target — so a lawyer keeps "Counsel" roles, a
# recruiter keeps "Recruiter" roles, a seller keeps "Account Executive" roles, etc.
_OFF_CATEGORIES = {
    "legal": {
        "title": r"\b(counsel|attorney|paralegal|lawyer)\b",
        "target": ("legal", "counsel", "attorney", "lawyer", "litigation", "paralegal", "law firm"),
    },
    "sales/BD": {
        "title": (r"\b(account executive|business development|\bbdr\b|\bsdr\b|partnerships?|partner solutions|"
                  r"revenue (?:operations|strategy|technology)|field cto|pre[\-\s]?sales|sales engineer|solutions consultant|"
                  # W71q (Opus-450 dial-in): clearly-off-target sales/account titles that leaked into GOOD
                  r"customer engagement manager|client account (?:lead|manager|director)|account lead|"
                  r"gtm enablement|go[\-\s]?to[\-\s]?market enablement)\b"),
        "target": ("sales", "account executive", "account manager", "business development",
                   "bdr", "sdr", "gtm", "go-to-market", "revenue", "quota", "partnership"),
    },
    "recruiting": {
        "title": r"\b(recruiter|talent acquisition|sourcer)\b",
        "target": ("recruit", "talent acquisition", "sourcing", "staffing", "human resources"),
    },
    "marketing": {
        "title": r"\b(market research|content marketing|brand manager|demand generation|seo specialist|social media manager|marketing operations)\b",
        "target": ("marketing", "brand", "seo", "demand gen", "growth marketing", "content strategy"),
    },
    "finance/acct": {
        "title": r"\b(accountant|controller|auditor|financial analyst|bookkeeper|finance & strategy)\b",
        "target": ("finance", "accounting", "accountant", "audit", "controller", "fp&a", "financial"),
    },
    # v0.3.42 (Opus-validation dial-in 2026-06-15): off-target FUNCTIONS that
    # consistently leaked into GOOD on Ziad's runs. High-precision + profile-gated;
    # validated against 413 real roles (0 false-rejects on Opus STRONG/GOOD).
    # Knowledge-management was tried and DROPPED — it false-flagged a "Knowledge
    # Mgmt + AI Enablement" role Opus rated GOOD.
    "customer success": {
        # W71q (Opus-450 dial-in): + CSM variants that leaked into GOOD (customer
        # advocate, AI-vendor "success/deployment/agent strategy" CSM titles).
        "title": (r"\b(customer success|customer education|client success|customer experience manager|"
                  r"customer advocate|customer strategy director|agent strategy manager|"
                  r"ai success manager|ai deployment manager)\b"),
        "target": ("customer success", "csm", "customer experience", "account management", "client success", "customer advocate"),
    },
    "IT support/ops": {
        "title": (r"\b(service ?desk|help ?desk|desktop support|deskside support|it support|"
                  r"systems? administrator|sys ?admin|power platform (?:operations|administrator|admin)|"
                  r"sharepoint administrator)\b"),
        "target": ("service desk", "help desk", "it support", "system administration", "sysadmin", "power platform", "sharepoint admin", "infrastructure"),
    },
    "architecture": {
        # W71q: match "architecture" too (was missing GitLab "Enterprise Architecture").
        "title": r"\b(enterprise architect(?:ure)?|solutions? architect(?:ure)?|technical architect|cloud architect|data architect)\b",
        "target": ("architect", "architecture", "solution architect"),
    },
    # W71q (Opus-450 dial-in): community/devrel management leaked into GOOD.
    "community": {
        "title": r"\b(community manager|community management|online community)\b",
        "target": ("community management", "community manager", "developer relations", "devrel"),
    },
}
_OFF_CAT_RE = {k: re.compile(v["title"], re.IGNORECASE) for k, v in _OFF_CATEGORIES.items()}


def title_off_target_for_profile(title: Optional[str], profile) -> Optional[str]:
    """Return the off-target CATEGORY a title names IF the candidate does NOT
    target that category, else None. Profile-gated so it never rejects the
    candidate's OWN field. Caller caps the role at SKIP."""
    if not title:
        return None
    hay = _profile_haystack(profile)
    for cat, rx in _OFF_CAT_RE.items():
        if rx.search(title) and not any(t in hay for t in _OFF_CATEGORIES[cat]["target"]):
            return cat
    return None


# ---------------------------------------------------------------------------
# SC1 (v0.3.26): HARD experience / seniority cut-offs. The scorer rewards
# FUNCTIONAL overlap (AI strategy/enablement/governance) and ignored hard gates
# — roles demanding far more experience than the candidate has ("10+/15+
# years") or executive-level titles (Chief / VP / equity Partner) well above
# the candidate's target level. Profile-gated off years_experience +
# target_seniority so it never fires on roles AT the candidate's level. Caller
# caps at STRETCH (still visible, just not an apply-now match).
# ---------------------------------------------------------------------------
_YEARS_PLUS_RE = re.compile(r"(\d{1,2})\s*\+\s*years?", re.IGNORECASE)
_YEARS_CTX_RE = re.compile(
    r"(\d{1,2})\s*(?:\+\s*)?years?\s+(?:of\s+)?(?:relevant\s+|progressive\s+|professional\s+)?"
    r"(?:experience|leadership|management|industry|hands[\-\s]?on)",
    re.IGNORECASE,
)
# Company-AGE phrasing that follows a "N+ years" that is NOT a requirement
# ("40+ years in business", "serving clients for 30+ years"). Excluded so we
# don't read a firm's tenure as a candidate requirement.
_YEARS_AGE_CTX_RE = re.compile(
    r"^\s*(?:in business|of business|in operation|serving|of service|old\b|"
    r"history|anniversary|established|founded|of growth|of excellence|of experience serving)",
    re.IGNORECASE,
)
# Unambiguous executive titles. Spelled-out "Chief X Officer" (NOT bare "CIO/CTO"
# — those collide with "CIO Advisory Consultant", a role that ADVISES CxOs).
# "Director" is intentionally excluded (borderline); the years gate catches
# Director-level roles that carry a 10+/15+ yr requirement.
_EXEC_TITLE_RE = re.compile(
    r"\b(?:chief\s+(?:\w+\s+){0,2}officer|"
    r"(?:senior |sr\.? |executive |group |global )?vice[\-\s]?president\b|\bsvp\b|\bevp\b|\bvp\b|"
    r"managing director\b|equity partner\b)",
    re.IGNORECASE,
)
_EXEC_TARGET_TERMS = ("director", "vice president", " vp", "chief", "head of", "executive", "partner")


def required_years_from_jd(jd_text: Optional[str]) -> Optional[int]:
    """Highest plausible 'N+ years' / 'N years of experience' requirement in the
    JD's top section. Requires the '+' sign OR an experience/leadership word so
    it won't trip on prose like 'founded 10 years ago'; excludes company-age
    phrasing ("40+ years in business") and caps at 25 (anything higher is noise,
    not a hiring requirement)."""
    if not jd_text:
        return None
    sample = jd_text[:4000]
    yrs: list[int] = []
    for m in _YEARS_PLUS_RE.finditer(sample):
        if _YEARS_AGE_CTX_RE.match(sample[m.end():m.end() + 24]):
            continue  # company tenure, not a candidate requirement
        yrs.append(int(m.group(1)))
    yrs += [int(m.group(1)) for m in _YEARS_CTX_RE.finditer(sample)]
    yrs = [y for y in yrs if 1 <= y <= 25]
    return max(yrs) if yrs else None


def experience_seniority_gate(role, profile) -> Optional[Tuple[str, str]]:
    """Return (severity, reason) if the role sits materially ABOVE the
    candidate's experience/seniority, else None. Conservative + profile-gated.

    severity is graded (v0.3.27), per the user's calibration:
      - "stretch"  — a HARD cut-off: a years gap >= 7 (e.g. a ~7yr candidate vs
                     a "15+ years" role), or an executive title (Chief/VP/Partner)
                     above an IC/Manager target. Caller caps the score at STRETCH.
      - "one_tier" — a MODERATE stretch: a years gap of 3-6 (e.g. ~7yr vs "10+").
                     Caller demotes the role a single tier (inside the percentile
                     tiering pass), rather than hard-capping it.

    When the candidate's years are unknown we fall back to absolute bars
    (>=12 => stretch, 9-11 => one_tier), assuming a mid-level default."""
    if profile is None:
        return None
    title = role.job_title or ""
    jd = getattr(role, "job_description_full", None) or getattr(role, "job_description_essence", None) or ""

    # 1) Years requirement above the candidate, graded by the size of the gap.
    cand_years = getattr(profile, "years_experience", None)
    req = required_years_from_jd(jd)
    if req is not None:
        if isinstance(cand_years, int) and cand_years > 0:
            gap = req - cand_years
            if gap >= 7:
                return ("stretch", f"requires {req}+ yrs vs candidate's ~{cand_years}")
            if gap >= 3:
                return ("one_tier", f"requires {req}+ yrs vs candidate's ~{cand_years}")
        else:
            if req >= 12:
                return ("stretch", f"requires {req}+ yrs (candidate's years unknown)")
            if req >= 9:
                return ("one_tier", f"requires {req}+ yrs (candidate's years unknown)")

    # 2) Executive-level TITLE when the candidate targets IC/Manager (not exec)
    #    — always a hard cut-off.
    target = (getattr(profile, "target_seniority", "") or "").lower()
    target_is_exec = any(t in target for t in _EXEC_TARGET_TERMS)
    if not target_is_exec and _EXEC_TITLE_RE.search(title):
        return ("stretch", "executive-level title above candidate's IC/Manager target")
    return None


# ---------------------------------------------------------------------------
# SC2 (v0.3.26): non-standard engagements that aren't real full-time roles for
# this candidate — SkillBridge (military transition), internships, fellowships,
# apprenticeships, co-ops, volunteer. Title-first (most reliable), plus a
# SkillBridge scan of the JD top. Caller caps at SKIP.
# ---------------------------------------------------------------------------
_ENGAGEMENT_TITLE_RE = re.compile(
    r"\b(skill\s?bridge|internship|interns?|fellowship|apprenticeship|apprentice|"
    r"co[\-\s]?op program|volunteer|practicum|work[\-\s]?study)\b",
    re.IGNORECASE,
)
_SKILLBRIDGE_RE = re.compile(r"\bskill\s?bridge\b", re.IGNORECASE)


def offtarget_engagement(role) -> Optional[str]:
    """Return the engagement type if the role is a SkillBridge / internship /
    fellowship / apprenticeship / volunteer posting (not a target FT role),
    else None. Caller caps at SKIP."""
    title = role.job_title or ""
    m = _ENGAGEMENT_TITLE_RE.search(title)
    if m:
        return m.group(1)
    jd = getattr(role, "job_description_full", None) or ""
    if _SKILLBRIDGE_RE.search(jd[:1500]):
        return "SkillBridge"
    return None


# ---------------------------------------------------------------------------
# Credential gate (v0.3.35, Rule 11 from the 2026-06 human calibration). A JD
# that HARD-REQUIRES a security clearance, professional license, or named
# certification the candidate does NOT hold caps the role at MAYBE — these are
# often obtainable/sponsorable, so the role is worth surfacing but is not an
# apply-now match. "Preferred" / "a plus" / "willing to obtain" / "eligible"
# NEVER fires. Deterministic; the candidate's held credentials come from a
# profile field they set in the setup wizard. The orchestrator keeps the gate
# OFF entirely when that field is None (unanswered), so it no-ops for every
# existing profile until the user answers the question.
# ---------------------------------------------------------------------------

# canonical credential name -> regex that NAMES it in a JD. Bare ambiguous
# initialisms (MD, DO, PE, NP) are deliberately spelled out to avoid matching
# the Maryland abbreviation, the word "do", etc.
_CREDENTIAL_PATTERNS = (
    ("TS/SCI clearance",     r"\bTS\s*/\s*SCI\b|\btop\s+secret\s*/\s*sci\b"),
    ("Top Secret clearance", r"\btop\s+secret\b"),
    ("Secret clearance",     r"\bsecret\s+clearance\b|\bsecret[-\s]level\s+clearance\b"),
    ("polygraph",            r"\b(?:ci|counterintelligence|full[-\s]?scope|lifestyle)\s+polygraph\b|\bpolygraph\b"),
    ("Public Trust",         r"\bpublic\s+trust\b"),
    ("security clearance",   r"\bsecurity\s+clearance\b|\bgovernment\s+clearance\b|\bactive\s+clearance\b"),
    ("CPA",                  r"\bCPA\b|certified\s+public\s+accountant"),
    ("CFA",                  r"\bCFA\b|chartered\s+financial\s+analyst"),
    ("CFE",                  r"\bCFE\b|certified\s+fraud\s+examiner"),
    ("PMP",                  r"\bPMP\b|project\s+management\s+professional"),
    ("CISSP",                r"\bCISSP\b"),
    ("PE license",           r"\bPE\s+licen[sc]e\b|professional\s+engineer\s+licen[sc]e|licensed\s+professional\s+engineer"),
    ("RN license",           r"registered\s+nurse|\bRN\s+licen[sc]e\b|nursing\s+licen[sc]e|nurse\s+practitioner"),
    ("bar admission",        r"\bbar\s+(?:admission|membership)\b|admitted\s+to\s+the\s+bar|licensed\s+attorney|active\s+bar\s+(?:license|membership)"),
    ("CDL",                  r"\bCDL\b|commercial\s+driver'?s?\s+licen[sc]e"),
    ("medical license",      r"\bmedical\s+licen[sc]e\b"),
)
_CREDENTIAL_RE = [(name, re.compile(pat, re.IGNORECASE)) for name, pat in _CREDENTIAL_PATTERNS]

# A "hard requirement" cue near the credential mention.
_CRED_HARD_CUE_RE = re.compile(
    r"\b(required|requires|require\s+an?|must\s+(?:hold|have|possess|be|maintain|currently)|"
    r"mandatory|active|current(?:ly)?|maintain|minimum\s+of\s+an?)\b",
    re.IGNORECASE,
)
# A "soft / obtainable" cue that DISQUALIFIES the mention from being hard-required.
_CRED_SOFT_CUE_RE = re.compile(
    r"\b(preferred|a\s+plus|plus\b|nice\s+to\s+have|desired|desirable|ideally|"
    r"bonus|helpful|advantage|willing(?:ness)?\s+to\s+obtain|ability\s+to\s+obtain|"
    r"able\s+to\s+obtain|eligible|or\s+equivalent|not\s+required|sponsor)\b",
    re.IGNORECASE,
)


def detect_required_credential(jd_text: Optional[str]) -> Optional[str]:
    """Return the canonical name of a clearance / license / certification the JD
    HARD-REQUIRES, or None.

    A mention counts as hard-required only when a 'required/must/active/mandatory'
    cue sits within ~70 chars AND no soft cue ('preferred/a plus/willing to
    obtain/eligible/sponsor') sits in that same window. Conservative: when in
    doubt (soft cue present, or no hard cue) returns None — the gate only caps at
    MAYBE anyway, so it errs toward surfacing the role."""
    if not jd_text:
        return None
    text = jd_text[:6000]
    for name, rx in _CREDENTIAL_RE:
        for m in rx.finditer(text):
            window = text[max(0, m.start() - 70):min(len(text), m.end() + 70)]
            if _CRED_SOFT_CUE_RE.search(window):
                continue
            if _CRED_HARD_CUE_RE.search(window):
                return name
    return None


# Clearance hierarchy: holding a higher clearance covers a lower requirement.
_CLEARANCE_RANK = {"public trust": 1, "secret": 2, "top secret": 3, "ts/sci": 4}


def candidate_holds_credential(required: str, held: Optional[list]) -> bool:
    """True if the candidate's held credentials cover the JD's required one.

    Clearances are hierarchical (a TS/SCI holder covers a Secret requirement);
    everything else is matched by normalized keyword (substring either way).
    `held` None/empty -> holds nothing -> False. Leans toward False on ambiguity
    (the caller only caps at MAYBE, so a false-fire merely surfaces the role one
    tier lower rather than hiding it)."""
    if not held:
        return False
    req = (required or "").strip().lower()
    req_key = req.replace(" clearance", "").replace(" license", "").replace(" admission", "").strip()
    held_l = [str(h).strip().lower() for h in held if str(h).strip()]
    for h in held_l:
        h_key = h.replace(" clearance", "").replace(" license", "").replace(" admission", "").strip()
        if req_key and (req_key in h_key or h_key in req_key):
            return True
    req_rank = _CLEARANCE_RANK.get(req_key)
    if req_rank:
        for h in held_l:
            for name, rank in _CLEARANCE_RANK.items():
                if name in h and rank >= req_rank:
                    return True
    return False
