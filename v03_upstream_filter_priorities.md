# Upstream Filter Push — v0.3 Priority

User-flagged architectural improvement for v0.3. Documenting here so it's preserved and can be incorporated into the deep-dive report.

## The insight

Currently the pipeline is:
```
[scrape EVERYTHING] → [hard filters: salary, location, age] → [Stage 1/2/3 scoring] → qualifying
```

Many scrapers' APIs support server-side filters we're NOT using. Pushing the filters upstream:
1. **Saves quota/cost** on paid APIs (JSearch, Adzuna)
2. **Reduces network bytes + dedup CPU + filter CPU**
3. **Improves data accuracy** — first-party salary/location/employment_type fields are more accurate than our post-hoc JD body parsing
4. **Reduces JD-fetch waste** on roles we'd reject anyway

## What to look for per scraper

For each existing scraper, identify:

1. **Server-side filters the API supports** — read API docs, inspect URL/query params being constructed:
   - `salary_min` / `salary_max` / `comp_min`
   - `location` / `city` / `state` / `country` / `region`
   - `radius` / `distance` (geographic)
   - `remote_only=true` / `is_remote` / `work_from_home`
   - `employment_type=FULLTIME/PARTTIME/CONTRACT`
   - `posted_within=today/3days/week/month` / `date_posted`
   - `experience_level=junior/mid/senior`
   - `category` / `industry` / `function`
   - `seniority`

2. **Which are we currently NOT using?** (file:line references)

3. **Should we push each one?** Trade-offs:
   - PRO: less data, less quota, better quality
   - CON: server-side filters might be too narrow (filter borderline acceptables), might fail for users with no salary preference

4. **Expected impact** quantified per filter

## Quick wins (suspected, pending agent verification)

- **JSearch** — supports `salary_min`, `remote_jobs_only`, `date_posted`, `country`, `region`. Currently only sends `employment_types=FULLTIME`. Pushing salary_min alone could save 30% Pro tier quota per run.
- **Adzuna** — supports `salary_min`, `where`, `distance`. We could push `where=Richmond,VA` + `distance=50` instead of grabbing all 989 raw and filtering.
- **USAJOBS** — supports MANY filters: `LocationName`, `RemuneratitionMinimumAmount`, `JobCategoryCode`, `WhoMayApply`, `PayGradeLow`, `PayGradeHigh`. Underutilized.
- **Findwork** — supports `remote`, `employment_type`, `location`. Push these.
- **TheMuse** — supports `category`, `level`, `location`. Push these.
- **Workday** — per-tenant CXS API supports location facets via `appliedFacets.locations`.
- **SmartRecruiters** — API supports `country`, `region`, `city` filters.

## Constraints to remember

- The user's profile MAY have empty fields (some testers don't set salary minimum). Handle gracefully — only push the filter if the user has expressed the preference.
- Some filters are user-pref + sensible defaults — e.g., posted_within=30days everywhere by default.
- For multi-keyword scraping, push the SAME filter set across all keywords for that scraper.

## Output

This analysis should be one of the TOP 3 sections in `v03_scraper_audit_v2.md`. Include a summary table:

| Scraper | Filter we could push | Currently? | Expected impact | Effort |
|---|---|---|---|---|
| JSearch | salary_min | ❌ | Save ~30% quota | Low |
| ... | ... | ... | ... | ... |
