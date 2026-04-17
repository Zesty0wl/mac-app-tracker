"""App catalogue loader -- reads tracked apps from the admin database."""

from __future__ import annotations

from typing import Dict, Any


def load_apps_config(**_kwargs) -> Dict[str, Dict[str, Any]]:
    """Return the application catalogue from the database."""
    try:
        from admin.database import load_apps_from_db
        return load_apps_from_db()
    except Exception:
        return {}


def build_identifier_lookup(apps_config: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Return a mapping keyed by package identifier with metadata plus id."""
    lookup: Dict[str, Dict[str, Any]] = {}
    for app_id, meta in apps_config.items():
        identifier = meta.get("identifier")
        if not identifier:
            continue
        entry = dict(meta)
        entry["id"] = app_id
        lookup[identifier] = entry
    return lookup
