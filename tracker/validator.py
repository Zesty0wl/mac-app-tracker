"""Re-validates stored download URLs and flags removed or broken assets."""

from __future__ import annotations

from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .database import VersionDatabase


def validate_urls(db_path: str = "microsoft_apps_versions.db") -> None:
    """Check stored download URLs and mark the ones removed upstream."""
    print("\n" + "=" * 60)
    print("VALIDATING DOWNLOAD URLS")
    print("=" * 60)

    with VersionDatabase(db_path) as db:
        urls_to_check = db.get_all_reachable_urls()
        if not urls_to_check:
            print("No URLs to validate")
            return

        print(f"Found {len(urls_to_check)} unique URLs to validate...")
        removed_count = 0
        valid_count = 0

        for record in urls_to_check:
            version_id = record["id"]
            url = record["actual_url"]
            display_url = url if len(url) <= 60 else url[:60] + "..."

            try:
                request = Request(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    },
                )
                request.get_method = lambda: "HEAD"

                with urlopen(request, timeout=10) as response:
                    if response.status == 200:
                        print(f"✓ {display_url}")
                        valid_count += 1
                    else:
                        print(f"✗ {display_url} (HTTP {response.status})")
                        db.mark_url_as_removed(version_id)
                        removed_count += 1

            except HTTPError as exc:
                if exc.code in [403, 404, 410]:
                    print(f"✗ {display_url} (HTTP {exc.code})")
                    db.mark_url_as_removed(version_id)
                    removed_count += 1
                else:
                    print(f"⚠ {display_url} (HTTP {exc.code} - keeping)")
                    valid_count += 1
            except URLError:
                print(f"⚠ {display_url} (Network error - keeping)")
                valid_count += 1
            except Exception as exc:  # pragma: no cover - defensive
                print(f"⚠ {display_url} (Error: {str(exc)[:30]} - keeping)")
                valid_count += 1

        print(f"\n✓ Validation complete: {valid_count} valid, {removed_count} removed")
