# Polar FIT Sync

A self-hosted tool that automatically downloads `.fit` exercise files from your Polar Flow account to a local volume. It handles OAuth linking through a small web UI, then incrementally fetches only new files on a schedule — or instantly when Polar pushes a webhook notification. Runs as a single container, locally with Docker Compose or in Kubernetes.

> Only `.fit` files are downloaded. One Polar account per instance. No TCX, GPX, or upload-to-Strava functionality.

---

## Prerequisites

- A Polar account with at least one exercise recorded
- A Polar app registered at https://www.polar.com/en/developers — you need the **Client ID** and **Client Secret**
- **Docker** (for local testing) or a **Kubernetes cluster** (for production)

---

## Quick Start — Local with Docker Compose

### 1. Register your Polar app redirect URI

In the [Polar developer console](https://www.polar.com/en/developers), set the redirect URI for your app to:

```
http://localhost:8080/oauth/callback
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in the two required values:

```env
POLAR_CLIENT_ID=your_client_id_here
POLAR_CLIENT_SECRET=your_client_secret_here
POLAR_REDIRECT_URI=http://localhost:8080/oauth/callback
```

### 3. Start the container

```bash
docker compose up
```

The service starts on port 8080. You will see `Application startup complete` in the logs when it is ready (takes about 5–10 seconds).

### 4. Link your Polar account

Open http://localhost:8080 in your browser, click **Connect Polar**, and complete the OAuth flow on Polar's site. You are redirected back to a "Connected" confirmation page.

### 5. Wait for files or trigger a sync manually

The scheduler runs every 60 minutes by default. To sync immediately:

```bash
docker compose exec polar-fit-sync python -m polar_fit_sync sync
```

### 6. Find your files

FIT files are written to the `pfs-data` Docker volume at `/data/fit` inside the container. To inspect them from the host:

```bash
docker run --rm -v pfs-data:/data alpine ls /data/fit
```

---

## Configuration Reference

All configuration is done through environment variables. Copy `.env.example` to `.env` for local use; use a Kubernetes Secret in production (see below).

| Variable | Required | Default | Description |
|---|---|---|---|
| `POLAR_CLIENT_ID` | **Yes** | — | OAuth client ID from the Polar developer console |
| `POLAR_CLIENT_SECRET` | **Yes** | — | OAuth client secret from the Polar developer console |
| `POLAR_REDIRECT_URI` | **Yes** | — | Must exactly match the redirect URI registered in your Polar app |
| `PFS_SYNC_MODE` | No | `poll` | Sync trigger mode: `poll`, `webhook`, or `both` |
| `PFS_SYNC_INTERVAL_MINUTES` | No | `60` | How often to poll for new exercises (used in `poll` and `both` modes) |
| `PFS_WEBHOOK_SECRET` | Required if using `webhook` or `both` mode | — | HMAC-SHA256 secret for verifying Polar webhook signatures |
| `PFS_BASE_URL` | No | — | Public base URL of this service (e.g. `https://your-domain.example.com`); used to display the webhook registration URL on the status page |
| `PFS_OUTPUT_DIR` | No | `/data/fit` | Directory where `.fit` files are written |
| `PFS_DB_PATH` | No | `/data/state.db` | Path to the SQLite state database |
| `PFS_MEMBER_ID` | No | `polar-fit-sync` | Stable identifier used when registering your Polar account |
| `PFS_LOG_LEVEL` | No | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PFS_SPORT_FILTER` | No | `""` | Comma-separated sport names to filter (e.g. `RUNNING,CYCLING`). Empty = no filter. |
| `PFS_SPORT_FILTER_MODE` | No | `include` | `include` = allow-list (only listed sports); `exclude` = block-list (skip listed sports). |

---

## Sync Modes

### `poll` (default)

The scheduler checks Polar for new exercises every `PFS_SYNC_INTERVAL_MINUTES` minutes. No public URL or webhook registration required. Best for home servers or environments without inbound internet access.

### `webhook`

Polar pushes a notification to your service the moment a new exercise is uploaded. No interval polling — syncs run only on receipt of a valid webhook delivery. Requires a publicly reachable URL and a shared secret.

To use webhook mode:
1. Set `PFS_SYNC_MODE=webhook` and `PFS_WEBHOOK_SECRET=<a-secret-you-choose>` in your environment
2. Set `PFS_BASE_URL=https://your-domain.example.com`
3. Register the following URL with your Polar app in the developer console:
   ```
   https://your-domain.example.com/webhook/polar
   ```

The webhook URL is also displayed on the status page at http://localhost:8080 when webhook mode is active.

For local testing with webhooks, use a tunnel tool such as [ngrok](https://ngrok.com):

```bash
ngrok http 8080
# Use the generated https://<ngrok-host> as PFS_BASE_URL
```

### `both`

Interval polling and webhook-triggered syncs run together. Files arrive quickly via webhook; the poll interval acts as a catch-up safety net.

---

## Kubernetes Deployment

### 1. Register the redirect URI

In the [Polar developer console](https://www.polar.com/en/developers), add your production redirect URI:

```
https://your-domain.example.com/oauth/callback
```

### 2. Create the Kubernetes secret

```bash
kubectl create secret generic polar-fit-sync-secrets \
  --from-literal=POLAR_CLIENT_ID=your_client_id \
  --from-literal=POLAR_CLIENT_SECRET=your_client_secret \
  --from-literal=POLAR_REDIRECT_URI=https://your-domain.example.com/oauth/callback
```

If you are using webhook mode, also include the webhook secret:

```bash
kubectl create secret generic polar-fit-sync-secrets \
  --from-literal=POLAR_CLIENT_ID=your_client_id \
  --from-literal=POLAR_CLIENT_SECRET=your_client_secret \
  --from-literal=POLAR_REDIRECT_URI=https://your-domain.example.com/oauth/callback \
  --from-literal=PFS_WEBHOOK_SECRET=your_webhook_secret
```

### 3. Apply the manifests

```bash
kubectl apply -f k8s/pvc.yaml -f k8s/deployment.yaml -f k8s/service.yaml
```

### 4. Link your Polar account (first-time setup)

The web UI is not exposed publicly by default. Use a port-forward for the one-time OAuth link:

```bash
kubectl port-forward svc/polar-fit-sync 8080:8080
```

Open http://localhost:8080 and click **Connect Polar** to complete the OAuth flow. You only need to do this once — the token is persisted in the database on the PVC.

### 5. Webhook mode (optional)

To enable webhook mode in Kubernetes:

1. Edit `k8s/ingress.yaml` with your domain and uncomment it, then apply it:
   ```bash
   kubectl apply -f k8s/ingress.yaml
   ```
2. Uncomment `PFS_BASE_URL` in `k8s/deployment.yaml` and set it to your public domain
3. Uncomment `PFS_WEBHOOK_SECRET` in `k8s/deployment.yaml`
4. Register `https://your-domain.example.com/webhook/polar` with your Polar app

### Manual sync (Kubernetes)

```bash
kubectl exec -it deploy/polar-fit-sync -- python -m polar_fit_sync sync
```

---

## Token Expiry

Polar tokens have a finite lifetime and cannot be refreshed automatically. When a token expires, the sync engine stops making API calls and the status page at http://localhost:8080 displays a **"Token may be expired — re-link recommended"** warning. Click **Re-link** to restart the OAuth flow and issue a new token. No exercises are lost — the sync resumes from where it left off, downloading only files not already on disk.
