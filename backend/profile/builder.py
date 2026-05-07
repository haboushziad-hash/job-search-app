"""Profile builder — turns one or more resumes into a unified CandidateProfile.

When the user uploads multiple resume versions (e.g., one emphasizing AI
strategy, one emphasizing consulting), we merge them into a single profile
with weighted target areas. Each resume's emphasis is preserved as metadata
so Stage 3 can flag "best resume for this role."

Single LLM call: ingests all resumes + user-provided preferences, outputs:
  1. CandidateProfile (target functions, skills, seniority, etc.)
  2. Personalized keyword list (30-50 keywords, tier-tagged)

This is the foundation that makes the tool "smart" — every search is
personalized to the candidate's actual background, not generic AI keywords.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from backend.config import config
from backend.models import CandidateProfile, Keyword, ResumeMetadata
from backend.profile.resume_parser import parse_resume
from backend.scoring.llm_client import LLMClient, get_llm_client


# Progress reporting — called from inside build_profile_from_resumes
# at each meaningful stage. callback(percent: int, stage: str) → None.
ProgressCallback = Callable[[int, str], None]


PROFILE_BUILDER_PROMPT = """\
You are a career analyst. Given inputs from a candidate, produce a unified
profile + a tailored job-search keyword list.

============================================================
CORE PRINCIPLE — READ CAREFULLY
============================================================
KEYWORDS MUST MATCH EMPLOYER JOB TITLES, NOT RESUME LANGUAGE.

The biggest mistake is generating keywords that summarize the candidate's
resume. Resumes describe what someone HAS DONE; keywords must describe what
EMPLOYERS POST. These are different vocabularies.

Bad (resume-summarizing): "AI Policy Analyst", "Data Analytics Consultant"
Good (employer-job-title): "AI Enablement Lead", "AI Adoption Manager"

If the candidate's resume says "policy analysis and AI governance," but the
roles they're TARGETING are titled "AI Enablement Lead" and "AI Adoption
Manager," generate the latter — not the former.

============================================================
INPUT TYPES — HOW TO USE EACH
============================================================

1. RESUME(S) — "Who they are"
   Use ONLY for: technical_skills, domain_expertise, years_experience,
   headline, soft_skills. NOT for keyword generation directly.
   Multiple resumes are different framings of the same candidate; UNION
   the targets, don't average.

2. USER PREFERENCES — "Hard requirements"
   Filter criteria: salary, locations, work arrangements. Not signals.

3. USER FREEFORM CONTEXT — "What roles they ACTUALLY WANT" (HIGHEST WEIGHT)
   This is where keywords come from. Specifically:

   a) Stated targets ("I'm targeting X" / "I want roles like Y") → Tier 1.
   b) Pasted JDs ("I love roles like this") → extract title patterns + put
      Tier 1 variants in keywords. Use the JD's emphasis to seed
      target_functions.
   c) "Avoid X" / "moving away from Y" → negative_signals. NEVER tier those
      areas as Tier 1, even if the resume describes them.
   d) Industry/seniority/company-size preferences → target_industries,
      target_seniority.

   FREEFORM CONTEXT OVERRIDES RESUME for keyword generation.

============================================================
KEYWORD GENERATION PRINCIPLES
============================================================

PRINCIPLE 1: TITLE-NAMING VARIATIONS
For each target concept, generate the realistic naming variations employers
actually use. Most concepts have 3-5 variants:

  Concept "AI Enablement" →
    Tier 1: "AI Enablement Lead", "AI Enablement Manager",
            "AI Enablement Specialist"
    Tier 2: "AI Enablement Director" (if seniority fits)

  Concept "AI Adoption" →
    Tier 1: "AI Adoption Lead", "AI Adoption Manager"
    Tier 2: "AI Adoption Specialist"

  Concept "AI Strategy" →
    Tier 1: "AI Strategy Consultant", "AI Strategist", "AI Strategy Lead"
    Tier 2: "AI Strategy Director", "Senior AI Strategist"

DO NOT just pick one variant — coverage matters. "AI Enablement Specialist"
and "AI Enablement Lead" are different searches with different result sets.

PRINCIPLE 2: USE EMPLOYER VOCABULARY
Common employer-side title patterns:
  - "{Concept} Lead"           (mid-senior IC)
  - "{Concept} Manager"        (people manager / function lead)
  - "{Concept} Specialist"     (mid-level IC)
  - "{Concept} Consultant"     (advisory role)
  - "{Concept} Director"       (senior leadership)
  - "Senior {Concept}"         (senior IC)
  - "Head of {Concept}"        (function head)
  - "Principal {Concept}"      (advanced senior IC)

Apply the variants that fit the candidate's seniority. A "Director" target
should not get "Specialist" variants.

PRINCIPLE 3: BROAD-BY-DEFAULT TERMS — SUPPRESS UNLESS USER-TARGETED
The following terms are very broad. They pollute most candidates' results
because they pull thousands of off-target roles. **DO NOT generate them by
default**. ONLY include them as keywords if the candidate's freeform context
or resume explicitly names them as a target:

  - "Management Consultant" (most non-MBB candidates don't want generic mgmt
    consulting; only include if user names it)
  - "Project Management Consultant" / generic PMP titles
  - "Business Intelligence Consultant" / "BI Developer"
  - "Data Analytics Consultant" / "Analytics Consultant"
  - "Solutions Architect" / "AI Solutions Architect"
  - "ML Engineer" / "Machine Learning Engineer"
  - "Data Engineer" / "Data Scientist"
  - "Workflow Optimization Specialist" / "Process Improvement"
  - Single-word generics: "AI", "consultant", "manager", "engineer"

INCLUDE THEM if any of these is true:
  - The candidate's freeform context names the role explicitly
    ("I'm targeting ML Engineer roles") → it becomes a valid Tier 1 keyword
  - The candidate's resume CURRENT job is one of these AND they're not
    explicitly trying to leave it (no negative_signal saying so)
  - The candidate's stated target_functions overlap clearly with the term

The principle: BROAD ≠ FORBIDDEN. Broad = "off by default to avoid noise,
on if the user actually wants it." A data engineer targeting ML Engineer
roles SHOULD see that keyword in Tier 1; a strategy consultant targeting
AI advisory roles should NOT.

