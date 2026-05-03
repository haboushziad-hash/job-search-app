"""Persistent storage for the desktop app — runs.db archive + audit folder.

The archive is a single SQLite database living in the user's audit folder
(per Settings). It captures every search run, every role ever scraped,
liveness-check history, application status, and cross-tester observations
that feed the closed-loop keyword learner.

Everything here is local-first: there's no server. The audit folder syncs
to the user's chosen cloud drive (OneDrive / Google Drive / iCloud), which
is what enables cross-tester learning without infrastructure.
"""
from backend.storage.archive import Archive, get_archive, set_audit_folder
from backend.storage.market import (
    export_contributions,
    market_titles_for_keyword_prompt,
    read_sibling_contributions,
)
from backend.storage.audit import write_audit_files, find_prior_audit_for_profile

__all__ = [
    "Archive", "get_archive", "set_audit_folder",
    "export_contributions", "market_titles_for_keyword_prompt",
    "read_sibling_contributions",
    "write_audit_files", "find_prior_audit_for_profile",
]
