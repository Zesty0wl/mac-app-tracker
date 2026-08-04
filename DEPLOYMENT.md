# Deployment

## Docker (recommended)

The tracker runs as a single Docker container (web app + hourly scheduler)
listening on port 5000, with all state in a `/data` volume. Put a reverse
proxy in front of it for production (see `nginx/` for a sample config).

### Prebuilt images

Multi-arch images (linux/amd64 + linux/arm64) are published to GitHub
Container Registry by `.github/workflows/docker-publish.yml` on every push
to main:

- `ghcr.io/zesty0wl/mac-app-tracker:latest` — current main
- `ghcr.io/zesty0wl/mac-app-tracker:<version>` — e.g. `1.3.0`, from the `VERSION` file
- `ghcr.io/zesty0wl/mac-app-tracker:sha-<short sha>` — pin an exact build

See the README Quick Start for a ready-to-use `docker-compose.yml`. Update
with `docker compose pull && docker compose up -d`.

### Management Commands

```bash
docker compose logs -f        # view logs
docker compose restart        # restart
docker compose down           # stop
docker compose up -d          # start (pulls the image if missing)
docker compose pull && docker compose up -d   # update to latest published image
GIT_SHA=$(git rev-parse --short HEAD) docker compose build   # rebuild from source
```

### Database Location
- Container: `/data/microsoft_apps_versions.db`
- Host: `./data/microsoft_apps_versions.db`

### Scheduler
The container runs a scheduler that checks for new versions every hour (3600 seconds). Logs are visible via `docker compose logs -f`.

### Health Check
The container includes a health check that verifies the API is responding every 30 seconds.

### Next Steps
1. Monitor the first few hourly checks to ensure stability
2. Consider setting up alerting for container failures
3. Add monitoring/metrics if needed