PRINCIPLE 3.5: "USES AI TOOLS" ≠ "TARGETS AI ROLES" (CRITICAL — v0.3.4)
This is the most common keyword-pollution failure: a non-AI candidate
mentions Copilot, ChatGPT, "GenAI", "AI tools", "leveraged AI", or
similar tooling phrases ONCE OR TWICE in their resume — usually in a
skills section at the bottom or as a brief bullet ("used Copilot to
draft client emails"). The LLM then concludes the candidate is
targeting AI Strategy / AI Adoption / AI Enablement / AI Governance
roles. THIS IS WRONG. Most professionals now use AI tools at work the
same way they use Excel — it does NOT make them an AI specialist.

ROLE OF AI MENTIONS — DECISION RULE:
  Step 1: Count how the resume describes AI/Copilot/GenAI work.
  Step 2: Classify each mention into one of three buckets:

  BUCKET A — TOOL USAGE (does NOT justify AI keywords):
    • "Used Copilot to draft emails / summarize meetings / write code"
    • "Familiar with ChatGPT, Claude, Gemini"
    • Skills section listing "AI tools" / "Generative AI" / "Copilot"
    • "Leveraged AI to streamline reporting"
    • "AI-assisted data analysis" (generic — same as "Excel-assisted")
    • Any mention where AI is the verb's instrument, not the object
    • LinkedIn endorsements / certificates in "AI for [non-AI field]"

  BUCKET B — DOMAIN ADJACENT (justifies SECONDARY/TIER 2 AI keywords ONLY,
  if and only if the candidate's freeform context confirms AI as a target):
    • "Piloted Copilot rollout for my team of 8"
    • "Member of internal AI working group"
    • "Authored AI usage guidelines for my department"
    • "Trained 5 colleagues on Copilot"
    • Coursework / certificate in AI strategy / AI ethics / responsible AI
    • One bullet about an AI-related side project at current job

  BUCKET C — AI AS CORE JOB FUNCTION (justifies PRIMARY/TIER 1 AI keywords):
    • Title contains "AI" / "Machine Learning" / "Data Science" / "GenAI"
    • Owned an AI roadmap / strategy / adoption program at a company
    • Multi-year track record of AI program management or AI consulting
    • Built or deployed ML models / RAG systems / AI products
    • Founded / led an AI practice or AI function

KEYWORD GENERATION RULES:
  - If ALL the candidate's AI evidence is BUCKET A → DO NOT generate
    "AI Strategy", "AI Adoption", "AI Enablement", "AI Governance",
    "AI Program Manager", "AI Operations Manager", "Generative AI
    Consultant", "Copilot Lead", or any AI-domain title in ANY tier.
    Generate ONLY non-AI keywords matching their actual function.
  - If BUCKET B evidence + freeform context says they want to pivot
    INTO AI roles → put 2-3 AI keywords in TIER 2 ONLY (never Tier 1).
  - If BUCKET C evidence → AI keywords in Tier 1 are appropriate.
  - WHEN UNCERTAIN, ASSUME BUCKET A. False AI-keyword generation
    pollutes the entire run and produces 100+ AI roles that the
    candidate is unqualified for.

PROFILE FIELD RULES (target_functions / target_industries / headline):
  - target_functions: must reflect the candidate's ACTUAL function +
    stated freeform target. NEVER add "AI Strategy", "AI Adoption",
    "AI Governance", "AI Enablement", or "AI Program Management"
    from BUCKET A evidence alone. A QA lab tech who uses Copilot is
    a QA lab tech, NOT an "AI Adoption Manager."
  - target_industries: NEVER add "Technology" or "AI" or "Tech" from
    BUCKET A evidence alone. Using AI tools at work doesn't make
    someone target the Tech industry. A billing specialist is in
    Financial Services / Healthcare / their actual industry, NOT
    Technology, even if they use Copilot daily.
  - headline: do NOT lead the headline with "AI" if the candidate's
    AI evidence is BUCKET A. "Senior Billing Specialist with 7 years
    of experience" is correct; "AI-Enabled Billing Specialist" is NOT.

WORKED EXAMPLES:

  EXAMPLE 1 — Operations / Lab / QA candidate, 3 yrs, Life Sciences,
  resume mentions "Copilot for documentation" + "Generative AI" in
  skills section. NO freeform AI targeting.
    AI evidence classification: BUCKET A only.
    target_functions: ["Business Operations", "Lab Operations",
                       "Quality Assurance", "Process Improvement",
                       "Project Coordination"]
                       (NO AI functions — she's not an AI specialist)
    target_industries: ["Life Sciences", "Biotechnology",
                       "Pharmaceuticals", "Healthcare", "Medical Devices"]
                       (NO "Technology" — she's in life sciences)
    keywords (T1): "Operations Coordinator", "Lab Operations Manager",
                   "Quality Assurance Specialist", "Project Coordinator",
                   "Operations Specialist" — NO AI keywords anywhere.

  EXAMPLE 2 — Senior Billing Specialist, 7 yrs Financial Services,
  resume mentions Copilot once for invoice summarization, no other
  AI signals, no AI freeform targeting.
    AI evidence classification: BUCKET A only.
    target_functions: ["Billing Operations", "Revenue Operations",
                       "Account Management", "Financial Systems Analysis"]
                       (NO AI functions)
    target_industries: ["Financial Services", "Healthcare", "Insurance",
                       "Real Estate", "Utilities"]
                       (NO "Technology")
    keywords (T1): "Senior Billing Specialist", "Billing Operations
                   Manager", "Revenue Operations Analyst", "Accounts
                   Receivable Lead" — NO AI keywords anywhere.

  EXAMPLE 3 — Senior Consultant at Booz Allen, 7 yrs, headline says
  "AI Strategy Consultant," resume has 3 separate AI strategy
  engagements + GenAI working group membership + freeform context
  says "targeting AI strategy / enablement / governance roles."
    AI evidence classification: BUCKET C.
    target_functions: AI Strategy / Enablement / Governance — correct.
    keywords (T1): "AI Strategy Consultant", "AI Enablement Lead",
                   "AI Adoption Manager", "AI Program Manager" — correct.

  EXAMPLE 4 — Marketing Director, 12 yrs CPG, took an AI-for-
  Marketing certificate, ran one Copilot rollout for her team.
  Freeform says "open to AI-adjacent marketing roles."
    AI evidence classification: BUCKET B.
    target_functions: ["Marketing Operations", "Brand Marketing",
                       "Demand Generation"]
                       (her primary function — NOT AI)
    keywords (T1): standard marketing keywords.
    keywords (T2): 1-2 entries like "AI Marketing Manager" or
                   "Marketing AI Adoption Lead" (T2 only — secondary
                   target).

============================================================
PRINCIPLE 4: NEGATIVE-SIGNAL GATING
If the candidate states they want to move AWAY from something (e.g.,
"moving out of federal", "tired of government work", "want to leave
consulting"), do NOT generate Tier 1 keywords in that area. The resume
might describe their CURRENT role, but their next role is the goal.

Example: candidate says "moving out of federal."
  Resume: heavy federal/government work
  Wrong: Tier 1 = "Federal AI Strategy", "Government AI Consultant"
  Right: Tier 2/3 (still allowed but not prioritized) OR omit entirely

PRINCIPLE 5: TIER ASSIGNMENT — PRIMARY vs SECONDARY TARGETS

When a candidate names multiple target paths (e.g., "PM, operations,
consulting, OR finance"), DO NOT spread Tier 1 equally across all of them.
That dilutes T1 and produces noisy searches.

Step 1: Identify the candidate's PRIMARY 1-2 targets — the ones their
        resume most strongly substantiates. Look at:
          - Years of experience in that function
          - Quantified accomplishments / proof points in that area
          - Skills section emphasis
          - Most recent / current role
Step 2: Identify SECONDARY targets — paths the candidate stated they're
        open to but where the resume is weaker (e.g., coursework only,
        tangential exposure, aspiration).

Tier 1 (always-run, ~10-15 keywords): primary target(s) ONLY.
  Multiple title-naming variants of the primary concepts go here.
Tier 2 (quota-allows, ~10-15 keywords): secondary targets + adjacent /
  stretch concepts under primary targets + funnel/lateral roles.
Tier 3 (optional, ~8-12 keywords): broader exploration — fields the
  candidate would consider but isn't actively pursuing, OR primary-target
  titles that require seniority/credentials they lack.

EXAMPLE — Senior Consultant targeting "AI enablement, governance, strategy":
  Primary (resume-substantiated): AI enablement, AI strategy → T1
  Secondary (resume mentions but lighter): AI governance, AI policy → T2

EXAMPLE — Business Admin new grad targeting "PM / ops / consulting / finance"
with 4 years of route sales / territory management:
  Primary (resume-substantiated): operations, consulting → T1
  Secondary (coursework or weaker fit): finance, project management → T2

RESUME EVIDENCE GUARD: Do NOT place a title in Tier 1 if the candidate's
resume provides only COURSEWORK or TANGENTIAL EXPOSURE for that title's
expected skill stack. Examples to demote:
  - "FP&A Analyst" T1 when resume shows finance minor + general analysis
    but no modeling/variance/budgeting experience → T2
  - "Supply Chain Analyst" T1 when resume shows logistics-on-a-route but
    no ERP/procurement/SCM certifications → T2
  - "Data Analyst" T2 when resume shows Excel + Salesforce reporting but
    no SQL/Python/Tableau → T3 or omit

TOTAL: 30-40 keywords. Quality > quantity. Don't pad with noise.

============================================================
SEARCH TERMS — broader phrases for upstream API queries (NEW v0.1.4)
============================================================
In addition to the tier-1/2/3 keyword lists above (specific job titles),
generate **8-12 broad SEARCH TERMS** (`search_terms`). These are 2-3 word
phrases used as the literal query string for upstream job-board APIs
(Workday's searchText, Adzuna's what_phrase, JSearch's query, etc.).

WHY a separate broad list:
- Specific titles like "AI Enablement Lead" don't match upstream APIs
  when the actual posting is "Lead, AI Enablement" — upstream filters
  by literal phrase before our tools see anything.
- Broader 2-3 word phrases like "AI enablement" or "tax compliance"
  catch a much wider net at the upstream layer. Our token-overlap
  matcher then verifies relevance downstream.

RULES for search_terms:
  - 2-3 words each (NOT 1 word — too broad, NOT 4+ — too narrow)
  - Functional concepts, NOT specific titles. "AI strategy" GOOD.
    "Senior AI Strategist" BAD (that's a keyword, not a search term).
  - Cover the candidate's primary AND secondary targets. Each Tier 1
    keyword cluster gets 1-2 search terms representing the concept.
  - Include domain modifiers when they sharply narrow vs noise:
    "tax provision" not "provision", "AI governance" not "governance".
  - Industry-standard phrases preferred. Use phrases that hiring
    managers and recruiters actually type.

EARLY-CAREER ROTATIONAL PROGRAMS (CRITICAL):
For candidates with fewer than 5 years of experience OR entry-level/
associate target seniority, ALWAYS include 2-3 industry-standard
rotational/development program search terms. Examples:
  - "leadership development program"
  - "management trainee"
  - "rotational analyst program"
  - "associate brand manager" (for CPG candidates)
  - "operations leadership program"
  - "sales development program"
These are common early-career pathways that won't appear under
specific title searches but are heavily posted on aggregator boards
by Fortune 500 employers (P&G, General Mills, Mars, JP Morgan, etc.).

EXAMPLES of good search_terms by persona:

Senior AI consultant (federal/commercial), 7 years:
  ["AI strategy", "AI enablement", "AI governance", "AI adoption",
   "AI consulting", "AI transformation", "change management",
   "AI program management"]

Business Admin senior, 4 years route sales (CPG track):
  ["account management", "territory sales", "sales operations",
   "operations analyst", "supply chain analyst",
   "leadership development program", "management trainee",
   "category manager"]

Senior Tax Associate, 5 years Big 4:
  ["tax accountant", "tax analyst", "tax manager", "tax compliance",
   "tax provision", "corporate tax", "international tax"]

Environmental data scientist with AI training crossover:
  ["environmental data", "GIS analyst", "remote sensing",
   "research scientist", "data scientist", "AI training",
   "research engineer", "environmental science"]
  ↑ note dual-vector for crossover personas

Output as a flat list of strings under the `search_terms` field.

============================================================
PRINCIPLE 6: TITLE-PATTERN MARKET INTELLIGENCE (field-agnostic)
============================================================
Some title patterns produce disproportionately good matches across many
fields. Others sound prestigious but consistently disappoint. Apply this
knowledge regardless of the candidate's field (strategy, accounting, data
engineering, marketing, healthcare, etc.).

HIGH-VALUE PATTERNS (treat as T1 when matching candidate's target):
  - "{Concept} Operations Manager" / "{Concept} Operations Lead"
    Modern companies use "Operations" as a name for tactical practitioner
    work. Don't dismiss as generic — it's often where the real work is.
    Examples: "Marketing Operations Manager", "Sales Ops Lead",
    "Revenue Operations", "AI Operations Manager", "Finance Operations".
  - "{Concept} Adoption Lead/Manager" — applies to enablement work in
    any field (tools, platforms, methodologies, programs)
  - "{Concept} Change Management Lead" — strong signal across many
    transformation-flavored fields
  - "{Concept} Program Manager" / "Program Manager, {Concept}"
    Many companies use "Program Manager" for what others call "Lead".
    Often T1, not T2.
  - "{Concept} Engagement Manager" — common at consulting firms across
    industries (strategy, accounting/audit, IT, healthcare advisory)
  - "{Concept} Deployment Manager" — client-facing rollout work
  - "{Concept} Coach" / "{Concept} Advisor" — niche but real

PRESTIGE-TRAP PATTERNS (sound T1 but usually mismatch):
  - "Head of {Concept}" — almost always requires substantial management
    experience. Default T2 unless candidate explicitly has Director+
    background. Applies in every field.
  - "VP {Concept}" / "Chief {Concept} Officer" — same management gap.
    Default T2/T3 unless senior leadership track.
  - "{Concept} Product Manager" — different function (product management
    has its own skill stack). Default T3 UNLESS candidate explicitly has
    PM background or is targeting PM roles.
  - "{Concept} Analyst" / "{Concept} Business Analyst" — for senior
    candidates this is a title downgrade and often surfaces junior or
    government-scale roles. For mid-junior candidates it can be T1/T2.
    Calibrate by candidate seniority.
  - "{Concept} Solutions Architect" / "{Concept} Engineer" — technical
    engineering roles. T3 or skip for non-technical candidates; T1 for
    technical candidates who explicitly target this.

DOMAIN-SPECIFIC NICHES (apply when matching candidate's stated field):
  IF candidate works with Microsoft tooling / Copilot / M365:
    - "Copilot enablement" / "Copilot adoption" / "Copilot Manager"
    - "M365 Copilot Lead" / "GenAI enablement"
  IF candidate is in accounting/finance/audit:
    - "Senior Manager, {Practice}" / "Director, {Practice}"
    - "{Practice} Controller" / "Audit Manager"
  IF candidate is in healthcare/pharma:
    - "Clinical {Concept} Manager" / "Health Economics Lead"
  IF candidate is in marketing/growth:
    - "Demand Gen Manager" / "Growth Marketing Lead"
  IF candidate is in supply chain / logistics / operations:
    - "Supply Chain {Concept} Manager" / "Logistics Operations Lead"
  IF candidate is in CPG / retail / distribution / route sales:
    - "Account Manager" / "Key Account Manager" / "Territory Sales Manager"
    - "Route Manager" / "District Manager" / "Field Operations Manager"
    - "Category Manager" / "Category Analyst" / "Buyer" / "Junior Buyer"
    - "Trade Marketing Analyst" / "Pricing Analyst"
    - "Sales Development Representative" (SDR) for early-career sales
  IF candidate is federal / public sector / government contracting (DC area,
  Booz Allen / Deloitte Federal / Accenture Federal background) AND not
  explicitly leaving:
    - "Federal {Concept} Consultant" / "Public Sector {Concept} Consultant"
    - "{Concept} Modernization Consultant" / "Mission {Concept} Lead"
    - "Forward Deployed Strategist" (Palantir-style client-facing)
  IF candidate is early-career / new grad with strong GPA:
    - "{Function} Leadership Program" / "{Function} Rotational Program"
    - "Management Trainee" / "Associate {Function} Analyst"

Calibrate to the candidate's actual field — don't apply niches that
aren't relevant to their resume + freeform context.

============================================================
PRINCIPLE 7: RECRUITER-PERSPECTIVE FUNNEL/LATERAL KEYWORDS
============================================================
Before finalizing, ask yourself: "If a specialized RECRUITER in this
candidate's industry were searching job boards on their behalf, what 3-5
niche/funnel/lateral titles would they search that are NOT obvious from
the resume content alone?"

These are the keywords that resume-driven generation systematically misses.
Examples by profile type:
  - Senior AI strategy consultant → "Forward Deployed Strategist",
    "Engagement Manager, AI", "Federal AI Strategy", "AI Coach",
    "Copilot Enablement Lead"
  - CPG route sales new grad → "Account Manager", "Route Manager",
    "Territory Sales Manager", "Category Analyst", "SDR"
  - Healthcare consultant → "Clinical Operations Manager",
    "Health System Strategy Lead", "Provider Engagement Manager"
  - Marketing manager → "Demand Gen Lead", "Lifecycle Marketing Manager",
    "GTM Strategy Manager"

These funnel/lateral keywords typically belong in Tier 2 (not Tier 1)
since they're recruiter-suggestions rather than user-named targets, but
they materially expand search coverage. Aim for 3-5 of these in every
keyword list.

============================================================
PROFILE GUIDANCE
============================================================
Be specific. Pull concrete details from the resume text + freeform context:
  - target_functions: exactly what kinds of work the candidate wants
                      (driven by freeform context, NOT resume current job)
  - target_industries: industries they're targeting (and excluding)
  - target_seniority: e.g., "Senior IC", "Manager", "Director"
  - years_experience: integer years of professional experience
  - technical_skills: list of specific tools/methods (max 25)
  - domain_expertise: subject-matter areas they're expert in
  - headline: one-sentence professional summary
  - negative_signals: things the candidate explicitly wants to AVOID
                       (cited from freeform context)
  - excluded_title_patterns: 5-15 short title-keyword tokens (1-3 words each,
                       lowercase) that should HARD-DROP a job at scrape-filter
                       time. Generate these by reading the resume + targets
                       and asking: "If a job title contains this word, would
                       this candidate be ineligible regardless of company?"

                       Examples by candidate type:
                       • Strategy consultant w/ no engineering: ["architect",
                         "engineer", "developer", "data scientist",
                         "ml researcher", "devops"]
                       • Software engineer transitioning to mgmt, no sales:
                         ["account executive", "sdr", "bdr",
                         "sales manager", "broker"]
                       • Marketing director: ["engineer", "data scientist",
                         "actuary", "underwriter"]
                       • Accountant w/ no public bg: ["auditor", "engineer",
                         "developer"]
                       • New-grad business student: ["engineer", "developer",
                         "physician", "rn", "registered nurse"]

                       RULES:
                       - Use ONLY title keywords that would actually appear in
                         job titles
                       - Each pattern matches as a whole word; "engineer"
                         drops "Senior Engineer" but NOT "Engagement Manager"

                       - **CRITICAL — NEVER exclude the candidate's own
                         function.** If the candidate IS an engineer, do NOT
                         add "engineer" — that would drop their target roles.
                         If they are a nurse, do NOT add "nurse." If they are
                         an analyst, do NOT add "analyst." The exclusion list
                         is for roles OUTSIDE the candidate's target function,
                         never within it.

                         Specifically forbidden combinations:
                           • Software/data/ML/environmental engineer profile
                             → MUST NOT include "engineer" or "developer"
                           • Nurse / clinician profile
                             → MUST NOT include "nurse" or "rn"
                           • Accountant / auditor profile
                             → MUST NOT include "accountant" or "auditor"
                           • Marketing / growth profile
                             → MUST NOT include "marketing"
                           • Sales / account management profile
                             → MUST NOT include "sales", "account executive",
                               "account manager"
                           • Product manager profile
                             → MUST NOT include "product manager", "pm"
                           • Consultant profile
                             → MUST NOT include "consultant"
                           • Designer / UX profile
                             → MUST NOT include "designer", "ux"

                         Apply the same logic for any other function: if the
                         word appears in the candidate's headline, target
                         functions, or current role, it CANNOT be in the
                         excluded list.

                       - When uncertain, OMIT — false drops cost more than
                         false passes
                       - Empty list is acceptable for true-generalist profiles

  - profile_tags: 5-10 short tags (single words or short phrases, lowercase
                       with underscores) describing the candidate's INDUSTRY,
                       FUNCTION, SKILLS, and DOMAIN. These power cross-tester
                       closed-loop learning: when a new candidate's search
                       runs, the system filters past observed (company, title)
                       pairs to those from candidates whose tags overlap with
                       theirs by 20%+. The overlapping titles then get
                       injected as "real titles seen in market for similar
                       candidates."

                       Tags should be specific enough to be meaningful but
                       general enough to allow some overlap. Examples by
                       candidate type:
                       • Senior AI strategy consultant at Booz Allen:
                         ["consulting", "federal", "ai", "enablement",
                         "governance", "change_management",
                         "policy_analysis", "data_analytics"]
                       • Business admin senior with 4yrs route sales at PepsiCo:
                         ["cpg", "retail", "sales", "operations",
                         "territory_management", "account_management",
                         "logistics", "finance"]
                       • Software engineer at a fintech startup:
                         ["software_engineering", "fintech", "backend",
                         "distributed_systems", "python", "kubernetes"]
                       • Tax accountant at Big 4:
                         ["accounting", "tax", "big_4", "audit",
                         "financial_analysis"]
                       • CRE analyst:
                         ["commercial_real_estate", "real_estate",
                         "investment_analysis", "underwriting", "finance"]

                       RULES:
                       - 5-10 tags per profile (more is noise, fewer is
                         too narrow for matching)
                       - Use lowercase with underscores: "change_management"
                         not "Change Management"
                       - Mix industry tags + function tags + skill tags
                       - Avoid hyper-specific (good: "fintech"; too narrow:
                         "consumer_credit_card_underwriting")

Respond with strict JSON matching the schema.
"""


_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "years_experience": {"type": "integer"},
        "target_seniority": {"type": "string"},
        "target_functions": {"type": "array", "items": {"type": "string"}},
        "target_industries": {"type": "array", "items": {"type": "string"}},
        "technical_skills": {"type": "array", "items": {"type": "string"}},
        "domain_expertise": {"type": "array", "items": {"type": "string"}},
        "soft_skills": {"type": "array", "items": {"type": "string"}},
        "negative_signals": {"type": "array", "items": {"type": "string"}},
        "excluded_title_patterns": {"type": "array", "items": {"type": "string"}},
        "profile_tags": {"type": "array", "items": {"type": "string"}},
        # v0.1.6 stability fix: enforce a MINIMUM array size via schema
        # so keyword counts don't drift below a usable floor across runs
        # (we observed 12-then-10 search_terms variance from the same
        # resume, which silently changed search results downstream).
        # Schema-level minItems is a hard floor — far more reliable than
        # prompt instructions which the LLM sometimes ignores.
        #
        # Deliberately NO maxItems: if the model identifies 15 strong
        # keywords from a particularly rich resume, capping at 12 would
        # discard signal we want. The AI's job is to determine the right
        # upper bound based on what the resume actually warrants.
        "keywords_tier_1": {"type": "array", "items": {"type": "string"}, "minItems": 6},
        "keywords_tier_2": {"type": "array", "items": {"type": "string"}, "minItems": 6},
        "keywords_tier_3": {"type": "array", "items": {"type": "string"}, "minItems": 4},
        # v0.1.8: bumped floor 10 → 11 to compensate for hybrid location
        # filter tightening. v0.2.0: bumped 11 → 14 to compensate for the
        # cumulative tightening across location/liveness/hybrid fixes
        # (each rejects more roles upstream, so we cast a wider net at
        # the top to keep qualifying counts healthy). The Sonnet/Opus
        # cascade aggressively filters noise from weaker keywords, so
        # casting wider here is the right tradeoff — ~25% more roles
        # scraped, ~25% more cost ($1.10-1.20 vs $0.93), and meaningful
        # extra STRONG matches surfaced via embedding pre-filter that
        # the narrower set would have missed.
        "search_terms": {"type": "array", "items": {"type": "string"}, "minItems": 14},
        "resume_emphases": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string"},
                    "emphasis": {"type": "string"},
                    "headline": {"type": "string"},
                },
                "required": ["filename", "emphasis"],
            },
        },
    },
    "required": [
        "headline", "target_functions", "target_industries",
        "target_seniority", "technical_skills",
        "keywords_tier_1", "keywords_tier_2",
    ],
}


def _pull_market_intelligence_for_prompt(
    user_preferences: dict,
) -> str:
    """Build the "real titles seen in market" injection block for the keyword
    generator's prompt. Uses the most recent run's profile_tags (own + sibling
    testers') to filter what's relevant to this candidate.

    Returns an empty string if no archive is configured or no prior data exists
    (first-time users get no injection — they still get a great keyword list,
    just not augmented by market intelligence).
    """
    try:
        from backend.storage import (
            get_archive, market_titles_for_keyword_prompt,
        )
        archive = get_archive()
    except Exception:
        return ""

    # Pull this user's most recent profile_tags from any prior run.
    # On their first ever run this returns nothing and we skip injection.
    try:
        with archive._cursor() as cur:
            cur.execute("""
                SELECT profile_tags_json FROM runs
                WHERE profile_tags_json IS NOT NULL AND profile_tags_json != '[]'
                ORDER BY started_at DESC LIMIT 1
            """)
            row = cur.fetchone()
        if not row or not row["profile_tags_json"]:
            return ""
        prior_tags = json.loads(row["profile_tags_json"]) or []
    except Exception:
        return ""

    if not prior_tags:
        return ""

    # Get own contributions (full set) + sibling-filtered contributions
    try:
        own = archive.get_self_contributions()
        own_dicts = [
            {
                "company": r["company"], "job_title": r["job_title"],
                "score": r["score"],
                "profile_tags": json.loads(r["profile_tags_json"] or "[]"),
            }
            for r in own
        ]
    except Exception:
        own_dicts = []

    try:
        market_entries = market_titles_for_keyword_prompt(
            archive.folder,
            candidate_tags=prior_tags,
            own_contributions=own_dicts,
            max_titles=50,
            min_overlap=0.20,
        )
    except Exception:
        return ""

    if not market_entries:
        return ""

    lines = ["", "============================================================",
             "REAL TITLES OBSERVED IN MARKET (closed-loop signal)",
             "============================================================",
             "Below are job titles that have been seen at real companies for",
             "candidates with overlapping backgrounds (≥20% profile-tag overlap).",
             "These represent ACTUAL roles posted in the market, not hypothetical.",
             "Use them as vocabulary signals when generating keywords — favor",
             "title patterns the market actually uses, especially when scored well.",
             "",
             "Format: [score] Title — Company  (contributing tags)"]
    for e in market_entries[:50]:
        score = e.get("score", 0)
        company = e.get("company", "")
        title = e.get("job_title", "")
        tags = ", ".join(e.get("profile_tags", [])[:5])
        lines.append(f"  [{score}] {title} — {company}  ({tags})")
    lines.append("")
    return "\n".join(lines)


_DANGEROUS_GENERIC_EXCLUSIONS = {
    # Pure seniority words — these are not functional exclusions, they would
    # over-prune the search for roles at any level. The LLM sometimes
    # generates these by mistake.
    "senior", "junior", "associate", "principal", "lead", "head", "chief",
    "vp", "officer", "intern", "trainee", "entry", "entry-level",
    # Pure role-pattern words — almost every functional area uses these
    # nouns. Excluding them drops a huge swath of legitimate roles.
    "manager", "director", "specialist", "consultant", "analyst",
    "advisor", "executive", "coordinator", "administrator",
}


# Function-name synonyms. If the candidate's headline / target_functions /
# tier-1 keywords contain ANY of a pattern's synonyms, that pattern is also
# considered self-excluding. This catches the common LLM mistake of adding
# "developer" to a software engineer's excluded list, "lawyer" to a corporate
# attorney's, etc. — semantically equivalent function names that the LLM
# treats as different.
_FUNCTION_SYNONYMS: dict[str, set[str]] = {
    # Software / tech
    "engineer": {"developer"},
    "developer": {"engineer"},
    # Legal
    "lawyer": {"attorney", "counsel"},
    "attorney": {"lawyer", "counsel"},
    "counsel": {"lawyer", "attorney"},
    # Medical
    "physician": {"doctor"},
    "doctor": {"physician"},
    "nurse": {"rn"},
    "rn": {"nurse"},
    "veterinarian": {"vet", "dvm"},
    "vet": {"veterinarian", "dvm"},
    "dvm": {"veterinarian", "vet"},
    # Education
    "teacher": {"educator", "instructor"},
    "educator": {"teacher", "instructor"},
    "instructor": {"teacher", "educator"},
    # Accounting
    "accountant": {"cpa"},
    "cpa": {"accountant"},
    # Culinary
    "chef": {"cook"},
    "cook": {"chef"},
    # Real estate
    "realtor": {"agent"},
    # Tech roles
    "scientist": {"researcher"},
    "researcher": {"scientist"},
}


def _normalize_pattern(s: str) -> str:
    """Lowercase, strip non-alphanum to spaces, collapse whitespace."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9+ ]+", " ", s)  # keep + for c++ etc.
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _strip_self_excludes(
    *,
    patterns: list[str],
    headline: str,
    target_functions: list[str],
    tier1_keywords: list[str],
) -> list[str]:
    """Drop any excluded_title_pattern that would drop the candidate's own
    target roles. Defense-in-depth against the LLM adding "engineer" for an
    engineer profile, "nurse" for a nurse profile, etc.

    Strip rules (in order):
      1. Empty / whitespace-only / None → skip.
      2. After normalization (lowercase, strip punctuation, collapse spaces),
         duplicates of an already-kept pattern → skip.
      3. Pattern is a "dangerous generic" (pure seniority noun like "senior"
         or pure role-pattern noun like "manager") → strip. These over-prune
         even when not in the candidate's headline.
      4. Pattern whole-word-matches the candidate's own language
         (headline + target_functions + tier1 keywords combined) → strip.
         Whole-phrase match for multi-word patterns. This is the core check
         from the audit: a software engineer must not have "engineer" as an
         exclusion because `\\bengineer\\b` would match their target role
         titles like "Senior Software Engineer".

    Keep rules:
      - Multi-word patterns that don't whole-phrase match the candidate's
        language are kept even if they share a noun with the candidate
        (e.g. a software engineer can intentionally exclude "data engineer";
        the pattern "data engineer" doesn't whole-phrase match
        "software engineer").
      - Patterns referring to OTHER functions are kept (e.g. an engineer
        excluding "physician" or "broker").

    Returns a deduped, normalized list capped at 20 patterns.
    """
    if not patterns:
        return []

    # Build the haystack from candidate's own target language.
    parts = [headline or ""] + list(target_functions or []) + list(tier1_keywords or [])
    haystack = " ".join(_normalize_pattern(p) for p in parts if p).strip()

    # Augment haystack with synonyms of every word it contains. This catches
    # cases where the LLM adds "developer" to excluded patterns for a
    # software engineer (haystack has "engineer" but not "developer" — the
    # synonym expansion adds "developer" so the pattern strips correctly).
    if haystack:
        words_in_haystack = set(haystack.split())
        synonyms_to_add: set[str] = set()
        for w in words_in_haystack:
            synonyms_to_add.update(_FUNCTION_SYNONYMS.get(w, set()))
        if synonyms_to_add:
            haystack = haystack + " " + " ".join(synonyms_to_add)

    seen: set[str] = set()
    out: list[str] = []

    for raw in patterns:
        if raw is None:
            continue
        if not isinstance(raw, str):
            continue
        p = _normalize_pattern(raw)
        if not p:
            continue
        if p in seen:
            continue
        seen.add(p)

        # Rule 3: dangerous generics are always stripped.
        if p in _DANGEROUS_GENERIC_EXCLUSIONS:
            continue

        # Rule 4: whole-phrase whole-word match against candidate's language.
        if haystack and re.search(rf"\b{re.escape(p)}\b", haystack):
            continue

        out.append(p)
        if len(out) >= 20:
            break

    return out


async def build_profile_from_resumes(
    *,
    resume_paths: list[str | Path],
    user_preferences: Optional[dict] = None,
    client: Optional[LLMClient] = None,
    progress: Optional[ProgressCallback] = None,
) -> CandidateProfile:
    """Parse resumes, ingest into LLM, return unified CandidateProfile + keywords.

    user_preferences (all optional) accepts:
      - salary_minimum: int
      - work_arrangements: list[str] from {"remote", "hybrid", "on-site"}
      - acceptable_locations: list[str]
      - excluded_locations: list[str]
    """
    user_preferences = user_preferences or {}
    client = client or get_llm_client()

    def _report(pct: int, stage: str) -> None:
        if progress:
            try:
                progress(pct, stage)
            except Exception:
                pass

    _report(3, "Reading your resume(s)")

    # 1. Parse every resume into plain text
    parsed_resumes: list[tuple[str, str]] = []
    for path in resume_paths:
        path = Path(path)
        text = parse_resume(path)
        if text:
            parsed_resumes.append((path.name, text))

    if not parsed_resumes:
        raise ValueError("No readable resume content. Check file paths and formats.")

    _report(8, "Resumes parsed — preparing AI generations")

    # 2. Build the prompt
    user_message_parts = [
        f"Number of resumes uploaded: {len(parsed_resumes)}",
        "",
    ]
    for i, (filename, text) in enumerate(parsed_resumes, 1):
        # Truncate each resume to 8K chars to stay under token limits
        excerpt = text[:8000]
        user_message_parts.append(f"=== RESUME {i}: {filename} ===\n{excerpt}\n")

    if user_preferences:
        user_message_parts.append("\n=== USER PREFERENCES ===")
        for k, v in user_preferences.items():
            user_message_parts.append(f"  {k}: {v}")

    # Inject closed-loop market intelligence (sibling-folder + own past runs).
    # Empty string for first-time users; a block of "real titles seen for
    # similar candidates" for returning users / users whose audit folder is
    # synced alongside other testers' folders.
    market_block = _pull_market_intelligence_for_prompt(user_preferences or {})
    if market_block:
        user_message_parts.append(market_block)

    prompt = "\n".join(user_message_parts)

    # 3. STAGE 1 — Parallel diversity sampling
    # Run 3x Gemini Pro generations at temperature 0.5 (self-consistency)
    # AND 1x Claude Opus generation (cross-model). All 4 in parallel.
    if hasattr(client, "current_stage"):
        client.current_stage = "profile_build"  # type: ignore[attr-defined]

    _report(12, "Running 4 AI models in parallel")

    candidate_drafts = await _stage1_diversity_sampling(
        gemini_client=client,
        prompt=prompt,
        progress=lambda done, total: _report(
            12 + int(58 * (done / max(1, total))),
            f"AI generation {done} of {total} complete"
        ),
    )

    if not candidate_drafts:
        raise RuntimeError(
            "Profile builder produced no usable drafts. "
            "Check Gemini API access and try again."
        )

    _report(72, "Synthesizing the best result")

    # 4. STAGE 2 — Final merge / synthesis
    # Claude Opus (preferred) or Gemini Pro merges all candidate drafts into
    # a single best consensus. Opus is particularly good at meta-tasks like
    # "synthesize 4 expert opinions into the best output."
    data = await _stage2_synthesize(
        gemini_client=client,
        candidate_drafts=candidate_drafts,
        resumes=parsed_resumes,
        user_preferences=user_preferences,
    )

    _report(95, "Finalizing your keyword list")

    # 4. Build the CandidateProfile
    keywords: list[Keyword] = []
    for kw in data.get("keywords_tier_1", []):
        keywords.append(Keyword(text=str(kw).strip(), tier=1))
    for kw in data.get("keywords_tier_2", []):
        keywords.append(Keyword(text=str(kw).strip(), tier=2))
    for kw in data.get("keywords_tier_3", []):
        keywords.append(Keyword(text=str(kw).strip(), tier=3))

    resume_meta: list[ResumeMetadata] = []
    for emph in data.get("resume_emphases", []):
        if isinstance(emph, dict):
            resume_meta.append(ResumeMetadata(
                filename=str(emph.get("filename", ""))[:100],
                emphasis=str(emph.get("emphasis", ""))[:200],
                headline=str(emph.get("headline", ""))[:200] or None,
            ))
    if not resume_meta:
        # Fallback: at least record the filenames
        for filename, _ in parsed_resumes:
            resume_meta.append(ResumeMetadata(filename=filename))

    # Build excluded_title_patterns then strip any pattern that overlaps with
    # the candidate's own target function — defense-in-depth against the LLM
    # adding "engineer" for engineers, "nurse" for nurses, etc.
    raw_excluded = [str(x).strip().lower() for x in data.get("excluded_title_patterns", []) if x][:20]
    headline_lower = str(data.get("headline", "")).lower()
    target_funcs_lower = [str(x).strip().lower() for x in data.get("target_functions", []) if x]
    t1_lower = [str(x).strip().lower() for x in data.get("keywords_tier_1", []) if x]
    safe_excluded = _strip_self_excludes(
        patterns=raw_excluded,
        headline=headline_lower,
        target_functions=target_funcs_lower,
        tier1_keywords=t1_lower,
    )

    # Parse search_terms (NEW v0.1.4). Bounded 5-15 entries, 2-5 words each.
    raw_search_terms = data.get("search_terms") or []
    search_terms: list[str] = []
    for st in raw_search_terms:
        s = str(st or "").strip()
        if not s:
            continue
        # Sanity bound on phrase length — search terms should be 1-5 words
        if 1 <= len(s.split()) <= 5:
            search_terms.append(s)
    search_terms = search_terms[:15]

    profile = CandidateProfile(
        headline=str(data.get("headline", ""))[:300],
        years_experience=int(data["years_experience"]) if data.get("years_experience") else None,
        target_seniority=str(data.get("target_seniority", ""))[:50] or None,
        target_functions=[str(x).strip() for x in data.get("target_functions", []) if x][:30],
        target_industries=[str(x).strip() for x in data.get("target_industries", []) if x][:20],
        technical_skills=[str(x).strip() for x in data.get("technical_skills", []) if x][:30],
        domain_expertise=[str(x).strip() for x in data.get("domain_expertise", []) if x][:20],
        soft_skills=[str(x).strip() for x in data.get("soft_skills", []) if x][:15],
        negative_signals=[str(x).strip() for x in data.get("negative_signals", []) if x][:15],
        excluded_title_patterns=safe_excluded,
        profile_tags=[str(x).strip().lower() for x in data.get("profile_tags", []) if x][:12],
        salary_minimum=user_preferences.get("salary_minimum"),
        work_arrangements=user_preferences.get("work_arrangements", []),
        acceptable_locations=user_preferences.get("acceptable_locations", []),
        acceptable_location_radii=user_preferences.get("acceptable_location_radii", []),
        excluded_locations=user_preferences.get("excluded_locations", []),
        keywords=keywords,
        search_terms=search_terms,
        resumes=resume_meta,
    )

    return profile


def keywords_to_search_strings(profile: CandidateProfile, max_tier: int = 2) -> list[str]:
    """Return the list of search strings to feed scrapers' upstream APIs.

    v0.1.4: prefer profile.search_terms (broad 2-3 word phrases) when
    populated. Fall back to profile.keywords for old profiles built before
    the search-terms split. The fallback ensures back-compat — old saved
    profiles work without rebuild.
    """
    if profile.search_terms:
        return list(profile.search_terms)
    return [kw.text for kw in profile.keywords if kw.tier <= max_tier]


# ----------------------------------------------------------------------------
# Audit/refine pass — second Pro call critiques and revises the draft profile
# ----------------------------------------------------------------------------

PROFILE_AUDIT_PROMPT = """\
You are a senior career analyst auditing another analyst's work.

You will receive a draft candidate profile + keyword list, plus the original
resumes and user preferences/freeform context. Your job is to critique it
rigorously, then output a REVISED final version that fixes the issues.

Apply these audit principles:

============================================================
AUDIT CHECKLIST
============================================================

1. KEYWORDS MUST MATCH EMPLOYER JOB TITLES, NOT RESUME LANGUAGE.
   Are the keywords what employers actually post? Or are they paraphrasing
   the candidate's resume? Title-naming patterns (Lead/Manager/Specialist/
   Director/Consultant) should be applied generously.

2. TITLE-NAMING VARIATION COVERAGE.
   For each core concept, are 2-4 naming variants present? Missing variants
   leave whole job postings unsearched. If "X Specialist" is in Tier 1,
   "X Lead" and "X Manager" probably should be too (subject to candidate
   seniority).

3. NEGATIVE-SIGNAL GATING.
   Does the user state they're moving AWAY from anything? If so, did the
   draft put related keywords in Tier 1 anyway? (Common mistake: candidate
   says "moving out of federal," draft puts "Federal Strategy" in Tier 1.)
   Move those to Tier 3 or omit. Resume describes current job; keywords
   describe target.

4. BROAD-NOISE SUPPRESSION.
   Are there overly generic terms that will pollute results?
     - "Management Consultant" (without further qualification)
     - "Project Management Consultant"
     - "Business Intelligence Consultant"
     - Single-word generics
   These should be omitted UNLESS the user's context explicitly names them.
   Ask: would this term return many off-target roles?

5. WRONG-FUNCTION KEYWORDS.
   Did the draft include keywords that describe a different career path
   than the candidate is actually pursuing? E.g., a strategy candidate with
   "ML Engineer" in Tier 2, or a data engineer with "Marketing Manager"?

5a. AI-DOMAIN OVER-CLAIM (CRITICAL — v0.3.4).
   "Uses AI tools" ≠ "targets AI roles." Re-classify the resume's AI
   evidence into:
     • BUCKET A — tool usage (Copilot in skills section, "leveraged AI",
       "familiar with ChatGPT") → STRIP all AI keywords from every tier,
       strip "AI Strategy" / "AI Adoption" / "AI Governance" /
       "AI Enablement" / "AI Program Management" from target_functions,
       strip "AI" / "Technology" from target_industries.
     • BUCKET B — domain adjacent (piloted Copilot for own team,
       AI working-group member, authored internal AI usage guidelines)
       → AI keywords belong in TIER 2 ONLY if freeform context asks for
       AI roles. Otherwise demote / drop.
     • BUCKET C — AI as core function (title contains AI/ML, owned an
       AI roadmap, multi-year AI program work) → AI keywords in Tier 1
       are correct.
   When uncertain, assume BUCKET A. False AI promotion produces 100+
   off-target AI roles for non-AI candidates.

5b. INDUSTRY-OVERREACH (CRITICAL — v0.3.4).
   Did the draft include target_industries the candidate doesn't
   actually target? Common over-includes:
     • "Technology" added because the resume mentions using Copilot,
       Salesforce, ServiceNow, or any SaaS tool
     • An industry that appears as a single client / vendor mention
       (a healthcare consultant with one real estate client should NOT
       have "Real Estate" in target_industries)
     • An old internship in an unrelated field
   Strip industries unless the candidate has multi-year tenure OR
   freeform context names that industry as a target.

6. TIER ASSIGNMENT.
   - Tier 1 should have 10-15 keywords representing the candidate's
     EXACT target roles
   - Tier 2: 10-15 adjacent/stretch concepts
   - Tier 3: 8-12 broader exploration
   - Are the tier counts reasonable?
   - Are the most direct target keywords actually in Tier 1?

7. MISSING KEYWORDS.
   Read the freeform context carefully. Did the draft miss any role types
   the user named directly? Did it miss Lead/Manager/Specialist variants
   of concepts that ARE in the draft?

============================================================
OUTPUT INSTRUCTIONS
============================================================

Output the REVISED profile in the same JSON schema as the input. Apply
ALL audit principles. Don't be conservative — make real changes.

Specifically:
  - Move keywords between tiers as needed
  - Add missing title variants (Lead/Manager/Specialist) for core concepts
  - Remove broad-noise terms that don't fit the candidate
  - Move concepts the candidate is moving AWAY from to lower tiers
  - Keep total keywords in 30-40 range

If the draft is already excellent, you can keep most of it. But always
explicitly ensure all 7 principles above pass.

Respond with strict JSON in the same schema as the input.
"""


# ----------------------------------------------------------------------------
# Stage 1 — Parallel diversity sampling
# ----------------------------------------------------------------------------

async def _stage1_diversity_sampling(
    *,
    gemini_client,
    prompt: str,
    progress: Optional[Callable[[int, int], None]] = None,
) -> list[tuple[str, dict]]:
    """Run 3x Gemini Pro samples + 1x Claude Opus in parallel.

    Returns list of (label, draft_dict) tuples. Failed calls are skipped.
    Progress callback is fired (done, total) each time a sample completes.

    v0.3.5: Builds a Gemini context cache for the ~7,500-token system prompt
    once, then passes the cache handle to all 3 Gemini Pro samples. The
    system prompt clears Gemini Pro's 4,096-token caching minimum, and the
    repeated calls within ~1 minute (well under the 1h TTL) hit the cache
    each time — saves 50% of input cost on the prompt body across the 3
    samples (~$0.04-0.06/build at current pricing). Falls back to inline
    system prompt if cache creation fails.
    """
    # v0.3.5: cache enabled (fix verified live on ship night).
    # PROFILE_BUILDER_PROMPT is ~7,500 tokens — well above Pro's
    # 4,096-token cache minimum. Three Gemini Pro draft calls share
    # the same cache → ~50% input-cost reduction on profile build
    # (~$0.05/build savings).
    #
    # Bug history: prior in-progress work passed the CachedContent
    # object directly to the SDK, which raised Pydantic ValidationError.
    # Fix landed at llm_client.py:257 — extracts .name string when an
    # object is passed. The try/except below is the safety net for
    # transient API errors.
    cached_handle = None
    try:
        cached_handle = await gemini_client.create_cache(
            model=config.PROFILE_BUILD_MODEL,
            system=PROFILE_BUILDER_PROMPT,
            ttl_seconds=3600,
        )
    except Exception:
        cached_handle = None

    tasks: list[Any] = []
    labels: list[str] = []

    # 3 Gemini Pro samples at temp 0.5 for diversity
    for i in range(config.PROFILE_BUILD_SAMPLES):
        tasks.append(
            _gemini_sample(
                gemini_client, prompt, sample_idx=i,
                cached_content=cached_handle,
            )
        )
        labels.append(f"Gemini-Pro-{i + 1}")

    # 1 Claude Opus sample (cross-model). Anthropic caching is a separate
    # mechanism — not piggybacking on Gemini's cache handle.
    if config.PROFILE_CONSENSUS_ENABLED:
        tasks.append(_claude_sample(gemini_client, prompt))
        labels.append("Claude-Opus")

    total = len(tasks)
    drafts: list[tuple[str, dict]] = []
    completed = 0

    # asyncio.as_completed lets us fire progress events as each sample finishes
    for coro in asyncio.as_completed(tasks):
        try:
            result = await coro
        except Exception:
            result = None
        completed += 1
        if isinstance(result, dict):
            drafts.append((f"sample_{completed}", result))
        if progress:
            try:
                progress(completed, total)
            except Exception:
                pass

    return drafts


async def _gemini_sample(
    client,
    prompt: str,
    sample_idx: int = 0,
    cached_content: Any = None,
) -> Optional[dict]:
    """Single Gemini Pro generation at the configured temperature.

    cached_content (v0.3.5): when provided, the system prompt is served from
    Gemini's context cache (~50% input-token discount) instead of being sent
    inline on every call. Pass None to disable caching for this call.
    """
    try:
        # Each sample uses its own current_stage tag for cost-tracker visibility
        original_stage = getattr(client, "current_stage", "profile_build")
        client.current_stage = f"profile_build_gemini_{sample_idx + 1}"  # type: ignore[attr-defined]
        response = await client.complete(
            model=config.PROFILE_BUILD_MODEL,
            # When the cache is in play, drop the inline system instruction —
            # the cache handle already carries it. (Mirrors the stage2_triage
            # pattern for the same reason.)
            system=None if cached_content else PROFILE_BUILDER_PROMPT,
            user=prompt,
            max_output_tokens=8192,
            temperature=config.PROFILE_BUILD_TEMPERATURE,
            json_schema=_RESPONSE_SCHEMA,
            cached_content=cached_content,
            thinking_budget=config.PROFILE_BUILD_THINKING,
        )
        client.current_stage = original_stage  # type: ignore[attr-defined]
        data = response.parsed_json
        if isinstance(data, dict):
            return data
        if response.text:
            try:
                return json.loads(response.text)
            except json.JSONDecodeError:
                pass
    except Exception:
        pass
    return None


async def _claude_sample(gemini_client, prompt: str) -> Optional[dict]:
    """Single Claude Opus generation. Returns None if Anthropic key missing."""
    try:
        from backend.scoring.anthropic_client import get_anthropic_client
        anthropic = get_anthropic_client()
        if anthropic is None:
            return None
        anthropic.current_run_id = getattr(gemini_client, "current_run_id", None)
        anthropic.current_stage = "profile_build_claude"
        response = await anthropic.complete(
            model=config.PROFILE_ANTHROPIC_MODEL,
            system=PROFILE_BUILDER_PROMPT + (
                "\n\nIMPORTANT: Output strictly valid JSON only. "
                "No markdown fences. The JSON object must contain "
                "all fields shown in the response schema."
            ),
            user=prompt,
            max_output_tokens=8192,
            temperature=config.PROFILE_BUILD_TEMPERATURE,
            thinking_budget=config.PROFILE_ANTHROPIC_THINKING,
        )
        if isinstance(response.parsed_json, dict):
            return response.parsed_json
    except Exception:
        pass
    return None


# ----------------------------------------------------------------------------
# Stage 2 — Synthesize the best output from all candidate drafts
# ----------------------------------------------------------------------------

PROFILE_SYNTHESIS_PROMPT = """\
You are the senior reviewer synthesizing multiple expert keyword lists for
the same candidate. You will receive 3-4 independently-generated drafts
(some from Gemini Pro at varied temperatures, one from Claude Opus). Your
job is to merge them into the SINGLE BEST final list.

============================================================
SYNTHESIS PRINCIPLES
============================================================

1. CONSENSUS = HIGH CONFIDENCE
   Keywords appearing in 3+ drafts are almost certainly correct. KEEP.
   Keywords appearing in 2 drafts are likely correct. KEEP at appropriate tier.
   Keywords in only 1 draft — judge case-by-case using all the principles
   below.

2. MISSING TITLE-NAMING VARIANTS — FILL THEM IN
   For each Tier 1 concept (e.g., "AI Enablement"), ensure ALL reasonable
   employer-side title variants are present:
     - {Concept} Lead
     - {Concept} Manager
     - {Concept} Specialist
     - {Concept} Consultant
     - {Concept} Director (if seniority fits)
     - Senior {Concept}
     - Head of {Concept} (if seniority fits)

   Different drafts will name different variants. Your output should UNION
   all reasonable variants. Coverage matters — each title returns a different
   set of job postings.

3. CORE PRINCIPLE: KEYWORDS MATCH EMPLOYER JOB TITLES, NOT RESUME LANGUAGE
   Re-check every keyword. Is it an employer-side title? Or is it
   resume-summary language? Drop the latter.

4. NEGATIVE-SIGNAL GATING
   If the user's freeform context says "moving away from X" or "avoid Y",
   demote those concepts to Tier 3 or omit. The resume describes their
   current job; keywords describe the next role.

5. BROAD-NOISE SUPPRESSION
   Drop generic terms like "Management Consultant", "Business Intelligence
   Consultant", "Project Management Consultant" unless the user's context
   explicitly names them as targets. Different from Principle #4 — these
   are noise-prone, not necessarily contradictory.

6. WRONG-FUNCTION ELIMINATION
   Re-check that all keywords align with the candidate's actual target.
   A strategy candidate shouldn't have "ML Engineer" in Tier 2.
   A data engineer shouldn't have "Marketing Manager" in Tier 2.

6a. AI-DOMAIN GUARD (CRITICAL — v0.3.4)
    "Uses AI tools" ≠ "targets AI roles." If the resume's only AI evidence
    is BUCKET A — tool usage like "used Copilot to draft emails," "AI
    tools" or "Generative AI" in the skills section, "leveraged AI to
    streamline X" — DO NOT include "AI Strategy", "AI Adoption", "AI
    Enablement", "AI Governance", "AI Program Manager", "AI Operations
    Manager", "Generative AI Consultant", "Copilot Lead" in any tier.
    Also drop "AI" / "Technology" from target_industries when only
    BUCKET A evidence is present.

    BUCKET A (tool usage — does NOT justify AI keywords):
      • "Used Copilot / ChatGPT / Claude / Gemini for [X]"
      • "Familiar with AI tools" / "AI proficiency" / "GenAI" in skills
      • "AI-assisted [report / analysis / writing]"
      • Generic AI certificate or short course

    BUCKET B (domain adjacent — Tier 2 ONLY if freeform asks for AI):
      • Piloted Copilot rollout for own team
      • Member of internal AI working group
      • Authored AI usage guidelines for own department

    BUCKET C (AI as core function — Tier 1 AI keywords appropriate):
      • Title contains "AI" / "ML" / "GenAI" / "Data Science"
      • Owned AI roadmap / strategy / adoption program at a company
      • Multi-year AI program management or AI consulting track record

    WHEN UNCERTAIN, ASSUME BUCKET A. False AI promotion produces 100+
    AI roles a non-AI candidate is unqualified for and pollutes their
    entire run. The cost of erroneously omitting AI keywords from a
    true AI candidate (Ziad-style) is much smaller — they re-add via
    freeform context on next run.

6b. INDUSTRY-OVERREACH GUARD (CRITICAL — v0.3.4)
    The same caution applies to target_industries. A candidate with
    ONE client in real estate is NOT targeting real estate. A candidate
    who used a SaaS tool is NOT targeting Technology. Only include
    industries the candidate explicitly targets in freeform context OR
    has multi-engagement / multi-year experience in (not single-client,
    not tooling).

    Concretely, DO NOT add industries based on:
      • A single client engagement in that industry
      • Working with vendors / SaaS tools from that industry
      • A coursework mention or one-line resume bullet
      • An internship over 3 years ago in a different field

    DO add industries based on:
      • Multi-year tenure at a company in that industry
      • The candidate's freeform context naming the industry
      • Multiple distinct engagements proving sustained domain expertise

7. TIER COUNTS — PRIMARY vs SECONDARY TARGET DISCIPLINE
   When the candidate names multiple target paths, do NOT spread Tier 1
   equally across all of them. Identify the PRIMARY 1-2 targets (those
   the resume most strongly substantiates with experience + accomplishments)
   and weight Tier 1 toward those. Secondary targets (stated but lighter
   resume evidence, or coursework-only) belong in Tier 2.

   RESUME EVIDENCE GUARD: Demote any Tier 1 keyword whose expected skill
   stack is NOT supported by actual experience on the resume. Examples:
     - FP&A Analyst T1 → T2 if resume has finance coursework but no
       modeling/variance/budgeting work
     - Supply Chain Analyst T1 → T2 if resume shows logistics-on-a-route
       but no ERP/SCM/procurement credentials
     - Data Analyst T2 → T3/omit if resume has Excel + Salesforce reporting
       but no SQL/Python/Tableau
     - Head of {X} T1 → T2/T3 if candidate lacks Director+ management track

   Target tier counts:
   - Tier 1: 12-18 keywords (PRIMARY targets, all variants)
   - Tier 2: 10-15 keywords (secondary targets + adjacent + funnel/lateral)
   - Tier 3: 8-12 keywords (broader exploration + stretch/credentials-gap)
   - Total: 30-45 keywords. Quality over quantity.

8. TITLE-PATTERN MARKET INTELLIGENCE (apply firmly, field-agnostic)
   PROMOTE these to Tier 1 when matching the candidate's target — they
   consistently produce good matches across MANY fields, not just AI:
     - "{Concept} Operations Manager / Lead" — modern tactical role naming
     - "{Concept} Adoption Lead/Manager" — enablement work
     - "{Concept} Change Management Lead" — transformation roles
     - "{Concept} Program Manager" — many companies use this instead of
       "Lead"; often missed
     - "{Concept} Engagement Manager" / "{Concept} Deployment Manager" —
       common at consulting firms across industries
     - "{Concept} Coach" / "{Concept} Advisor"

   DEMOTE these from Tier 1 (sound prestigious but usually mismatch):
     - "Head of {anything}" — requires Director+ management background
     - "{anything} Product Manager" — different function unless candidate
       has PM background
     - "{anything} Analyst" / "Business Analyst" — title downgrade for
       senior candidates
     - "{anything} Solutions Architect" / "{anything} Engineer" —
       technical engineering, only T1 for technical candidates

   DOMAIN NICHES (only if candidate's field clearly matches):
     - Microsoft Copilot work: "Copilot enablement", "GenAI enablement"
     - Big 4/audit: "Senior Manager, {Practice}", "Audit Manager"
     - Healthcare: "Clinical {Concept} Manager"
     - Marketing/growth: "Demand Gen Manager", "Growth Marketing Lead"
     - Supply chain: "Supply Chain Manager", "Logistics Ops Lead"
     - CPG/retail/distribution/route sales: "Account Manager",
       "Key Account Manager", "Territory Sales Manager", "Route Manager",
       "District Manager", "Field Operations Manager", "Category Manager",
       "Category Analyst", "Buyer", "Trade Marketing Analyst",
       "Pricing Analyst", "SDR" (early-career)
     - Federal / public sector / government contracting (if NOT explicitly
       leaving): "Federal {Concept} Consultant", "Public Sector {Concept}",
       "{Concept} Modernization Consultant", "Forward Deployed Strategist"
     - Early-career / new grad with strong GPA: "{Function} Leadership
       Program", "{Function} Rotational Program", "Management Trainee"

   Calibrate niches to the candidate's actual field — don't apply ones
   they aren't targeting.

9. RECRUITER-PERSPECTIVE FUNNEL/LATERAL KEYWORDS
   Before finalizing, ask: "What 3-5 niche/funnel/lateral titles would a
   specialized RECRUITER in this candidate's industry search that are
   NOT obvious from the resume content alone?" Add those to Tier 2 if
   missing from all drafts. The drafts will systematically miss these
   because each individual model generates from resume content rather
   than from market knowledge of recruiter search behavior.

8. PROFILE FIELDS
   For headline, target_functions, technical_skills, etc. — synthesize the
   best information across drafts. Don't just pick one. Where drafts
   disagree, pick the version that most matches the candidate's stated
   freeform context.

============================================================
OUTPUT
============================================================

Output the FINAL synthesized profile in the SAME JSON schema as the inputs.
This is the user's actual profile. Be thorough. Be rigorous.

DO NOT regress — your output should be at least as comprehensive as any
single draft.
"""


async def _stage2_synthesize(
    *,
    gemini_client,
    candidate_drafts: list[tuple[str, dict]],
    resumes: list[tuple[str, str]],
    user_preferences: dict,
) -> dict:
    """Synthesize a final profile from multiple candidate drafts."""
    parts = ["ORIGINAL CANDIDATE INPUTS:"]
    for filename, text in resumes:
        parts.append(f"=== {filename} ===\n{text[:5000]}")

    if user_preferences.get("freeform_context"):
        parts.append("\n=== USER FREEFORM CONTEXT ===")
        parts.append(user_preferences["freeform_context"])

    if user_preferences:
        parts.append("\n=== USER PREFERENCES ===")
        for k, v in user_preferences.items():
            if k != "freeform_context":
                parts.append(f"  {k}: {v}")

    parts.append(f"\n=== CANDIDATE DRAFTS ({len(candidate_drafts)} total) ===")
    for label, draft in candidate_drafts:
        parts.append(f"\n--- DRAFT: {label} ---")
        parts.append(json.dumps(draft, indent=2))

    user_message = "\n".join(parts)

    # Prefer Claude Opus for the synthesis if available — it's exceptionally
    # good at meta-tasks like "synthesize multiple expert opinions."
    use_claude = config.PROFILE_MERGE_USE_CLAUDE
    if use_claude:
        try:
            from backend.scoring.anthropic_client import get_anthropic_client
            anthropic = get_anthropic_client()
            if anthropic is not None:
                anthropic.current_run_id = getattr(gemini_client, "current_run_id", None)
                anthropic.current_stage = "profile_synthesize_claude"
                response = await anthropic.complete(
                    model=config.PROFILE_ANTHROPIC_MODEL,
                    system=PROFILE_SYNTHESIS_PROMPT + (
                        "\n\nIMPORTANT: Output strictly valid JSON only. "
                        "No markdown fences. Match the schema of the input drafts."
                    ),
                    user=user_message,
                    max_output_tokens=8192,
                    temperature=0.2,
                    thinking_budget=config.PROFILE_ANTHROPIC_THINKING,
                )
                if isinstance(response.parsed_json, dict):
                    return response.parsed_json
        except Exception:
            pass

    # Fall back to Gemini for synthesis if Claude unavailable
    if hasattr(gemini_client, "current_stage"):
        gemini_client.current_stage = "profile_synthesize_gemini"  # type: ignore[attr-defined]
    response = await gemini_client.complete(
        model=config.PROFILE_BUILD_MODEL,
        system=PROFILE_SYNTHESIS_PROMPT,
        user=user_message,
        max_output_tokens=8192,
        temperature=0.2,
        json_schema=_RESPONSE_SCHEMA,
        thinking_budget=config.PROFILE_BUILD_THINKING,
    )

    merged = response.parsed_json
    if not isinstance(merged, dict):
        try:
            merged = json.loads(response.text)
        except json.JSONDecodeError:
            # Ultimate fallback — return the first usable draft
            return candidate_drafts[0][1]
    return merged


# ----------------------------------------------------------------------------
# Legacy single-merge (kept for backwards compat / reference)
# ----------------------------------------------------------------------------

PROFILE_MERGE_PROMPT = """\
You are a senior career analyst synthesizing two independent expert keyword
lists for the same candidate. Two analysts (different reasoning models) each
produced their own draft profile + keyword list. Your job is to merge them
into the BEST possible final list.

============================================================
MERGE PRINCIPLES
============================================================

1. KEYWORDS PRESENT IN BOTH LISTS = HIGH-CONFIDENCE
   If the same keyword (or close variant — e.g., "AI Strategy Consultant"
   and "AI Strategist Consultant") appears in both, it's almost certainly
   right. Keep it. Use the higher tier of the two.

2. KEYWORDS IN ONE LIST ONLY = JUDGE INDIVIDUALLY
   Read the keyword + candidate profile + freeform context. Three outcomes:
   a) Clearly correct — the other model missed it. KEEP. Tier appropriately.
   b) Clearly off-target (broad/wrong-function/contradicts user's "avoid"
      signals). DROP.
   c) Reasonable but uncertain. KEEP at Tier 2 or 3.

3. APPLY ALL THE STANDARD AUDIT PRINCIPLES
   - Keywords must match employer job titles, not resume language
   - Title-naming variations (Lead/Manager/Specialist/Director) for each
     core concept — don't drop variants just because both drafts only had
     one. ADD missing variants.
   - Negative-signal gating — if user says "moving away from X," demote
     X-related keywords.
   - Broad-noise suppression — drop generic terms unless user names them.
   - Tier 1: 10-15 keywords. Tier 2: 10-15. Tier 3: 8-12.

4. PROFILE FIELDS — MERGE THE BEST
   For headline, target_functions, technical_skills, etc. — combine
   complementary information. Don't just pick one. If both drafts agree on
   "Senior IC", keep it. If one says "Senior IC" and other says "Manager",
   pick the one that better matches the candidate's stated freeform context.

5. EXPAND TITLE-NAMING VARIANTS
   This is the most common gap: both drafts will name "X Specialist" but
   miss "X Lead" and "X Manager". For each Tier 1 concept, ensure ALL
   reasonable title variants are present.

6. NEVER REGRESS — output should be at least as good as the better draft.
   When in doubt, lean toward INCLUDING rather than excluding.

============================================================
OUTPUT
============================================================

Output the FINAL merged profile in the same JSON schema as the inputs.
This is the user's actual profile — make it count.
"""


async def _merge_consensus(
    *,
    client,
    gemini_draft: dict,
    claude_draft: dict,
    resumes: list[tuple[str, str]],
    user_preferences: dict,
) -> dict:
    """Merge two independent keyword lists into a consensus."""
    parts = ["ORIGINAL CANDIDATE INPUTS:"]
    for filename, text in resumes:
        parts.append(f"=== {filename} ===\n{text[:5000]}")

    if user_preferences.get("freeform_context"):
        parts.append("\n=== USER FREEFORM CONTEXT ===")
        parts.append(user_preferences["freeform_context"])

    if user_preferences:
        parts.append("\n=== USER PREFERENCES ===")
        for k, v in user_preferences.items():
            if k != "freeform_context":
                parts.append(f"  {k}: {v}")

    parts.append("\n=== DRAFT A (Gemini Pro) ===")
    parts.append(json.dumps(gemini_draft, indent=2))
    parts.append("\n=== DRAFT B (Claude Opus) ===")
    parts.append(json.dumps(claude_draft, indent=2))

    if hasattr(client, "current_stage"):
        client.current_stage = "profile_merge"  # type: ignore[attr-defined]
    response = await client.complete(
        model=config.PROFILE_BUILD_MODEL,
        system=PROFILE_MERGE_PROMPT,
        user="\n".join(parts),
        max_output_tokens=8192,
        temperature=0.2,
        json_schema=_RESPONSE_SCHEMA,
        thinking_budget=config.PROFILE_BUILD_THINKING,
    )

    merged = response.parsed_json
    if not isinstance(merged, dict):
        try:
            merged = json.loads(response.text)
        except json.JSONDecodeError:
            return gemini_draft

    required = ["headline", "target_functions", "keywords_tier_1"]
    if not all(k in merged for k in required):
        return gemini_draft

    return merged


async def _audit_and_refine(
    *,
    client,
    draft: dict,
    resumes: list[tuple[str, str]],
    user_preferences: dict,
) -> dict:
    """Run a second Pro pass to critique and revise the initial keyword list."""
    parts = [
        "ORIGINAL INPUTS — RESUMES:",
    ]
    for filename, text in resumes:
        parts.append(f"=== {filename} ===\n{text[:6000]}")

    if user_preferences.get("freeform_context"):
        parts.append("\n=== USER FREEFORM CONTEXT ===")
        parts.append(user_preferences["freeform_context"])

    if user_preferences:
        parts.append("\n=== USER PREFERENCES ===")
        for k, v in user_preferences.items():
            if k != "freeform_context":
                parts.append(f"  {k}: {v}")

    parts.append("\n=== DRAFT PROFILE TO AUDIT ===")
    parts.append(json.dumps(draft, indent=2))

    prompt = "\n".join(parts)

    if hasattr(client, "current_stage"):
        client.current_stage = "profile_audit"  # type: ignore[attr-defined]
    response = await client.complete(
        model=config.PROFILE_BUILD_MODEL,
        system=PROFILE_AUDIT_PROMPT,
        user=prompt,
        max_output_tokens=8192,
        temperature=0.2,
        json_schema=_RESPONSE_SCHEMA,
        thinking_budget=config.PROFILE_AUDIT_THINKING,
    )

    revised = response.parsed_json
    if not isinstance(revised, dict):
        try:
            revised = json.loads(response.text)
        except json.JSONDecodeError:
            return draft  # audit returned garbage, keep draft

    # Sanity check: revised version must still have the required fields
    required = ["headline", "target_functions", "keywords_tier_1"]
    if not all(k in revised for k in required):
        return draft

    return revised
