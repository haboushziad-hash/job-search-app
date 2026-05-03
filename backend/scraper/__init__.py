"""Scraper module — pulls fresh roles from job boards.

Architecture:
  - `client.py`         shared HTTP client with anti-bot pacing + retries
  - `base.py`           abstract Scraper interface every board implements
  - `<board>.py`        one file per job board (greenhouse, lever, ashby, ...)
  - `orchestrator.py`   runs all configured scrapers in parallel
  - `liveness.py`       HEAD-checks role URLs, drops dead links
  - `salary_extractor.py` regex + LLM fallback for missed salaries
"""
