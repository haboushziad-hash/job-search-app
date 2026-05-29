# PyInstaller spec for the bundled FastAPI backend that ships with the
# Tauri desktop app as a sidecar. Builds a SINGLE EXECUTABLE FILE — no
# separate _internal folder — so Tauri's externalBin can ship it cleanly.
#
# Build with:
#   backend\venv\Scripts\python.exe -m PyInstaller backend.spec --clean
#
# Output:
#   dist/backend.exe                     (single-file)
#
# Then for Tauri sidecar, copy/rename to:
#   desktop_app/src-tauri/binaries/backend-x86_64-pc-windows-msvc.exe

from PyInstaller.utils.hooks import collect_all, collect_data_files
import sys
from pathlib import Path

PROJECT_ROOT = Path('.').resolve()

# Hidden imports — modules PyInstaller's static analyzer can miss because
# they're loaded dynamically (Pydantic, FastAPI, uvicorn, google-genai).
hidden_imports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "google.genai",
    "google.auth",
    "anthropic",
    "tenacity",
    "httpx",
    "pydantic",
    "pydantic_core",
    "multipart",            # python-multipart — required by FastAPI for Form() params (e.g., /profile/build)
    "python_multipart",     # newer name some FastAPI versions look for
]

# Bundle data files for packages that need them at runtime
datas = []
for pkg in ("google.genai", "google.api_core", "anthropic", "fastapi", "uvicorn"):
    try:
        datas.extend(collect_data_files(pkg))
    except Exception:
        pass

# Backend package itself — collect everything (its scrapers etc. are
# loaded dynamically via the registry pattern)
b_datas, b_binaries, b_hidden = collect_all('backend')
datas.extend(b_datas)
hidden_imports.extend(b_hidden)

# Seed market contributions — pre-launch validation data baked into the
# build so testers' first runs benefit from cross-tester learning before
# they've synced any audit folder.
seed_path = PROJECT_ROOT / "backend" / "data" / "seed_market_contributions.jsonl"
if seed_path.exists():
    datas.append((str(seed_path), "backend/data"))

# v0.3.12: Workday tenant JSON configs (165+ files). These power the runtime
# tenant loader added to backend/scraper/workday.py. `collect_all('backend')`
# above SHOULD include them transitively, but PyInstaller's data-file
# collection is sensitive to package layout: if `backend/scraper/__init__.py`
# excludes subpackages, or if collect_all's heuristic misses non-.py files
# in a non-package subdirectory, these silently vanish from the bundle and
# the loader regresses to the 31 hardcoded tenants with NO error surfaced.
#
# This is the same silent-failure bug class as GoogleJobs and iCIMS — and
# the loss would be catastrophic: 135 dynamically-loaded tenants gone in
# production. So we add them EXPLICITLY here as a belt-and-suspenders
# guarantee. Each JSON gets dropped at backend/scraper/workday_tenants/
# inside the bundle, matching what `Path(__file__).resolve().parent /
# 'workday_tenants'` resolves to at runtime.
tenants_dir = PROJECT_ROOT / "backend" / "scraper" / "workday_tenants"
if tenants_dir.is_dir():
    for tenant_json in tenants_dir.glob("*.json"):
        datas.append((str(tenant_json), "backend/scraper/workday_tenants"))


a = Analysis(
    ['backend/backend_main.py'],
    pathex=[str(PROJECT_ROOT)],
    binaries=b_binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Trim large unused packages
        "playwright",       # only used in dev scripts, not at runtime
        "pyinstaller",
        "PyQt5", "PyQt6", "PySide2", "PySide6",
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
        "IPython", "jupyter",
        "sphinx",
        "pytest",
        "test", "tests",
        # v0.2.0: geonamescache is dev-only — used by scripts/regen_city_lists.py
        # to expand the backend's CITIES dict from a public dataset, but the
        # output is the static dict in geocoding.py. The bundled .exe doesn't
        # need geonamescache at runtime (we wrote out the cities into the .py
        # already). Excluding shaves ~32MB from the build.
        "geonamescache",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name='backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,        # Backend runs as a console app (Tauri reads its stdout)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
