# Tester Intro — Send-Ready Template

Two parts: a short pitch you can paste into a DM/SMS, and a longer email
version. Metric placeholders (`{TBD: ...}`) get filled in after Ryan's
proxy run audit + final smoke test.

---

## Short version (DM / SMS / iMessage)

```
Hey — built a job-search tool I want you to try.

You drop in your resume, it searches 16 job boards, scores every role
1-100 against your background using GPT-class AI, and shows you which
ones to actually apply to. Takes ~10 min per search.

Free for you — no sign-up, no API keys, nothing to install except the
app itself. I'm covering the AI cost during the pilot.

Download: https://findmesomedamnjobz.com

Two things to know:
- Windows / Mac may flag it as "unverified developer" because I haven't
  paid the $100/yr code-signing fee. Click "More info → Run anyway" on
  Windows, or right-click → Open on Mac.
- There's a "Send Feedback" link in the sidebar. Use it for anything
  weird — bad matches, missed roles, crashes, confusing UI.

Updates auto-install in the background.

Thanks for testing — getting honest feedback before I open this up wider.
```

---

## Long version (email)

**Subject:** `findmesomedamnjobz — pre-release tester invite`

```
Hey {NAME},

I've been building a desktop job-search tool for the last few weeks and
it's finally at a state where I want a few people to break it.

What it does:

You drop your resume in. It builds a profile of what you're targeting,
generates {TBD: search term count, ~10-14} broad search queries plus
{TBD: keyword count, ~30-40} specific job titles to score against, then
hits 16 different job boards in parallel:

  Greenhouse · Lever · Ashby · Workday · iCIMS
  USAJOBS (federal) · Adzuna · JSearch (LinkedIn/Indeed/Glassdoor/ZipRecruiter)
  BuiltIn · The Muse · Remotive · Climatebase
  SmartRecruiters · Arbeitnow · Hacker News · Findwork

Every role gets scored 1-100 by a 4-stage AI cascade (embedding pre-filter
→ Flash anti-pattern check → Flash triage → Pro deep evaluation). You get
a dashboard with STRONG / GOOD / MAYBE / STRETCH tiers, salary, location,
and the LLM's reasoning for each role's score.

Typical run: {TBD: 8-12} minutes, {TBD: 100-180} qualifying roles surfaced
from {TBD: 3500-5000} scraped, {TBD: 25-35} STRONG matches.

What's free vs paid for you:

  Everything is free. I'm covering the AI cost during the pilot
  (~${TBD: 0.60-1.00} per search × your usage). No sign-up, no payment
  info, no API keys to manage.

What I want from you:

  1. Run a search or two when you're job-hunting
  2. Use the "Send Feedback" link in the left sidebar for anything weird
     — wrong scores, missed roles, crashes, confusing UI, bad copy.
     Specific examples are gold (role title + company + what you
     expected vs. what you saw).
  3. Tell me when it's better than what you'd get manually, and when
     it's worse.

Install:

  https://findmesomedamnjobz.com

Heads up:

  - Windows: SmartScreen will flag it because I haven't paid for code
    signing yet. Click "More info" → "Run anyway."
  - Mac: drag to Applications, then right-click the app → Open the
    first time. Don't double-click — Mac is stricter about that.
  - Updates apply automatically. You'll see a quick toast on next launch
    when I ship a fix.

That's it. Should take ~5 minutes to install and ~10 minutes to run your
first search. Let me know what breaks.

— Ziad
```

---

## Metrics to fill in after final smoke test

Replace all `{TBD: ...}` placeholders with actual numbers from the final
audit. Pull from the v0.1.4 verification runs:

| Placeholder | Value source |
|---|---|
| `{TBD: search term count}` | profile.search_terms count from a typical persona run (Ziad: 14, Zach: 11, Ryan: ~10) |
| `{TBD: keyword count}` | profile.keywords count (~30-40) |
| `{TBD: minutes}` | run_metadata.duration_seconds / 60 |
| `{TBD: qualifying roles}` | pipeline_funnel.qualifying_final |
| `{TBD: scraped}` | pipeline_funnel.total_scraped |
| `{TBD: STRONG matches}` | pipeline_funnel.tier_breakdown.STRONG |
| `{TBD: cost per search}` | run_metadata.cost_breakdown.total_usd |

Round to friendly numbers. "150 qualifying roles" is more readable than
"168 qualifying roles."

---

## Don't include in the intro (yet)

- Specific scoring methodology details (the cascade is internal — let
  the experience speak)
- Cost breakdown (could spook testers thinking it's their money)
- Limitations (Workday Big 4 still failing, JSearch free-tier limits) —
  testers will discover what works and doesn't through use
- Tester pilot end date — you'll know when you've gotten enough feedback

## Send order

1. Send short version to people you know well + can ping immediately
2. Send long version to people who'd want context first (more formal
   relationships, work contacts, etc.)
3. After 2-3 days, follow up with non-responders ONCE: "any luck with
   the install? happy to help debug if needed"
