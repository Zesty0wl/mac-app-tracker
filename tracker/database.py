#!/usr/bin/env python3
"""
SQLite database layer for storing app versions and component metadata.

Provides VersionDatabase with methods for recording detected versions,
querying history, and managing the schema (versions, components tables).
"""

import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path


class VersionDatabase:
    def __init__(self, db_path="company_portal_versions.db"):
        self.db_path = Path(db_path)
        self.conn = None
        self.init_database()
    
    def init_database(self):
        """Initialize the database and create tables if they don't exist"""
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row  # Enable column access by name
        
        cursor = self.conn.cursor()
        
        # Create versions table (main packages)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_detected TIMESTAMP NOT NULL,
                download_url TEXT NOT NULL,
                actual_url TEXT,
                version TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                checksum_sha256 TEXT NOT NULL,
                package_identifier TEXT,
                app_path TEXT,
                bundle_id TEXT,
                num_files INTEGER,
                install_kb INTEGER,
                last_modified TEXT,
                etag TEXT,
                UNIQUE(version, checksum_sha256)
            )
        """)
        
        # Add new columns if they don't exist (for existing databases)
        try:
            cursor.execute("ALTER TABLE versions ADD COLUMN last_modified TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        try:
            cursor.execute("ALTER TABLE versions ADD COLUMN etag TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists
        
        # Create components table (sub-packages within a main package)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS components (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                version_id INTEGER NOT NULL,
                package_identifier TEXT NOT NULL,
                version TEXT NOT NULL,
                app_path TEXT,
                bundle_id TEXT,
                install_location TEXT,
                num_files INTEGER,
                install_kb INTEGER,
                FOREIGN KEY (version_id) REFERENCES versions(id) ON DELETE CASCADE
            )
        """)
        
        # Create index for faster lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_version 
            ON versions(version)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_detected 
            ON versions(first_detected DESC)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_component_version 
            ON components(version_id)
        """)
        
        self.conn.commit()
    
    def calculate_checksum(self, file_path):
        """Calculate SHA256 checksum of a file"""
        sha256_hash = hashlib.sha256()
        
        with open(file_path, "rb") as f:
            # Read in chunks to handle large files
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        
        return sha256_hash.hexdigest()
    
    def version_exists(self, version, checksum):
        """Check if a version with this checksum already exists"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT id FROM versions 
            WHERE version = ? AND checksum_sha256 = ?
        """, (version, checksum))
        
        return cursor.fetchone() is not None

    def update_headers_for_version(self, version, checksum, last_modified=None, etag=None, actual_url=None):
        """Update stored headers for an existing version.
        
        Called when a re-download discovers the same version but with
        different ETag/Last-Modified headers (e.g. CDN rotation).
        This prevents repeated unnecessary re-downloads.
        """
        cursor = self.conn.cursor()
        updates = []
        params = []
        if etag is not None:
            updates.append("etag = ?")
            params.append(etag)
        if last_modified is not None:
            updates.append("last_modified = ?")
            params.append(last_modified)
        if actual_url is not None:
            updates.append("actual_url = ?")
            params.append(actual_url)
        if not updates:
            return
        params.extend([version, checksum])
        cursor.execute(f"""
            UPDATE versions SET {', '.join(updates)}
            WHERE version = ? AND checksum_sha256 = ?
        """, params)
        self.conn.commit()
        if cursor.rowcount > 0:
            print(f"✓ Updated stored headers for existing version {version}")
    
    def add_version(self, download_url, version, file_path, 
                   actual_url=None, package_identifier=None, app_path=None, bundle_id=None,
                   num_files=None, install_kb=None, components=None, 
                   last_modified=None, etag=None):
        """Add a new version to the database
        
        Args:
            components: List of dict with component info, e.g.:
                [{'package_identifier': 'com.microsoft.dlp.agent', 
                  'version': '1.25082.101',
                  'app_path': './com.microsoft.dlp.agent.app',
                  'bundle_id': 'com.microsoft.dlp.agent',
                  'install_location': '/Library/Application Support/Microsoft/DLP',
                  'num_files': 79,
                  'install_kb': 6597}, ...]
        """
        
        # Calculate checksum
        print(f"Calculating checksum for {file_path.name}...")
        checksum = self.calculate_checksum(file_path)
        print(f"✓ SHA256: {checksum}")
        
        # Get file size
        size_bytes = file_path.stat().st_size
        
        # Check if this exact version already exists
        if self.version_exists(version, checksum):
            print(f"ℹ Version {version} with this checksum already exists in database")
            return None
        
        # Insert new version
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO versions (
                first_detected, download_url, actual_url, version, 
                size_bytes, checksum_sha256, package_identifier,
                app_path, bundle_id, num_files, install_kb,
                last_modified, etag
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now().isoformat(),
            download_url,
            actual_url,
            version,
            size_bytes,
            checksum,
            package_identifier,
            app_path,
            bundle_id,
            num_files,
            install_kb,
            last_modified,
            etag
        ))
        
        version_id = cursor.lastrowid
        
        # Add components if provided
        if components:
            for component in components:
                cursor.execute("""
                    INSERT INTO components (
                        version_id, package_identifier, version,
                        app_path, bundle_id, install_location,
                        num_files, install_kb
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    version_id,
                    component.get('package_identifier'),
                    component.get('version'),
                    component.get('app_path'),
                    component.get('bundle_id'),
                    component.get('install_location'),
                    component.get('num_files'),
                    component.get('install_kb')
                ))
            print(f"✓ Added {len(components)} component(s)")
        
        self.conn.commit()
        
        print(f"✓ Added new version to database (ID: {version_id})")
        return version_id
    
    def get_latest_version(self):
        """Get the most recently detected version"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM versions 
            ORDER BY first_detected DESC 
            LIMIT 1
        """)
        
        return cursor.fetchone()
    
    def get_latest_headers_for_url(self, actual_url):
        """Get the most recent Last-Modified and ETag headers for a given URL
        
        Checks both actual_url (for PKG files) and download_url (for manifest caches)
        """
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT last_modified, etag, size_bytes, actual_url 
            FROM versions 
            WHERE actual_url = ? OR download_url = ?
            ORDER BY first_detected DESC 
            LIMIT 1
        """, (actual_url, actual_url))
        
        result = cursor.fetchone()
        if result:
            return {
                'last_modified': result['last_modified'],
                'etag': result['etag'],
                'size_bytes': result['size_bytes'],
                'actual_url': result['actual_url']
            }
        return None
    
    def get_all_versions(self):
        """Get all versions ordered by detection date"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM versions 
            ORDER BY first_detected DESC
        """)
        
        return cursor.fetchall()
    
    def get_version_count(self):
        """Get total number of versions tracked"""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM versions")
        result = cursor.fetchone()
        return result['count'] if result else 0
    
    def get_components_for_version(self, version_id):
        """Get all components for a specific version"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT * FROM components 
            WHERE version_id = ?
            ORDER BY package_identifier
        """, (version_id,))
        
        return cursor.fetchall()
    
    def print_version_history(self):
        """Print a formatted version history"""
        versions = self.get_all_versions()
        
        if not versions:
            print("No versions in database yet.")
            return
        
        print("\n" + "=" * 80)
        print("VERSION HISTORY")
        print("=" * 80)
        
        for v in versions:
            print(f"\nDetected: {v['first_detected']}")
            print(f"Version: {v['version']}")
            print(f"Package Identifier: {v['package_identifier']}")
            print(f"App Path: {v['app_path']}")
            print(f"Bundle ID: {v['bundle_id']}")
            print(f"Size: {v['size_bytes']:,} bytes ({v['size_bytes'] / 1024 / 1024:.2f} MB)")
            print(f"Checksum: {v['checksum_sha256']}")
            print(f"Download URL: {v['download_url']}")
            if v['actual_url']:
                print(f"Actual URL: {v['actual_url']}")
            if v['num_files']:
                print(f"Files: {v['num_files']}")
            if v['install_kb']:
                print(f"Install Size: {v['install_kb']} KB")
            
            # Show components
            components = self.get_components_for_version(v['id'])
            if components:
                print(f"\nComponents ({len(components)}):")
                for comp in components:
                    print(f"  • {comp['package_identifier']}")
                    print(f"    Version: {comp['version']}")
                    if comp['app_path']:
                        print(f"    Path: {comp['app_path']}")
                    if comp['install_location']:
                        print(f"    Location: {comp['install_location']}")
                    if comp['num_files']:
                        print(f"    Files: {comp['num_files']}, Size: {comp['install_kb']} KB")
            
            print("-" * 80)
    
    def export_to_json(self, output_path="versions.json"):
        """Export version history to JSON including components"""
        import json
        
        versions = self.get_all_versions()
        
        # Convert Row objects to dictionaries and include components
        versions_list = []
        for v in versions:
            version_dict = dict(v)
            
            # Add components
            components = self.get_components_for_version(v['id'])
            version_dict['components'] = [dict(comp) for comp in components]
            
            versions_list.append(version_dict)
        
        with open(output_path, 'w') as f:
            json.dump(versions_list, f, indent=2)
        
        print(f"✓ Exported {len(versions_list)} versions to {output_path}")
    
    def store_manifest_headers(self, manifest_url, pkg_url, etag=None, last_modified=None):
        """Store manifest headers for efficient caching
        
        This stores the manifest URL as the download_url and the PKG URL as actual_url,
        allowing us to check if the manifest has changed without downloading it.
        We use a fake version/checksum to avoid conflicts with real version entries.
        """
        cursor = self.conn.cursor()
        
        # Check if we already have an entry for this manifest URL
        cursor.execute("""
            SELECT id FROM versions 
            WHERE download_url = ? AND package_identifier = '__manifest_cache__'
        """, (manifest_url,))
        
        existing = cursor.fetchone()
        
        if existing:
            # Update the existing manifest cache entry
            cursor.execute("""
                UPDATE versions 
                SET actual_url = ?, last_modified = ?, etag = ?, first_detected = ?
                WHERE id = ?
            """, (pkg_url, last_modified, etag, datetime.now(), existing['id']))
        else:
            # Insert new manifest cache entry
            cursor.execute("""
                INSERT INTO versions (
                    first_detected, download_url, actual_url, version, 
                    size_bytes, checksum_sha256, package_identifier,
                    last_modified, etag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(),
                manifest_url,      # Store manifest URL as download_url
                pkg_url,          # Store PKG URL as actual_url  
                '__manifest__',   # Fake version
                0,                # No size
                '__manifest__',   # Fake checksum
                '__manifest_cache__',  # Special identifier
                last_modified,
                etag
            ))
        
        self.conn.commit()
    
    def mark_url_as_removed(self, version_id):
        """Mark a version's actual_url as [download removed]"""
        cursor = self.conn.cursor()
        cursor.execute("""
            UPDATE versions 
            SET actual_url = '[download removed]'
            WHERE id = ?
        """, (version_id,))
        self.conn.commit()
    
    def get_all_reachable_urls(self):
        """Get all unique actual URLs that haven't been marked as removed"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT DISTINCT id, actual_url 
            FROM versions 
            WHERE actual_url IS NOT NULL 
            AND actual_url != '[download removed]'
            ORDER BY first_detected DESC
        """)
        return cursor.fetchall()
    
    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()
    
    def update_last_check_time(self):
        """Update the last check timestamp"""
        cursor = self.conn.cursor()
        now = datetime.now().isoformat()
        
        # Use a special package_identifier to store the last check time
        cursor.execute("""
            INSERT OR REPLACE INTO versions (
                first_detected, download_url, version, size_bytes, 
                checksum_sha256, package_identifier
            ) VALUES (?, ?, ?, ?, ?, ?)
        """, (now, '__last_check__', '__last_check__', 0, '__last_check__', '__last_check__'))
        
        self.conn.commit()
    
    def get_last_check_time(self):
        """Get the last check timestamp"""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT first_detected FROM versions 
            WHERE package_identifier = '__last_check__'
            LIMIT 1
        """)
        row = cursor.fetchone()
        return row['first_detected'] if row else None
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def main():
    """Test the database functionality"""
    with VersionDatabase() as db:
        print(f"Database initialized: {db.db_path}")
        print(f"Total versions tracked: {db.get_version_count()}")
        
        if db.get_version_count() > 0:
            db.print_version_history()


if __name__ == "__main__":
    main()
