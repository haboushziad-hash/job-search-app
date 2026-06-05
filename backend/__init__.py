"""Job Search App — Backend package.

v0.2.0: stdout/stderr UTF-8 reconfigure runs the moment ANY module in
the `backend` package is imported. This is the global defense against
Windows cp1252 console encoding crashes — print statements anywhere in
the codebase that contain non-ASCII characters (em-dashes, arrows,
foreign-language city names from scraper results, etc.) used to crash
the backend with UnicodeEncodeError. Fixing them per-file was whack-a-
mole; doing it once here covers every entry point:

    - python -m backend.api   (dev mode)
    - python -m backend.runner (CLI)
    - backend_main.py          (production .exe)
    - any test in backend/tests/

`errors='replace'` substitutes any unencodeable char with '?' so even
truly broken bytes never crash the app.
"""
import sys

__version__ = "0.3.39"

# Reconfigure as early as possible — before any other backend module
# can run a print() with a non-ASCII char in it.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    # Older Python or non-tty stdout — best effort. Production builds
    # all run on Python 3.11+ so reconfigure() is always available; this
    # except is just paranoia for unusual environments.
    pass
