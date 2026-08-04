"""Re-validates stored download URLs and flags removed or broken assets."""

from __future__ import annotations

from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .database import VersionDatabase

_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_GONE_CODES = (403, 404, 410)


def _request_status(url: str, method: str) -> int:
    headers = {"User-Agent": _USER_AGENT}
    if method == "GET":
        # Existence probe only -- ask for a single byte so servers that
        # honour Range don't send the whole package.
        headers["Range"] = "bytes=0-0"
    request = Request(url, headers=headers)
    request.get_method = lambda: method
    with urlopen(request, timeout=10) as response:
        return response.status


def _classify_url(url: str) -> tuple:
    """Probe ``url`` and return (verdict, detail).

    verdict is 'valid', 'removed' or 'keep' (transient/unknown failure).
    Some hosts (e.g. download.msappproxy.net, which serves Global Secure
    Access) reject HEAD with 404 while GET works, so a URL is only
    condemned when a ranged GET agrees with the failing HEAD.
    """
    try:
        _request_status(url, "HEAD")
        return ("valid", "")
    except HTTPError as exc:
        if exc.code not in _GONE_CODES:
            return ("keep", f"HTTP {exc.code}")
        head_code = exc.code
    except URLError as exc:
        return ("keep", f"Network error: {str(exc.reason)[:30]}")
    except Exception as exc:  # pragma: no cover - defensive
        return ("keep", f"Error: {str(exc)[:30]}")

    try:
        _request_status(url, "GET")
        return ("valid", f"HEAD {head_code} but GET ok")
    except HTTPError as exc:
        if exc.code in _GONE_CODES:
            return ("removed", f"HTTP {exc.code}")
        return ("keep", f"HTTP {exc.code}")
    except URLError as exc:
        return ("keep", f"Network error: {str(exc.reason)[:30]}")
    except Exception as exc:  # pragma: no cover - defensive
        return ("keep", f"Error: {str(exc)[:30]}")


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

            verdict, detail = _classify_url(url)
            if verdict == "valid":
                print(f"✓ {display_url}" + (f" ({detail})" if detail else ""))
                valid_count += 1
            elif verdict == "removed":
                print(f"✗ {display_url} ({detail})")
                db.mark_url_as_removed(version_id)
                removed_count += 1
            else:
                print(f"⚠ {display_url} ({detail} - keeping)")
                valid_count += 1

        print(f"\n✓ Validation complete: {valid_count} valid, {removed_count} removed")
