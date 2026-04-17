"""PKG, ZIP, and flat-package extraction using 7z, xar, and cpio."""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional
import zipfile


class PackageExtractor:
    """Extract PKG or ZIP archives into a temporary workspace."""

    def __init__(self) -> None:
        self.temp_dir: Optional[Path] = None

    def extract(self, pkg_path: Path) -> Optional[Path]:
        print(f"\nExtracting package: {pkg_path.name}...")
        self.temp_dir = Path(tempfile.mkdtemp(prefix="companyportal_"))
        print(f"Working directory: {self.temp_dir}")

        try:
            file_type = self._detect_file_type(pkg_path)
            # Check for actual ZIP archives, but exclude bzip2 which contains "zip"
            if file_type and "zip" in file_type.lower() and "bzip" not in file_type.lower():
                print("Detected ZIP archive")
                return self._extract_zip(pkg_path)
            if not file_type or "bzip" not in file_type.lower():
                if zipfile.is_zipfile(pkg_path):
                    print("Detected ZIP archive (fallback check)")
                    return self._extract_zip(pkg_path)

            if not self._check_extraction_tools():
                return None
            if shutil.which("7z"):
                return self._extract_with_7z(pkg_path)
            if shutil.which("xar"):
                return self._extract_with_xar(pkg_path)
            print("✗ No suitable extraction tool found (7z or xar)")
            return None
        except Exception as exc:  # pragma: no cover - defensive logging
            print(f"✗ Error during extraction: {exc}")
            return None

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

    def find_package_info(self, extract_dir: Path) -> List[Path]:
        if not extract_dir:
            return []
        print("\nSearching for PackageInfo files...")
        package_info_files = list(Path(extract_dir).rglob("PackageInfo"))
        if package_info_files:
            print(f"✓ Found {len(package_info_files)} PackageInfo file(s)")
            for pkg_info in package_info_files:
                print(f"  - {pkg_info.relative_to(extract_dir)}")
        else:
            print("✗ No PackageInfo files found")
            app_bundles = self._find_top_level_app_bundles(extract_dir)
            if app_bundles:
                print(f"✓ Found {len(app_bundles)} .app bundle(s) instead")
                for app_bundle in app_bundles:
                    print(f"  - {app_bundle.name}")
        return package_info_files

    def decompress_payload(self, extract_dir: Path) -> Optional[List[Path]]:
        if not extract_dir:
            return None
        print("\nLooking for Payload files...")
        # Match both "Payload" and "Payload~" (flat componentless pkgs use the tilde form)
        payload_files = list(Path(extract_dir).glob("**/Payload"))
        payload_files += [p for p in Path(extract_dir).glob("**/Payload~") if p not in payload_files]
        if not payload_files:
            print("No Payload files found")
            return None

        extracted_dirs = []
        for payload_path in payload_files:
            print(f"Found Payload: {payload_path.relative_to(extract_dir)}")
            payload_dir = payload_path.parent / "Payload_extracted"
            payload_dir.mkdir(exist_ok=True)
            if self._extract_payload_with_7z(payload_path, payload_dir):
                extracted_dirs.append(payload_dir)
                continue
            if self._extract_payload_with_cpio(payload_path, payload_dir):
                extracted_dirs.append(payload_dir)
        return extracted_dirs or None

    def cleanup(self) -> None:
        if self.temp_dir and self.temp_dir.exists():
            print(f"\nCleaning up temporary directory: {self.temp_dir}")
            try:
                shutil.rmtree(self.temp_dir)
                print("✓ Temp directory cleanup complete")
            except OSError as exc:
                print(f"✗ Temp directory cleanup failed: {exc}")

    # Internal helpers --------------------------------------------
    def _detect_file_type(self, pkg_path: Path) -> Optional[str]:
        try:
            result = subprocess.run(
                ["file", "-b", "--mime-type", str(pkg_path)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except Exception as exc:
            print(f"⚠️  Could not detect file type with 'file' command: {exc}")
            return None
        if result.returncode == 0:
            file_type = result.stdout.strip()
            print(f"Detected file type: {file_type}")
            return file_type
        return None

    def _check_extraction_tools(self) -> bool:
        tools = ["7z", "xar"]
        available = [tool for tool in tools if shutil.which(tool)]
        if not available:
            print("✗ Missing required tools. Please install one of: 7z, xar")
            print("  Ubuntu/Debian: sudo apt-get install p7zip-full")
            print("  or: sudo apt-get install xar")
            return False
        print(f"✓ Found extraction tool(s): {', '.join(available)}")
        return True

    def _extract_with_7z(self, pkg_path: Path) -> Optional[Path]:
        print("Using 7z for extraction...")
        result = subprocess.run(
            ["7z", "x", str(pkg_path), f"-o{self.temp_dir}", "-y"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"✗ 7z extraction failed: {result.stderr}")
            return None
        print("✓ Package extracted successfully")
        return self.temp_dir

    def _extract_with_xar(self, pkg_path: Path) -> Optional[Path]:
        print("Using xar for extraction...")
        result = subprocess.run(
            ["xar", "-xf", str(pkg_path), "-C", str(self.temp_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"✗ xar extraction failed: {result.stderr}")
            return None
        print("✓ Package extracted successfully")
        return self.temp_dir

    def _extract_zip(self, zip_path: Path) -> Optional[Path]:
        print("Using zipfile for extraction...")
        try:
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(self.temp_dir)
        except Exception as exc:
            print(f"✗ Error extracting ZIP: {exc}")
            return None
        print("✓ ZIP archive extracted successfully")
        app_bundles = list(self.temp_dir.glob("*.app"))
        if app_bundles:
            print(f"✓ Found {len(app_bundles)} .app bundle(s)")
            return self.temp_dir
        print("✗ No .app bundle found in ZIP")
        return None

    def _extract_payload_with_7z(self, payload_path: Path, payload_dir: Path) -> bool:
        if not shutil.which("7z"):
            return False
        print("Decompressing Payload with 7z...")
        result = subprocess.run(
            ["7z", "x", str(payload_path), f"-o{payload_dir}", "-y"],
            capture_output=True,
            text=True,
        )
        # 7z may return non-zero for symlink warnings (common with Electron
        # frameworks) yet still extract the .app bundle successfully.
        app_bundles = list(payload_dir.glob("*.app"))
        if result.returncode == 0 or app_bundles:
            print(f"✓ Payload extracted to {payload_dir}")
            if result.returncode != 0:
                print(f"  (7z exited {result.returncode} -- non-fatal warnings ignored)")
            self._list_directory_structure(payload_dir)
            return True
        print(f"✗ 7z Payload extraction failed: {result.stderr}")
        return False

    def _extract_payload_with_cpio(self, payload_path: Path, payload_dir: Path) -> bool:
        print("Trying gunzip + cpio method...")
        try:
            with payload_path.open("rb") as f_in:
                decompressed = gzip.decompress(f_in.read())
        except Exception as exc:
            print(f"✗ Error decompressing Payload: {exc}")
            return False
        cpio_process = subprocess.Popen(
            ["cpio", "-idm"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=payload_dir,
        )
        stdout, stderr = cpio_process.communicate(input=decompressed)
        if cpio_process.returncode == 0:
            print(f"✓ Payload extracted to {payload_dir}")
            self._list_directory_structure(payload_dir)
            return True
        print(f"✗ cpio extraction failed: {stderr.decode()}")
        return False

    def _list_directory_structure(self, directory: Path, max_depth: int = 3) -> None:
        print(f"\nDirectory structure of {directory.name}:")
        try:
            for root, dirs, files in os.walk(directory):
                level = root.replace(str(directory), "").count(os.sep)
                if level >= max_depth:
                    dirs.clear()
                    continue
                indent = "  " * level
                print(f"{indent}{Path(root).name}/")
                subindent = "  " * (level + 1)
                for file in files[:10]:
                    print(f"{subindent}{file}")
                if len(files) > 10:
                    print(f"{subindent}... and {len(files) - 10} more files")
        except Exception as exc:
            print(f"Could not list directory: {exc}")
