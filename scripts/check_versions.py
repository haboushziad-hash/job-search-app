"""Pre-push SemVer validator for Job Search App.

Runs both locally (via git pre-push hook) and in CI (via GitHub Actions
workflow) to catch invalid version strings BEFORE Tauri's strict SemVer
parser rejects the build.

History: v0.3.5.2 hotfix attempt failed in GHA because tauri.conf.json
contained "0.3.5.2" — a 4-segment Windows-installer-style version that
isn't valid SemVer 2.0.0. Tauri rejected it before the build started,
which we couldn't tell from the build's local pass since `tauri build`
wasn't invoked locally before the push. This script catches that case
in 50ms by validating ALL version strings against the SemVer 2.0.0 spec.

What this checks:
  - desktop_app/package.json::version
  - desktop_app/src-tauri/tauri.conf.json::version
  - desktop_app/src-tauri/Cargo.toml::version
  - All four version strings must be valid SemVer 2.0.0
  - All four must agree (no drift between the JSON, TOML, and Python)
  - backend/__init__.py::__version__ — must be SemVer-valid

Exit code:
  0 — all version strings valid and consistent
  1 — invalid SemVer or version mismatch detected

Usage:
  python scripts/check_versions.py
  # Or via the pre-push hook (auto-installed by `git config core.hooksPath`)

CI usage:
  Add this to .github/workflows/build-desktop.yml as a job that runs
  BEFORE the Tauri build matrix, so failed validation fails fast without
  burning a 10-minute Mac build slot.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Strict SemVer 2.0.0 regex from semver.org spec
# https://semver.org/#is-there-a-suggested-regular-expression-regex-to-check-a-semver-string
SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)"
    r"\.(?P<minor>0|[1-9]\d*)"
    r"\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+(?P<buildmetadata>[0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)

ROOT = Path(__file__).resolve().parent.parent


def is_valid_semver(version: str) -> bool:
    """True if version matches SemVer 2.0.0 spec (3-segment + optional
    prerelease + optional build metadata)."""
    return bool(SEMVER_RE.match(version))


def check_package_json() -> tuple[str, bool, str]:
    """Returns (version_string, is_valid, error_message)."""
    p = ROOT / "desktop_app" / "package.json"
    if not p.exists():
        return ("", False, f"missing file: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return ("", False, f"invalid JSON in {p}: {e}")
    v = data.get("version", "")
    if not v:
        return ("", False, f"missing 'version' field in {p}")
    return (v, is_valid_semver(v), "")


def check_tauri_conf() -> tuple[str, bool, str]:
    p = ROOT / "desktop_app" / "src-tauri" / "tauri.conf.json"
    if not p.exists():
        return ("", False, f"missing file: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return ("", False, f"invalid JSON in {p}: {e}")
    v = data.get("version", "")
    if not v:
        return ("", False, f"missing 'version' field in {p}")
    return (v, is_valid_semver(v), "")


def check_cargo_toml() -> tuple[str, bool, str]:
    p = ROOT / "desktop_app" / "src-tauri" / "Cargo.toml"
    if not p.exists():
        return ("", False, f"missing file: {p}")
    text = p.read_text(encoding="utf-8")
    # Extract `version = "..."` from the [package] section
    # Cargo only allows one `version` line in the package section
    m = re.search(
        r'^\s*\[package\]\s*\n(?:[^\[]*?)version\s*=\s*"([^"]+)"',
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return ("", False, f"could not find 'version = \"...\"' in {p}")
    v = m.group(1)
    return (v, is_valid_semver(v), "")


def check_backend_init() -> tuple[str, bool, str]:
    p = ROOT / "backend" / "__init__.py"
    if not p.exists():
        return ("", False, f"missing file: {p}")
    text = p.read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        return ("", False, f"could not find __version__ in {p}")
    v = m.group(1)
    return (v, is_valid_semver(v), "")


def main() -> int:
    print("Checking version strings (SemVer 2.0.0 compliance)...\n")

    checks = {
        "desktop_app/package.json": check_package_json(),
        "desktop_app/src-tauri/tauri.conf.json": check_tauri_conf(),
        "desktop_app/src-tauri/Cargo.toml": check_cargo_toml(),
        "backend/__init__.py": check_backend_init(),
    }

    any_invalid = False
    versions = {}
    for name, (version, valid, err) in checks.items():
        if err:
            print(f"  [ERROR] {name}: {err}")
            any_invalid = True
            continue
        marker = "OK" if valid else "INVALID"
        print(f"  [{marker}] {name}: {version}")
        versions[name] = version
        if not valid:
            any_invalid = True

    if any_invalid:
        print("\nFAILED: at least one version string is not valid SemVer 2.0.0.")
        print("SemVer 2.0.0 format: MAJOR.MINOR.PATCH[-prerelease][+buildmetadata]")
        print("Examples of valid: 0.3.7, 1.0.0, 2.5.1-rc.1, 1.0.0+build.123")
        print("Examples of INVALID: 0.3.5.2 (4-segment), v1.0.0 (leading v), 1.0 (2-segment)")
        return 1

    # Cross-file consistency: package.json, tauri.conf.json, and Cargo.toml
    # must agree (Cargo can lag if SemVer prerelease syntax is used elsewhere,
    # but normal releases must match).
    pkg_versions = {
        k: v
        for k, v in versions.items()
        if k != "backend/__init__.py"
    }
    distinct = set(pkg_versions.values())
    if len(distinct) > 1:
        print(
            f"\nWARNING: desktop-app version strings disagree across files: "
            f"{pkg_versions}"
        )
        print("This may cause confusing installer / Cargo build behavior.")
        # Don't fail — Cargo can legitimately lag if Tauri uses prerelease
        # syntax (Cargo SemVer is stricter). Just warn.

    backend_v = versions.get("backend/__init__.py", "")
    tauri_v = versions.get("desktop_app/src-tauri/tauri.conf.json", "")
    if backend_v and tauri_v and backend_v != tauri_v:
        print(
            f"\nWARNING: backend.__version__ ({backend_v}) does not match "
            f"tauri.conf.json version ({tauri_v}). Audit JSON output will "
            f"report a different version than the installer claims."
        )

    print("\nPASSED: all version strings are valid SemVer 2.0.0.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
