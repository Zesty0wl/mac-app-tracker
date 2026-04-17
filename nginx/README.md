# Nginx reverse-proxy example

App Tracker mounts itself at the `/app-tracker` path prefix internally
(see [`web_app.py`](../web_app.py) — it sets `SCRIPT_NAME=/app-tracker`).
If you want to serve the tracker behind nginx, use this as a starting
point.

## Files

- [`app-tracker.conf`](app-tracker.conf) — minimal HTTPS reverse-proxy
  example. Serves the tracker at `https://<your-domain>/app-tracker/`
  and redirects `/` to that path. TLS certs are expected at the paths
  shown; swap them for whatever your setup uses (Let's Encrypt,
  Cloudflare origin cert, etc.).

## Setup

1. **DNS** — point your domain at the server running the container.
2. **Run the container** — `docker compose up -d` (default port
   `5000`, change in `docker-compose.yml` if needed).
3. **Copy the config** —
   ```bash
   sudo cp nginx/app-tracker.conf /etc/nginx/sites-available/app-tracker
   sudo ln -s /etc/nginx/sites-available/app-tracker /etc/nginx/sites-enabled/
   ```
4. **Edit `server_name`** and the TLS cert paths in the copied file.
5. **If your container isn't on `127.0.0.1:5000`**, update the
   `proxy_pass` line to match.
6. **Test & reload**:
   ```bash
   sudo nginx -t && sudo systemctl reload nginx
   ```
7. **Verify**: `curl -I https://<your-domain>/app-tracker/` should
   return `200 OK`.

## Why the `/app-tracker` prefix?

The Flask app is reverse-proxy-aware and injects `SCRIPT_NAME` so that
all `url_for()` calls and email links include the prefix. This lets
you co-host the tracker alongside other services on the same domain
without URL collisions. If you want it at root instead, that's a code
change — see [`web_app.py`](../web_app.py) around the `ReverseProxied`
class.

## Serving multiple apps on one domain

The same pattern works if you're co-hosting several tools — add more
`location` blocks for each path prefix and proxy to the corresponding
container port. Set `SITE_URL` in each app's `.env` to the public
HTTPS origin so email links come out correct.
