"""Production entry point for the bundled backend.

PyInstaller turns this into backend.exe, which Tauri spawns as a sidecar
on app launch. Differences from `python -m backend.api`:
  - Loads .env from a user-writable directory (not next to the .exe)
  - Logs to a file (not just stdout) so issues are diagnosable post-mortem
  - Listens only on 127.0.0.1 (no remote access ever)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path


def _user_data_dir() -> Path:
    """Per-user writable directory for logs + .env (Windows: %APPDATA%)."""
    if sys.platform == "win32":
        import os
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path.home() / ".config"
    folder = base / "JobSearchApp"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def main() -> None:
    # v0.2.0: force stdout/stderr to UTF-8 globally. Without this, any
    # print() statement that contains non-ASCII characters (em-dashes,
    # arrows, fancy bullets, foreign-language city names from scraper
    # results, etc.) crashes the backend on Windows because the default
    # console codec is cp1252. The previous v0.1.x backends had latent
    # crashes from print statements like "Greenhouse — searching" that
    # only manifested when the print was actually triggered.
    # `errors='replace'` substitutes any unencodeable char with '?' so
    # diagnostic logs never crash the app.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # Older Python or non-tty stdout — best effort.
        pass

    user_dir = _user_data_dir()
    # Load .env from user-data dir if present (testers can drop one there)
    try:
        from dotenv import load_dotenv
        env_file = user_dir / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=True)
    except Exception:
        pass

    # File logging
    log_path = user_dir / "backend.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(str(log_path), encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.info(f"Backend starting. Logs at {log_path}")

    # Run the FastAPI server
    import uvicorn
    from backend.api import app  # noqa: F401  (ensures startup hook fires)
    uvicorn.run(
        "backend.api:app",
        host="127.0.0.1",
        port=8765,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
