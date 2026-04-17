#!/usr/bin/env python3
"""
CLI entry point for downloading and analysing macOS application packages.

Supports scanning individual apps or all configured apps, exporting
version history to JSON, and validating stored download URLs.
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict

from tracker.database import VersionDatabase
from tracker import MicrosoftAppAnalyzer, load_apps_config, validate_urls


def _build_parser(known_apps: Dict[str, Dict[str, str]]) -> argparse.ArgumentParser:
    epilog = f"Available apps: {', '.join(sorted(known_apps))}" if known_apps else None
    parser = argparse.ArgumentParser(
        description="Download and analyze Microsoft Mac applications",
        epilog=epilog,
    )
    parser.add_argument("app", nargs="?", help="Application to analyze (use 'all', default: companyportal)")
    parser.add_argument("--show-history", action="store_true", help="Show version history from database")
    parser.add_argument("--export-json", metavar="FILE", help="Export version history to JSON file")
    parser.add_argument("--custom-url", help="Custom download URL (requires --custom-name)")
    parser.add_argument("--custom-name", help="Custom app name (requires --custom-url)")
    parser.add_argument("--db", default="microsoft_apps_versions.db", help="Database file path")
    parser.add_argument("--list-apps", action="store_true", help="List all available apps")
    parser.add_argument("--keep-downloads", action="store_true", help="Keep downloaded packages on disk")
    parser.add_argument("--validate-urls", action="store_true", help="Check stored URLs and mark removed ones")
    return parser


def _list_apps(apps: Dict[str, Dict[str, str]]) -> None:
    print("\nAvailable applications:")
    print("=" * 80)
    for app_id, info in sorted(apps.items()):
        print(f"\n{app_id}:")
        print(f"  Name: {info.get('name')}")
        print(f"  URL: {info.get('url')}")
        print(f"  Identifier: {info.get('identifier')}")
        description = info.get("description")
        if description:
            print(f"  Description: {description}")
    print("\n" + "=" * 80)


def _analyze_app(app_id: str, info: Dict[str, str], args: argparse.Namespace) -> bool:
    analyzer = MicrosoftAppAnalyzer(
        app_name=info.get("name", app_id),
        download_url=info.get("url", ""),
        expected_identifier=info.get("identifier"),
        package_type=info.get("type", "single"),
        db_path=args.db,
        keep_downloads=args.keep_downloads,
        url_type=info.get("url_type", "direct"),
    )
    results = analyzer.analyze()
    if results is None:
        return False
    if isinstance(results, dict) and results.get("unchanged"):
        return True
    return bool(results)


def main() -> int:
    known_apps = load_apps_config()
    parser = _build_parser(known_apps)
    args = parser.parse_args()

    if args.list_apps:
        if not known_apps:
            print("No applications configured.")
            return 0
        _list_apps(known_apps)
        return 0

    if args.show_history:
        with VersionDatabase(args.db) as db:
            db.print_version_history()
        return 0

    if args.validate_urls:
        validate_urls(args.db)
        return 0

    if args.export_json:
        with VersionDatabase(args.db) as db:
            db.export_to_json(args.export_json)
        return 0

    if args.custom_url and args.custom_name:
        analyzer = MicrosoftAppAnalyzer(
            app_name=args.custom_name,
            download_url=args.custom_url,
            package_type="single",
            db_path=args.db,
            keep_downloads=args.keep_downloads,
        )
        return 0 if analyzer.analyze() else 1

    app_to_analyze = args.app or "companyportal"

    if app_to_analyze == "all":
        success = True
        for app_id, info in known_apps.items():
            print("\n")
            if not _analyze_app(app_id, info, args):
                success = False
        print("\n")
        validate_urls(args.db)
        return 0 if success else 1

    if app_to_analyze not in known_apps:
        print(f"✗ Unknown app: {app_to_analyze}")
        if known_apps:
            print(f"Available apps: {', '.join(sorted(known_apps))}")
            print("Use --list-apps to see details")
        return 1

    if _analyze_app(app_to_analyze, known_apps[app_to_analyze], args):
        print("\n" + "=" * 60)
        print("✓ Analysis complete!")
        print("=" * 60)
        return 0

    print("\n✗ Analysis failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
