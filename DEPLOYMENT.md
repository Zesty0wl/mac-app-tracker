# Deployment Summary

## Docker Container

The Mac Apps Version Tracker is now deployed as a Docker container.

### Status
- **Container Name**: intune-mac-tracker
- **Local Port**: 5000
- **Public URL**: http://localhost:5000 (configure reverse proxy for production)
- **Status**: Running with auto-restart

### Features
- ✅ Automatic hourly version checks
- ✅ HTTP header-based change detection (ETag/Last-Modified)
- ✅ Modern web UI for browsing version history
- ✅ Persistent database in `./data/` directory
- ✅ Automatic cleanup of downloaded packages

### Management Commands

#### View Logs
```bash
docker-compose logs -f
```

#### Restart Container
```bash
docker-compose restart
```

#### Stop Container
```bash
docker-compose down
```

#### Start Container
```bash
docker-compose up -d
```

#### Rebuild Container
```bash
docker-compose up --build -d
```

### Database Location
- Container: `/data/microsoft_apps_versions.db`
- Host: `./data/microsoft_apps_versions.db`

### Nginx Configuration
Location: `/etc/nginx/sites-available/appledevicepolicy`

The `/app-tracker` path proxies to `localhost:5000`

### Scheduler
The container runs a scheduler that checks for new versions every hour (3600 seconds). Logs are visible via `docker-compose logs -f`.

### Health Check
The container includes a health check that verifies the API is responding every 30 seconds.

### Next Steps
1. Monitor the first few hourly checks to ensure stability
2. Consider setting up alerting for container failures
3. Add monitoring/metrics if needed
