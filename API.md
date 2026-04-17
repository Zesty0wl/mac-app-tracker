# Mac Apps Version Tracker - API Documentation

## Base URL
```
http://localhost:5000
```

## Endpoints

### GET `/api/latest`
Returns the latest version information for all tracked Mac applications in a script-friendly JSON format.

**Response Format:**
```json
{
  "generated": "2025-10-31T12:56:27",
  "apps": [
    {
      "name": "Company Portal",
      "package_identifier": "com.microsoft.CompanyPortalMac",
      "version": "5.2508.1",
      "detected": "2025-10-31T12:38:59.822899",
      "download_url": "https://go.microsoft.com/fwlink/?linkid=853070",
      "direct_url": "https://officecdn.microsoft.com/pr/.../CompanyPortal-Installer.pkg",
      "size_bytes": 86842811,
      "size_mb": 82.82,
      "sha256": "5a740461bc25d3c28046d4b7b9614edb14eb67c9142dbf8d53ceda459eec6bb9",
      "app_path": "/Applications/Company Portal.app",
      "bundle_id": "com.microsoft.CompanyPortalMac",
      "num_files": 1140,
      "install_kb": 264722,
      "last_modified": "Wed, 08 Oct 2025 16:07:06 GMT",
      "etag": "\"0xFA8BD4C5FFDA026FA6721AC958DC4B47613CDFDE982B206737A52CF16BA8AD94\"",
      "components": [
        {
          "name": "Microsoft AutoUpdate",
          "package_identifier": "com.microsoft.package.Microsoft_AutoUpdate.app",
          "version": "4.80.25092610",
          "bundle_id": "com.microsoft.autoupdate2",
          "app_path": "/Library/Application Support/Microsoft/MAU2.0/Microsoft AutoUpdate.app"
        }
      ]
    }
  ]
}
```

**Example Usage:**

**Shell Script (bash):**
```bash
#!/bin/bash
# Get latest Office Suite version
curl -s "http://localhost:5000/api/latest" | \
  jq -r '.apps[] | select(.package_identifier == "com.microsoft.suite") | .version'
```

**Python:**
```python
import requests

# Get all latest versions
response = requests.get("http://localhost:5000/api/latest")
data = response.json()

for app in data['apps']:
    print(f"{app['name']}: {app['version']} ({app['size_mb']} MB)")
    print(f"  Download: {app['download_url']}")
    print(f"  SHA256: {app['sha256']}")
    if 'components' in app:
        print(f"  Components: {len(app['components'])}")
```

**PowerShell:**
```powershell
# Get Microsoft Edge version
$response = Invoke-RestMethod -Uri "http://localhost:5000/api/latest"
$edge = $response.apps | Where-Object { $_.package_identifier -eq "com.microsoft.edgemac" }
Write-Host "Microsoft Edge version: $($edge.version)"
Write-Host "SHA256: $($edge.sha256)"
```

### GET `/api/apps`
Returns list of all tracked applications.

### GET `/api/app/<package_identifier>/versions`
Returns full version history for a specific application.

### GET `/api/stats`
Returns overall statistics about tracked applications.

## Package Identifiers

- **Company Portal**: `com.microsoft.CompanyPortalMac`
- **Microsoft Edge**: `com.microsoft.edgemac`
- **Defender for Mac**: `com.microsoft.wdav`
- **Office Suite**: `com.microsoft.suite`

## Notes

- The `/api/latest` endpoint is optimized for automation and scripting
- Data is updated hourly
- Components with version "0" (fonts, frameworks, proofing tools) are excluded from the output
- The `direct_url` field contains the actual CDN download URL (resolved from the fwlink)
- All timestamps are in ISO 8601 format
- File sizes are provided in both bytes and megabytes
