"""Orchestrates download, extraction, and plist parsing for macOS packages."""

from __future__ import annotations

import plistlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.error import HTTPError, URLError

from .database import VersionDatabase
from .download import PackageDownloader
from .extraction import PackageExtractor


def construct_full_path(install_location: str, relative_path: str) -> Optional[str]:
    if not relative_path:
        return None
    clean_path = relative_path.lstrip("./")
    if install_location.endswith("/"):
        return install_location + clean_path
    return install_location + "/" + clean_path


class MicrosoftAppAnalyzer:
    """Download, extract, and capture metadata for Microsoft macOS packages."""

    def __init__(
        self,
        app_name: str,
        download_url: str,
        expected_identifier: Optional[str] = None,
        package_type: str = "single",
        download_dir: Union[str, Path] = "./downloads",
        db_path: str = "microsoft_apps_versions.db",
        keep_downloads: bool = False,
        url_type: str = "direct",
    ) -> None:
        self.app_name = app_name
        self.original_url = download_url
        self.expected_identifier = expected_identifier
        self.package_type = package_type
        self.keep_downloads = keep_downloads
        self.db = VersionDatabase(db_path)
        self.download_dir = Path(download_dir)
        self.downloader = PackageDownloader(
            original_url=download_url,
            url_type=url_type,
            download_dir=self.download_dir,
            db=self.db,
            keep_downloads=keep_downloads,
        )
        self.extractor = PackageExtractor()
        self.download_url = download_url

    # ------------------------------------------------------------------
    @staticmethod
    def _find_top_level_app_bundles(directory: Path) -> List[Path]:
        """Find .app bundles at the top level or one level deep (e.g. DMG volumes).

        Excludes .app bundles nested inside another .app (frameworks, plugins).
        """
        bundles = list(Path(directory).glob("*.app"))
        if not bundles:
            bundles = [
                p for p in Path(directory).rglob("*.app")
                if not any(part.endswith(".app") for part in p.relative_to(directory).parts[:-1])
            ]
        return bundles

    # ------------------------------------------------------------------
    def analyze(self) -> Optional[Union[List[Dict[str, Any]], Dict[str, Any]]]:
        print("=" * 60)
        print(f"Microsoft {self.app_name} Analyzer")
        print("=" * 60)

        try:
            resolved_url = self.downloader.resolve_download_url()
            self.download_url = resolved_url
            try:
                download_result = self.downloader.download(resolved_url)
            except (URLError, HTTPError):
                print("✗ Download failed")
                return None

            if download_result.unchanged:
                print("\n" + "=" * 60)
                print("✓ No changes detected - skipping analysis")
                print("=" * 60)
                return {"unchanged": True}

            if not download_result.file_path:
                print("✗ No package to analyze")
                return None

            extract_dir = self.extractor.extract(download_result.file_path)
            if not extract_dir:
                print("\n✗ Extraction failed. Cannot proceed.")
                return None

            package_info_files = self.extractor.find_package_info(extract_dir)
            if not package_info_files:
                app_bundles = self._find_top_level_app_bundles(extract_dir)
                if app_bundles:
                    print("\n✓ Found .app bundle(s) - analyzing directly...")
                    return self._analyze_app_bundle(
                        app_bundle_path=app_bundles[0],
                        pkg_path=download_result.file_path,
                        actual_url=download_result.actual_url,
                        headers=download_result.headers.as_dict(),
                    )
                print("\n✗ No PackageInfo files found - trying direct Payload extraction...")
                return self._analyze_flat_package(
                    pkg_path=download_result.file_path,
                    extract_dir=extract_dir,
                    actual_url=download_result.actual_url,
                    headers=download_result.headers.as_dict(),
                )

            results: List[Dict[str, object]] = []
            all_packages: List[Dict[str, object]] = []
            print("\n" + "=" * 60)
            print("VERSION INFORMATION")
            print("=" * 60)

            main_package = None
            for pkg_info_path in package_info_files:
                info, bundles, raw_xml = self.parse_package_info(pkg_info_path)
                if not info:
                    continue
                package_data = {
                    "path": pkg_info_path,
                    "info": info,
                    "bundles": bundles,
                    "raw": raw_xml,
                }
                results.append(package_data)
                all_packages.append(package_data)

                print(f"\nPackage Identifier: {info['identifier']}")
                print(f"Package Version: {info['version']}")
                print(f"Install Location: {info['install_location']}")
                print(f"Number of Files: {info.get('num_files', 'N/A')}")
                print(f"Install Size: {info.get('install_kb', 'N/A')} KB")

                if bundles:
                    print("\nMain Application Bundle:")
                    for bundle in bundles:
                        print(f"  Path: {bundle['path']}")
                        print(f"  Bundle ID: {bundle['id']}")
                        print(f"  Version: {bundle['version']}")
                        print(f"  Build: {bundle['build']}")

                print("-" * 60)

                if self.package_type != "suite":
                    if self.expected_identifier:
                        if any(bundle["id"] == self.expected_identifier for bundle in bundles):
                            main_package = package_data
                    elif not main_package:
                        main_package = package_data

            if self.package_type == "suite":
                components, version_info, bundles = self._build_suite_payload(
                    all_packages,
                    download_result.file_path,
                )
            else:
                if not main_package:
                    print("✗ Could not identify main package")
                    return None
                version_info = main_package["info"]
                bundles = main_package["bundles"]
                components = self._build_component_list(all_packages, version_info)

            version_id = self.db.add_version(
                download_url=self.download_url,
                actual_url=download_result.actual_url,
                version=version_info["version"],
                file_path=download_result.file_path,
                package_identifier=self._primary_identifier(version_info, bundles),
                app_path=bundles[0]["path"] if bundles else None,
                bundle_id=bundles[0]["id"] if bundles else None,
                num_files=self._safe_int(version_info.get("num_files")),
                install_kb=self._safe_int(version_info.get("install_kb")),
                components=components or None,
                last_modified=download_result.headers.last_modified,
                etag=download_result.headers.etag,
            )

            if version_id:
                print("\n🎉 New version detected and recorded!")
            else:
                print("\nℹ️  This version is already in the database")
                # Update stored headers so future ETag/Last-Modified checks match
                self.db.update_headers_for_version(
                    version=version_info["version"],
                    checksum=self.db.calculate_checksum(download_result.file_path),
                    last_modified=download_result.headers.last_modified,
                    etag=download_result.headers.etag,
                    actual_url=download_result.actual_url,
                )

            print(f"\nTotal versions tracked: {self.db.get_version_count()}")
            if not version_id:
                return {"unchanged": True, "version": version_info["version"]}
            return results
        finally:
            self.cleanup()

    # ------------------------------------------------------------------
    def parse_package_info(self, package_info_path: Path) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], Optional[ET.Element]]:
        print(f"\nParsing: {package_info_path.name}")
        try:
            tree = ET.parse(package_info_path)
            root = tree.getroot()
        except Exception as exc:
            print(f"✗ Error parsing PackageInfo: {exc}")
            return None, [], None

        info = {
            "identifier": root.get("identifier", "N/A"),
            "version": root.get("version", "N/A"),
            "install_location": root.get("install-location", "/"),
        }

        bundles: List[Dict[str, Optional[str]]] = []
        install_location = info["install_location"]

        for bundle in root.findall(".//bundle"):
            bundle_path = bundle.get("path", "")
            bundle_id = bundle.get("id", "")
            normalized_path = construct_full_path(install_location, bundle_path)
            bundle_entry = {
                "path": normalized_path,
                "relative_path": bundle_path,
                "id": bundle_id,
                "version": bundle.get("CFBundleShortVersionString", "N/A"),
                "build": bundle.get("CFBundleVersion", "N/A"),
            }
            if self.package_type == "suite":
                # For suites, collect only main .app bundles (not frameworks, plugins, etc.)
                if bundle_path.endswith(".app") and "/" not in bundle_path.strip("./"):
                    bundles.append(bundle_entry)
            elif self._is_main_bundle(bundle_entry, bundle_path):
                bundles.append(bundle_entry)
                break

        payload = root.find("payload")
        if payload is not None:
            info["num_files"] = payload.get("numberOfFiles", "N/A")
            info["install_kb"] = payload.get("installKBytes", "N/A")

        return info, bundles, root

    def _is_main_bundle(self, bundle: Dict, bundle_path: str) -> bool:
        if self.package_type == "suite":
            return False
        if self.expected_identifier and bundle["id"] == self.expected_identifier:
            return True
        if bundle_path.endswith(".app") and bundle_path.count("/") == 1:
            return True
        return False

    def _build_suite_payload(self, all_packages: List[Dict[str, Any]], pkg_path: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
        filename = pkg_path.name
        version_match = re.search(r"(\d+\.\d+\.\d+)", filename)
        suite_version = version_match.group(1) if version_match else "unknown"
        bundles: List[Dict] = []
        for pkg_data in all_packages:
            if pkg_data["info"].get("install_location") == "/Applications" and pkg_data["bundles"]:
                bundles = pkg_data["bundles"]
                suite_version = pkg_data["info"].get("version", suite_version)
                break
        info = {
            "identifier": self.expected_identifier or f"suite.{self.app_name.lower().replace(' ', '.')}",
            "version": suite_version,
            "install_location": "/",
            "num_files": "N/A",
            "install_kb": "N/A",
        }
        components = []
        for pkg_data in all_packages:
            pkg_info = pkg_data["info"]
            pkg_bundles = pkg_data["bundles"]
            components.append(
                {
                    "package_identifier": pkg_info["identifier"],
                    "version": pkg_info["version"],
                    "app_path": pkg_bundles[0]["path"] if pkg_bundles else None,
                    "bundle_id": pkg_bundles[0]["id"] if pkg_bundles else None,
                    "install_location": pkg_info["install_location"],
                    "num_files": self._safe_int(pkg_info.get("num_files")),
                    "install_kb": self._safe_int(pkg_info.get("install_kb")),
                }
            )
        return components, info, bundles

    def _build_component_list(self, all_packages: List[Dict[str, Any]], main_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        components = []
        for pkg_data in all_packages:
            pkg_info = pkg_data["info"]
            if pkg_info["identifier"] == main_info["identifier"]:
                continue
            pkg_bundles = pkg_data["bundles"]
            components.append(
                {
                    "package_identifier": pkg_info["identifier"],
                    "version": pkg_info["version"],
                    "app_path": pkg_bundles[0]["path"] if pkg_bundles else None,
                    "bundle_id": pkg_bundles[0]["id"] if pkg_bundles else None,
                    "install_location": pkg_info["install_location"],
                    "num_files": self._safe_int(pkg_info.get("num_files")),
                    "install_kb": self._safe_int(pkg_info.get("install_kb")),
                }
            )
        return components

    def _primary_identifier(self, info: Dict[str, Any], bundles: List[Dict[str, Any]]) -> str:
        # For suite packages, always use the info identifier
        if self.package_type == "suite":
            return info["identifier"]
        # For single packages, prefer bundle ID if available
        if bundles and bundles[0].get("id"):
            return bundles[0]["id"]
        return info["identifier"]

    def _safe_int(self, value: Optional[Union[str, int]]) -> Optional[int]:
        if value in (None, "N/A"):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _analyze_flat_package(
        self,
        pkg_path: Path,
        extract_dir: Path,
        actual_url: Optional[str],
        headers: Dict[str, Optional[str]],
    ) -> Optional[Union[List[Dict[str, Any]], Dict[str, Any]]]:
        payload_dirs = self.extractor.decompress_payload(extract_dir)
        if not payload_dirs:
            print("✗ No Payload directory extracted")
            return None
        payload_dir = payload_dirs[0]
        app_bundles = list(payload_dir.glob("*.app"))
        if not app_bundles:
            print("✗ No .app bundle found in Payload")
            return None
        app_bundle = app_bundles[0]
        info_plist_path = app_bundle / "Contents" / "Info.plist"
        if not info_plist_path.exists():
            print(f"✗ No Info.plist found in {app_bundle.name}")
            return None

        print("\n" + "=" * 60)
        print("VERSION INFORMATION")
        print("=" * 60)

        with info_plist_path.open("rb") as handle:
            plist_data = plistlib.load(handle)

        bundle_id = plist_data.get("CFBundleIdentifier", "N/A")
        version = plist_data.get("CFBundleShortVersionString", "N/A")
        build = plist_data.get("CFBundleVersion", "N/A")

        print(f"\nApp: {app_bundle.name}")
        print(f"Bundle ID: {bundle_id}")
        print(f"Version: {version}")
        print(f"Build: {build}")

        print("\n" + "=" * 60)
        print("UPDATING DATABASE")
        print("=" * 60)

        version_id = self.db.add_version(
            download_url=self.download_url,
            actual_url=actual_url,
            version=version,
            file_path=pkg_path,
            package_identifier=bundle_id,
            app_path=f"/Applications/{app_bundle.name}",
            bundle_id=bundle_id,
            num_files=None,
            install_kb=None,
            components=None,
            last_modified=headers.get("last_modified"),
            etag=headers.get("etag"),
        )

        if version_id:
            print("\n🎉 New version detected and recorded!")
        else:
            print("\nℹ️  This version is already in the database")
            # Update stored headers so future ETag/Last-Modified checks match
            self.db.update_headers_for_version(
                version=version,
                checksum=self.db.calculate_checksum(pkg_path),
                last_modified=headers.get("last_modified"),
                etag=headers.get("etag"),
                actual_url=actual_url,
            )

        print(f"\nTotal versions tracked: {self.db.get_version_count()}")
        if not version_id:
            return {"unchanged": True, "version": version}
        return [
            {
                "app_bundle": app_bundle,
                "version": version,
                "build": build,
                "bundle_id": bundle_id,
            }
        ]

    def _analyze_app_bundle(
        self,
        app_bundle_path: Path,
        pkg_path: Path,
        actual_url: Optional[str],
        headers: Dict[str, Optional[str]],
    ) -> Optional[Union[List[Dict[str, Any]], Dict[str, Any]]]:
        print("\n" + "=" * 60)
        print("ANALYZING .APP BUNDLE")
        print("=" * 60)

        info_plist_path = app_bundle_path / "Contents" / "Info.plist"
        if not info_plist_path.exists():
            print(f"✗ Info.plist not found in {app_bundle_path.name}")
            return None

        with info_plist_path.open("rb") as handle:
            info = plistlib.load(handle)

        bundle_id = info.get("CFBundleIdentifier", "unknown")
        version = info.get("CFBundleShortVersionString", "unknown")
        build = info.get("CFBundleVersion", "")

        print(f"\nApp Bundle: {app_bundle_path.name}")
        print(f"Bundle ID: {bundle_id}")
        print(f"Version: {version}")
        if build:
            print(f"Build: {build}")

        print("\n" + "=" * 60)
        print("UPDATING DATABASE")
        print("=" * 60)

        version_id = self.db.add_version(
            download_url=self.original_url,
            actual_url=actual_url,
            version=version,
            file_path=pkg_path,
            package_identifier=bundle_id,
            app_path=f"/Applications/{app_bundle_path.name}",
            bundle_id=bundle_id,
            last_modified=headers.get("last_modified"),
            etag=headers.get("etag"),
        )

        if version_id is None:
            # Update stored headers so future ETag/Last-Modified checks match
            self.db.update_headers_for_version(
                version=version,
                checksum=self.db.calculate_checksum(pkg_path),
                last_modified=headers.get("last_modified"),
                etag=headers.get("etag"),
                actual_url=actual_url,
            )
            total = self.db.get_version_count()
            print(f"\nTotal versions tracked: {total}")
            return {"unchanged": True, "version": version}

        print(f"✓ Added new version to database (ID: {version_id})")
        print("\n🎉 New version detected and recorded!")
        total = self.db.get_version_count()
        print(f"\nTotal versions tracked: {total}")

        return [
            {
                "app_bundle": app_bundle_path.name,
                "version": version,
                "build": build,
                "bundle_id": bundle_id,
            }
        ]

    def cleanup(self) -> None:
        self.extractor.cleanup()
        self.downloader.cleanup()
        if self.db:
            self.db.close()
