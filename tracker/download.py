"""URL resolution, ETag/Last-Modified checks, and streaming download of macOS packages."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Tuple, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from .database import VersionDatabase

DOWNLOAD_TIMEOUT = 300  # 5 minutes

# Order of preference when picking a macOS asset from a GitHub release.
GITHUB_ASSET_PRIORITY = (".dmg", ".pkg", ".zip")


@dataclass
class DownloadHeaders:
    last_modified: Optional[str]
    etag: Optional[str]
    content_length: Optional[str]

    def as_dict(self) -> Dict[str, Optional[str]]:
        return {
            "last_modified": self.last_modified,
            "etag": self.etag,
            "content_length": self.content_length,
        }


@dataclass
class DownloadResult:
    file_path: Optional[Path]
    actual_url: Optional[str]
    headers: DownloadHeaders
    unchanged: bool = False


class PackageDownloader:
    """Handle URL resolution and package download lifecycle."""

    def __init__(
        self,
        original_url: str,
        url_type: str,
        download_dir: Path,
        db: VersionDatabase,
        keep_downloads: bool = False,
    ) -> None:
        self.original_url = original_url
        self.url_type = url_type
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.db = db
        self.keep_downloads = keep_downloads
        self.downloaded_file: Optional[Path] = None

    # Public API -----------------------------------------------------
    def resolve_download_url(self) -> str:
        if self.url_type == "metadata_json":
            return self._resolve_from_metadata(self.original_url)
        if self.url_type == "github_release":
            return self._resolve_from_github_release(self.original_url)
        return self.original_url

    def download(self, resolved_url: Optional[str] = None) -> DownloadResult:
        download_url = resolved_url or self.resolve_download_url()
        print(f"Checking download target at {download_url}...")
        try:
            head_headers, head_actual_url = self._head_request(download_url)
        except (URLError, HTTPError) as exc:
            print(f"✗ Error checking remote headers: {exc}")
            cached = self._fallback_to_cached_file()
            if cached:
                return DownloadResult(cached, None, DownloadHeaders(None, None, None))
            raise

        unchanged = False
        if head_actual_url:
            unchanged = self._is_cached(head_actual_url, head_headers)
            if unchanged:
                print("ℹ️  Skipping download - no changes detected")
                return DownloadResult(None, head_actual_url, head_headers, unchanged=True)

        try:
            download_headers, actual_url, file_path = self._stream_download(download_url)
        except (URLError, HTTPError) as exc:
            print(f"✗ Error downloading file: {exc}")
            cached = self._fallback_to_cached_file()
            if cached:
                return DownloadResult(cached, None, DownloadHeaders(None, None, None))
            raise

        self.downloaded_file = file_path
        return DownloadResult(file_path, actual_url, download_headers)

    def cleanup(self) -> None:
        if self.keep_downloads:
            return
        if self.downloaded_file and self.downloaded_file.exists():
            try:
                print(f"Cleaning up downloaded file: {self.downloaded_file.name}")
                self.downloaded_file.unlink()
            except OSError as exc:
                print(f"✗ Failed to remove downloaded file: {exc}")

    # Internal helpers -----------------------------------------------
    def _resolve_from_metadata(self, url: str) -> str:
        print(f"Fetching metadata from {url}...")
        try:
            with urlopen(url, timeout=30) as response:
                data = response.read()
        except Exception as exc:
            print(f"✗ Error resolving metadata URL: {exc}")
            return url

        text = self._decode_payload(data)
        try:
            metadata = json.loads(text)
        except json.JSONDecodeError as exc:
            print(f"✗ Could not parse metadata JSON: {exc}")
            return url

        manifest_url = metadata.get("ManifestUrl")
        if not manifest_url:
            print("✗ No ManifestUrl found in metadata")
            return url

        print(f"✓ Found manifest URL: {manifest_url}")

        manifest_headers = self._fetch_manifest_headers(manifest_url)
        if manifest_headers:
            cached_manifest = self.db.get_latest_headers_for_url(manifest_url)
            if self._manifest_is_unchanged(manifest_headers, cached_manifest):
                cached_url = cached_manifest.get("actual_url") if cached_manifest else None
                if cached_url:
                    print(f"✓ Using cached PKG URL: {cached_url}")
                    return cached_url

        pkg_url = self._download_manifest(manifest_url, manifest_headers)
        return pkg_url or url

    def _resolve_from_github_release(self, url: str) -> str:
        """Resolve a GitHub releases URL (e.g. /releases/latest) to an asset URL."""
        repo = self._parse_github_repo(url)
        if not repo:
            print(f"✗ Could not parse GitHub repo from URL: {url}")
            return url

        owner, name = repo
        # Detect a specific tag in the URL: /releases/tag/<tag>
        tag_match = re.search(r"/releases/tag/([^/?#]+)", url)
        if tag_match:
            api_url = f"https://api.github.com/repos/{owner}/{name}/releases/tags/{tag_match.group(1)}"
        else:
            api_url = f"https://api.github.com/repos/{owner}/{name}/releases/latest"

        print(f"Querying GitHub release: {api_url}")
        request = Request(api_url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "appledevicepolicy-tracker",
        })
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            request.add_header("Authorization", f"Bearer {token}")

        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (URLError, HTTPError) as exc:
            print(f"✗ GitHub API request failed: {exc}")
            return url
        except json.JSONDecodeError as exc:
            print(f"✗ Could not parse GitHub API response: {exc}")
            return url

        assets = payload.get("assets") or []
        asset_url = self._pick_github_asset(assets)
        if not asset_url:
            print("✗ No suitable asset found in GitHub release")
            return url

        tag = payload.get("tag_name") or "latest"
        print(f"✓ Resolved GitHub release {tag} asset: {asset_url}")
        return asset_url

    @staticmethod
    def _parse_github_repo(url: str) -> Optional[Tuple[str, str]]:
        try:
            parsed = urlparse(url)
        except ValueError:
            return None
        host = (parsed.netloc or "").lower()
        if host not in ("github.com", "www.github.com"):
            return None
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            return None
        return parts[0], parts[1]

    @staticmethod
    def _pick_github_asset(assets: List[Dict]) -> Optional[str]:
        candidates = [a for a in assets if a.get("browser_download_url")]
        for ext in GITHUB_ASSET_PRIORITY:
            for asset in candidates:
                name = (asset.get("name") or "").lower()
                if name.endswith(ext):
                    return asset["browser_download_url"]
        # Fall back to the first asset if nothing matched preferred extensions.
        return candidates[0]["browser_download_url"] if candidates else None

    @staticmethod
    def _decode_payload(data: bytes) -> str:
        if data.startswith(b"\xff\xfe"):
            return data.decode("utf-16")
        if data.startswith(b"\xef\xbb\xbf"):
            return data[3:].decode("utf-8")
        return data.decode("utf-8")

    def _fetch_manifest_headers(self, manifest_url: str) -> Optional[DownloadHeaders]:
        print("Checking manifest headers...")
        head_request = Request(manifest_url, method="HEAD")
        try:
            with urlopen(head_request, timeout=30) as head_response:
                headers = DownloadHeaders(
                    last_modified=head_response.headers.get("Last-Modified"),
                    etag=head_response.headers.get("ETag"),
                    content_length=head_response.headers.get("Content-Length"),
                )
                if headers.etag:
                    print(f"Manifest ETag: {headers.etag}")
                if headers.last_modified:
                    print(f"Manifest Last-Modified: {headers.last_modified}")
                return headers
        except Exception as exc:
            print(f"⚠️  Unable to fetch manifest headers: {exc}")
        return None

    def _manifest_is_unchanged(
        self,
        manifest_headers: DownloadHeaders,
        cached_manifest: Optional[Dict[str, Optional[str]]],
    ) -> bool:
        if not cached_manifest:
            return False
        etag = manifest_headers.etag
        last_modified = manifest_headers.last_modified
        cached_etag = cached_manifest.get("etag")
        cached_last_modified = cached_manifest.get("last_modified")

        if etag and cached_etag and etag == cached_etag:
            print("✓ Manifest ETag matches - using cached PKG URL")
            return True
        if last_modified and cached_last_modified and last_modified == cached_last_modified:
            print("✓ Manifest Last-Modified matches - using cached PKG URL")
            return True
        return False

    def _download_manifest(
        self,
        manifest_url: str,
        manifest_headers: Optional[DownloadHeaders],
    ) -> Optional[str]:
        print("Downloading manifest...")
        try:
            with urlopen(manifest_url, timeout=30) as plist_response:
                plist_data = plist_response.read()
        except Exception as exc:
            print(f"✗ Failed to download manifest: {exc}")
            return None

        try:
            root = ET.fromstring(plist_data)
        except ET.ParseError as exc:
            print(f"✗ Could not parse manifest plist: {exc}")
            return None

        for array_elem in root.findall(".//array"):
            for dict_elem in array_elem.findall("dict"):
                found_url_key = False
                for child in dict_elem:
                    if child.tag == "key" and child.text == "url":
                        found_url_key = True
                    elif found_url_key and child.tag == "string":
                        pkg_url = child.text
                        if pkg_url and ".pkg" in pkg_url:
                            print(f"✓ Resolved to PKG URL: {pkg_url}")
                            try:
                                self.db.store_manifest_headers(
                                    manifest_url=manifest_url,
                                    pkg_url=pkg_url,
                                    etag=manifest_headers.etag if manifest_headers else None,
                                    last_modified=manifest_headers.last_modified if manifest_headers else None,
                                )
                            except Exception as exc:  # pragma: no cover - defensive
                                print(f"⚠️  Could not cache manifest headers: {exc}")
                            return pkg_url
                        found_url_key = False
        print("✗ Could not find PKG URL in plist")
        return None

    def _head_request(self, download_url: str) -> Tuple[DownloadHeaders, Optional[str]]:
        head_request = Request(
            download_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            },
        )
        head_request.get_method = lambda: "HEAD"
        with urlopen(head_request, timeout=30) as response:
            actual_url = response.geturl()
            print(f"✓ Redirected to: {actual_url}")
            headers = DownloadHeaders(
                last_modified=response.headers.get("Last-Modified"),
                etag=response.headers.get("ETag"),
                content_length=response.headers.get("Content-Length"),
            )
            if headers.content_length:
                print(f"Remote file size: {headers.content_length} bytes")
            else:
                print("Remote file size: unknown")
            if headers.last_modified:
                print(f"Last-Modified: {headers.last_modified}")
            if headers.etag:
                print(f"ETag: {headers.etag}")
            return headers, actual_url

    def _is_cached(self, actual_url: str, headers: DownloadHeaders) -> bool:
        cached_headers = self.db.get_latest_headers_for_url(actual_url)
        if not cached_headers:
            return False
        cached_etag = cached_headers.get("etag")
        cached_last_modified = cached_headers.get("last_modified")
        cached_size = cached_headers.get("size_bytes")
        cached_actual_url = cached_headers.get("actual_url")

        if headers.etag and cached_etag and headers.etag == cached_etag:
            print("✓ ETag matches - file unchanged")
            return True
        if headers.last_modified and cached_last_modified and headers.last_modified == cached_last_modified:
            print("✓ Last-Modified matches - file unchanged")
            return True
        # Some CDNs (notably Microsoft's onecdn) serve the same immutable
        # asset with slightly different Last-Modified timestamps from
        # different edge nodes and don't return ETags. The actual_url
        # almost always contains the version (e.g. Microsoft_Excel_X.Y.Z.pkg),
        # so an exact URL match plus identical Content-Length is a strong
        # enough signal to skip re-downloading the same bytes.
        if (
            cached_actual_url
            and cached_actual_url == actual_url
            and headers.content_length
            and str(cached_size) == headers.content_length
        ):
            print("✓ URL and Content-Length match - file unchanged")
            return True
        if headers.content_length and str(cached_size) == headers.content_length:
            print("⚠️  Content-Length matches but headers differ - will re-download to verify")
        return False

    def _stream_download(self, download_url: str) -> Tuple[DownloadHeaders, str, Path]:
        request = Request(
            download_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            },
        )
        with urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
            actual_url = response.geturl()
            headers = DownloadHeaders(
                last_modified=response.headers.get("Last-Modified"),
                etag=response.headers.get("ETag"),
                content_length=response.headers.get("Content-Length"),
            )
            filename = self._resolve_filename(response, actual_url)
            file_path = self.download_dir / filename
            file_size = int(headers.content_length or 0)
            downloaded = 0
            chunk_size = 8192
            with file_path.open("wb") as handle:
                while True:
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if file_size > 0:
                        percent = (downloaded / file_size) * 100
                        print(f"\rProgress: {percent:.1f}% ({downloaded}/{file_size} bytes)", end="")
            print("\n✓ Download complete")
        return headers, actual_url, file_path

    def _resolve_filename(self, response, actual_url: str) -> str:
        filename = actual_url.split("/")[-1] or "download.pkg"
        content_disp = response.headers.get("Content-Disposition")
        if content_disp and "filename=" in content_disp:
            filename = content_disp.split("filename=")[-1].strip('"\'')
        if "filename*=UTF-8" in filename:
            filename = filename.split("''")[-1] if "''" in filename else filename.split("=")[-1]
        return filename

    def _fallback_to_cached_file(self) -> Optional[Path]:
        existing_files: List[Path] = []
        for pattern in ("*.pkg*", "*.dmg*", "*.zip*"):
            existing_files.extend(self.download_dir.glob(pattern))
        if not existing_files:
            return None
        latest_file = max(existing_files, key=lambda p: p.stat().st_mtime)
        print(f"⚠️  Using cached file instead: {latest_file.name}")
        return latest_file
