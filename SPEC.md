# Polar FIT Sync — Specification

## 1. Summary

A small self-hosted tool that downloads `.fit` exercise files from Polar Flow
(via the Polar AccessLink API v3) to a local filesystem volume, incrementally.
A minimal FastAPI web UI handles the one-time OAuth linking of a Polar account
and exposes a status page. The same long-running process also runs the sync
engine in-process, either on a fixed poll interval, on Polar webhook delivery,
or both. Runs in Kubernetes as a single Deployment built from one container
image, backed by a SQLite state database and a FIT output directory on one PVC.

There is exactly one pod, one process, and one SQLite writer — eliminating any
concurrent-write concern.

## 2. Key Polar API facts (verified)

These shape the entire design; they are simpler than the task brief assumed.

- **OAuth authorize**: `GET https://flow.polar.com/oauth2/authorization`
  with `response_type=code`, `client_id`, `redirect_uri`, optional `state`.
- **Token exchange**: `POST https://polarremote.com/v2/oauth2/token`
  with `Authorization: Basic base64(client_id:client_secret)`,
  body `grant_type=authorization_code&code=...&redirect_uri=...`.
  Response: `access_token`, `token_type`, `expires_in`, `x_user_id`.
- **Tokens have a long but finite lifetime** (`expires_in` is returned) and
  **Polar does not support refresh tokens**. We therefore do NOT implement
  token refresh; we detect expiry/revocation (computed expiry or HTTP 401) and
  prompt the owner to re-link. See FR 12–14.
- **User registration is mandatory once** after token exchange:
  `POST https://www.polaraccesslink.com/v3/users` with Bearer token and body
  `{"member-id": "<any-stable-string>"}`. 409 = already registered (treat as OK).
- **Exercises are NOT transaction-based** in current v3. Simple list + fetch:
  - List: `GET https://www.polaraccesslink.com/v3/exercises`
  - Get one: `GET .../v3/exercises/{id}`
  - **FIT file**: `GET .../v3/exercises/{id}/fit` (binary FIT body)
- **Base host**: `https://www.polaraccesslink.com`
- **Webhooks (optional mode)**: Polar delivers a `POST` to a registered URL on an
  `EXERCISE` event. Payload includes `event`, `user_id`, `entity_id` (the exercise
  hash id), `timestamp`, and a `url`. Every payload is signed with HMAC-SHA256;
  the signature is in the `Polar-Webhook-Signature` header and is verified against
  the shared webhook secret. Only one active webhook per client; Polar deactivates
  a webhook after 7 days of delivery failures; the registration ping must return
  HTTP 200.
- **Rate limits**: 15-min = 500 + users×20; 24-h = 5000 + users×100; 429 on breach.
  For one user this is effectively irrelevant; we still handle 429 with backoff.
- Exercise list item fields include `id`, `upload_time`, `start_time`, `sport`,
  `duration`, `distance`. We key dedup on `id` (hashed, stable).

> Note: older docs describe a create/list/commit transaction flow. The modern
> v3 endpoints above are non-transactional and are what we use. We do NOT
> implement the deprecated transaction flow.

## 3. User stories & acceptance scenarios

### Story 1 — Link a Polar account (one-time setup)

As the tool owner, I want to authorize the app against my Polar account through
a web page so that the sync engine can download my exercise files.

- **Scenario: Start authorization**
  - Given the web UI is reachable and no account is linked
  - When I open `/` and click "Connect Polar"
  - Then I am redirected to Polar's authorization page with the configured
    `client_id` and `redirect_uri`.

- **Scenario: Complete authorization**
  - Given I approved access on Polar
  - When Polar redirects back to `/oauth/callback?code=...&state=...`
  - Then the app exchanges the code for an access token, registers the user
    (`POST /v3/users`, tolerating 409), persists the token + `x_user_id` +
    `expires_in` + `created_at`, and shows a "Connected" status page.

- **Scenario: CSRF protection on callback**
  - Given an authorization was started with a generated `state`
  - When the callback arrives with a missing or mismatched `state`
  - Then the request is rejected with HTTP 400 and no token is stored.

- **Scenario: State TTL expiry**
  - Given an authorization `state` was created more than 10 minutes ago
    (`OAUTH_STATE_TTL_SECONDS = 600`)
  - When the callback arrives with that now-expired `state`
  - Then the request is rejected with HTTP 400 and no token is stored.

- **Scenario: Already linked**
  - Given an account is already linked and the token is not expired
  - When I open `/`
  - Then I see the connected Polar `x_user_id`, last sync time, file count,
    the current sync mode, and a "Re-link" option.

### Story 2 — Incremental scheduled sync (poll mode)

As the tool owner, I want the running service to periodically download only new
`.fit` files so that my local archive stays current without duplicates.

- **Scenario: Interval polling**
  - Given mode is `poll` or `both` and `PFS_SYNC_INTERVAL_MINUTES=60`
  - When the service has been running for an hour
  - Then a sync run is triggered in-process by the scheduler.

- **Scenario: First sync**
  - Given an account is linked and no files have been downloaded
  - When a sync run executes
  - Then every exercise returned by `/v3/exercises` is downloaded to
    `{output_dir}/{start_date}_{sport}_{id}.fit`, and each `id` is recorded.

- **Scenario: Incremental sync (idempotency)**
  - Given some exercise `id`s are already recorded as downloaded
  - When a sync run executes again
  - Then only exercises whose `id` is not yet recorded are downloaded, and a
    second consecutive run downloads zero files.

- **Scenario: No linked account**s 
  - Given no account is linked
  - When a sync run executes
  - Then it logs a clear "no token; run web setup first" message, records the run
    as `no_token`, and returns without raising (the service keeps running).

- **Scenario: Partial failure on one file**
  - Given 5 new exercises and one returns a 5xx on FIT download
  - When a sync run executes
  - Then the other 4 are downloaded and recorded, the failing one is left
    unrecorded (retried next run), and the run is recorded as `partial`.

- **Scenario: Rate limited**
  - Given the API returns HTTP 429
  - When the sync run is downloading
  - Then it honours `RateLimit-Reset` / backs off and retries up to a bounded
    number of attempts before failing that item.

### Story 3 — Webhook-triggered sync (webhook mode)

As the tool owner, I want Polar to notify my service the moment a new exercise
exists so that files arrive promptly without waiting for the poll interval.

- **Scenario: Discover the webhook URL to register**
  - Given mode is `webhook` or `both`
  - When I open `/`
  - Then the status page shows the webhook URL to register with Polar
    (`{base_url}/webhook/polar`).

- **Scenario: Valid webhook triggers a targeted sync**
  - Given mode is `webhook` or `both` and `PFS_WEBHOOK_SECRET` is configured
  - When Polar sends `POST /webhook/polar` with an `EXERCISE` event and a valid
    `Polar-Webhook-Signature`
  - Then the app responds 200 immediately and triggers a sync for the referenced
    `entity_id` (falling back to a full sync if a targeted fetch is not possible).

- **Scenario: Webhook signature rejected**
  - Given `PFS_WEBHOOK_SECRET` is configured
  - When `POST /webhook/polar` arrives with a missing or invalid
    `Polar-Webhook-Signature`
  - Then the request is rejected with HTTP 401 and no sync is triggered.

- **Scenario: Webhook ping validation**
  - Given Polar sends its registration ping to `/webhook/polar`
  - When the ping payload is received
  - Then the endpoint responds HTTP 200 so Polar accepts the registration.

### Story 4 — Token expiry detection & re-link prompt
As the tool owner, I want to be warned when my token is no longer valid so that
syncing does not silently stop.

- **Scenario: Computed expiry at run start**
  - Given a token whose `created_at + expires_in` is in the past
  - When a sync run starts
  - Then it logs a warning, records the run as `token_expired`, and does not call
    the API.

- **Scenario: 401 during a call**
  - Given a token that the API rejects with HTTP 401
  - When a sync run makes any Polar API call
  - Then the token status is set to `token_expired` in the DB and the run exits
    gracefully (recorded as `token_expired`).

- **Scenario: UI warning**
  - Given the stored token is expired (computed or flagged by a prior 401)
  - When I open `/`
  - Then the page shows "Token may be expired — re-link recommended" with a
    re-link button. (No automatic refresh is attempted.)

## 4. Functional requirements

1. The app SHALL build a Polar authorize URL from env-configured `client_id`,
   `redirect_uri`, and a per-request random `state`.
2. The callback SHALL validate `state` (presence, match, and age ≤
   `OAUTH_STATE_TTL_SECONDS = 600`), exchange `code` for a token, and persist
   `access_token`, `x_user_id`, `expires_in`, and `created_at` to the state DB.
3. The app SHALL register the user via `POST /v3/users` after token exchange and
   treat HTTP 409 (already registered) as success.
4. The sync SHALL list exercises and download FIT files only for `id`s not
   already recorded as downloaded.
5. Downloaded files SHALL be written to a configurable output directory with a
   deterministic, human-readable, collision-free filename containing the `id`.
6. The sync SHALL be idempotent: a second run with no new exercises writes no
   files and is recorded as `ok` with `new_files = 0`.
7. Each FIT file SHALL be written atomically: bytes are first written to
   `{final_path}.tmp`, then atomically renamed to `{final_path}` via
   `os.rename()`, and the DB record SHALL be inserted only after the rename
   succeeds. A crash mid-write therefore never leaves a truncated file in place
   of a completed one, and never records an `id` for an incomplete file.
8. The sync SHALL handle HTTP 429 with bounded backoff and continue/fail-soft
   per item, recording the run as `partial` if any new item ultimately failed.
9. Credentials (`client_id`, `client_secret`) SHALL come only from environment /
   K8s Secret, never from source. They are required only for the web command.
10. The web UI SHALL expose `/healthz` returning 200 for liveness/readiness.
11. The sync SHALL return without raising (recording `no_token`) when no token is
    present, so the long-running service stays up.
12. At the start of every sync run the app SHALL check whether
    `now > created_at + expires_in`; if so it SHALL log a warning, record the run
    as `token_expired`, and make no API calls.
13. On any HTTP 401 from a Polar API call the app SHALL set the token status to
    `token_expired` in the DB and end the run gracefully (recorded as
    `token_expired`).
14. The web UI index page SHALL show a "Token may be expired — re-link
    recommended" warning with a re-link button when the stored token is expired
    (by computation or by a prior 401 flag). No token refresh is attempted.
15. The service SHALL run the sync engine in-process according to `PFS_SYNC_MODE`:
    - `poll`: an AsyncIOScheduler runs `run_sync` every
      `PFS_SYNC_INTERVAL_MINUTES` minutes; no webhook endpoint behaviour required.
    - `webhook`: no interval scheduler; sync runs only on webhook receipt.
    - `both`: interval polling AND webhook-triggered sync.
16. The app SHALL expose `POST /webhook/polar`. When mode includes `webhook`, a
    request with a valid `Polar-Webhook-Signature` (HMAC-SHA256 over the raw body
    using `PFS_WEBHOOK_SECRET`) SHALL trigger a sync for the payload's `entity_id`
    (falling back to a full sync) and respond 200; an invalid/missing signature
    SHALL be rejected with HTTP 401; a registration ping SHALL respond 200.
17. The web UI index page SHALL display the current sync mode, and when the mode
    includes `webhook` SHALL display the webhook URL to register with Polar
    (`{base_url}/webhook/polar`).

## 5. Architecture decision

**Language/framework: Python 3.12 + FastAPI (web) + httpx (HTTP) + APScheduler
(in-process scheduling) + SQLite.**

Rationale:
- Polar AccessLink is plain REST + OAuth2. The OAuth2 authorization-code flow is
  implemented manually with `httpx` (Basic auth header
  `Authorization: Basic base64(client_id:client_secret)` + form-body POST) — no
  OAuth library is needed for this single, well-understood flow. FastAPI gives a
  tiny, well-typed web layer with built-in health endpoints and Jinja2 templating.
- A **single long-running Deployment** hosts both the web UI and the sync engine.
  Sync is driven in-process by an `AsyncIOScheduler` (APScheduler) for poll mode
  and by the `POST /webhook/polar` endpoint for webhook mode. One image, one
  process, one entrypoint → minimal ops and no inter-pod coordination.
- SQLite on a small PVC is the right size for a personal tool: it stores the
  single OAuth token (with expiry metadata) and the set of downloaded exercise
  ids. Because there is exactly one pod and one process, there is exactly one
  SQLite writer — no concurrent-write/NFS-locking risk. No external DB, no queue.
- Both polling and webhooks are supported so the owner can choose between a
  zero-ingress poll setup and a low-latency push setup.

Rejected alternatives:
- Go (more boilerplate for OAuth/templating; not worth it at this size).
- External Postgres / Redis (overkill for one user, one token, a set of ids).
- A separate K8s CronJob for sync (rejected: created a second pod that shared the
  SQLite PVC, a documented concurrent-write corruption risk; the in-process
  scheduler removes both the extra workload and the risk).
- Any OAuth client library such as `authlib` (rejected: the single
  authorization-code exchange is a few lines of `httpx`; an extra dependency adds
  no value).

## 6. Component design

```
src/polar_fit_sync/
  config.py        # Pydantic Settings: env vars (client id/secret, redirect, dirs,
                   #   db path, sync mode, interval, webhook secret, base url, log)
  db.py            # SQLite access: schema init, token get/set, token-status update,
                   #   downloaded-id checks/inserts, state create/consume(TTL), run log
  polar.py         # PolarClient: authorize_url(), exchange_code(), register_user(),
                   #   list_exercises(), get_exercise(), download_fit(); 401 + 429 aware
  sync.py          # run_sync(): expiry pre-check → list/target → filter new →
                   #   atomic download/rename → record; 401 → token_expired; returns summary
  scheduler.py     # build_scheduler(settings, runner): AsyncIOScheduler wiring for
                   #   poll/both modes; start/stop helpers
  webhook.py       # verify_signature(secret, raw_body, header) HMAC-SHA256; payload parse
  web.py           # FastAPI app + lifespan: GET /, /oauth/start, /oauth/callback,
                   #   /healthz, POST /webhook/polar; starts/stops scheduler in lifespan
  __main__.py      # CLI dispatch: `python -m polar_fit_sync web` | `... sync`
  templates/
    index.html     # status page: connected user, last run, file count, sync mode,
                   #   webhook URL (if applicable), expiry warning, Connect/Re-link
    connected.html # post-callback confirmation
```

Responsibilities:
- **config.py** — single source of all runtime config; fails fast if required
  secrets are missing for the chosen command (web vs sync) or mode.
- **db.py** — only module that touches SQLite; everything else calls it. Holds
  `OAUTH_STATE_TTL_SECONDS = 600`.
- **polar.py** — only module that talks to Polar; fully unit-testable with a
  mocked `httpx` transport. Raises a typed error on 401 so callers can flag expiry.
- **sync.py** — business logic; depends on `polar.py` + `db.py`. Owns the
  expiry pre-check, atomic-write helper, `_safe_name` sanitization, and 401 handling.
- **scheduler.py** — APScheduler `AsyncIOScheduler` setup for poll/both modes.
- **webhook.py** — HMAC-SHA256 signature verification + payload parsing; no I/O.
- **web.py** — thin OAuth/status/webhook layer; delegates to the modules above and
  manages scheduler lifecycle via the FastAPI lifespan.

## 7. Data model (SQLite)

```sql
CREATE TABLE IF NOT EXISTS token (
    id           INTEGER PRIMARY KEY CHECK (id = 1),  -- single row
    access_token TEXT NOT NULL,
    token_type   TEXT NOT NULL DEFAULT 'bearer',
    x_user_id    TEXT NOT NULL,
    member_id    TEXT NOT NULL,
    expires_in   INTEGER,                              -- seconds, from token response
    created_at   TEXT NOT NULL,                        -- ISO8601 UTC (token issue time)
    status       TEXT NOT NULL DEFAULT 'active'        -- active | token_expired
);

CREATE TABLE IF NOT EXISTS downloaded_exercise (
    exercise_id   TEXT PRIMARY KEY,   -- Polar hashed id == dedup key
    file_path     TEXT NOT NULL,
    sport         TEXT,
    start_time    TEXT,
    downloaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_state (
    state      TEXT PRIMARY KEY,
    created_at TEXT NOT NULL          -- TTL = OAUTH_STATE_TTL_SECONDS (600s); rejected if older
);

CREATE TABLE IF NOT EXISTS sync_run (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    new_files     INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    trigger       TEXT NOT NULL DEFAULT 'poll',  -- poll | webhook | manual
    status        TEXT NOT NULL                  -- ok | partial | no_token | token_expired | error
);
```

- **Dedup key** = `downloaded_exercise.exercise_id` (Polar `id`). Idempotency
  comes from `INSERT OR IGNORE` + checking membership before download.
- **Token** is a single-row table (one Polar account). `expires_in` + `created_at`
  drive the computed-expiry check; `status` is flipped to `token_expired` on a 401.
- **oauth_state** rows are rejected by `consume_state` if older than
  `OAUTH_STATE_TTL_SECONDS` (600s) and deleted on consume.
- The DB file and the FIT output dir live on the **same PVC**, mounted by the
  single Deployment pod — the only reader and writer.

## 8. Configuration (environment variables)

| Var | Required | Default | Purpose |
|---|---|---|---|
| `POLAR_CLIENT_ID` | yes (web) | — | OAuth client id (from Secret); web command only |
| `POLAR_CLIENT_SECRET` | yes (web) | — | OAuth client secret (from Secret); web command only |
| `POLAR_REDIRECT_URI` | yes (web) | — | Must match Polar app config |
| `PFS_DB_PATH` | no | `/data/state.db` | SQLite path (on PVC) |
| `PFS_OUTPUT_DIR` | no | `/data/fit` | Where `.fit` files are written (on PVC) |
| `PFS_MEMBER_ID` | no | `polar-fit-sync` | Stable member-id for `/v3/users` |
| `PFS_SYNC_MODE` | no | `poll` | `poll` \| `webhook` \| `both` |
| `PFS_SYNC_INTERVAL_MINUTES` | no | `60` | Poll interval; used in `poll` and `both` modes |
| `PFS_WEBHOOK_SECRET` | required if mode includes `webhook` | — | HMAC-SHA256 secret for verifying Polar webhook signatures |
| `PFS_BASE_URL` | no | — | Public base URL; used to render the webhook URL on the status page |
| `PFS_LOG_LEVEL` | no | `INFO` | Logging level |

Notes:
- The sync command reads the stored token from the DB and does not require
  `POLAR_CLIENT_ID` / `POLAR_CLIENT_SECRET` at startup.
- `PFS_WEBHOOK_SECRET` is mandatory when `PFS_SYNC_MODE` is `webhook` or `both`;
  config validation fails fast if it is missing.

## 9. Deployment strategy

**Single image, one K8s Deployment, one shared PVC, one Secret.**

- `Dockerfile` — `python:3.12-slim`, install deps, copy `src/`, non-root user,
  entrypoint `python -m polar_fit_sync`. Default CMD `web` (which also starts the
  in-process scheduler for poll/both modes).
- `k8s/secret.example.yaml` — `polar-fit-sync-secrets` holding `POLAR_CLIENT_ID`,
  `POLAR_CLIENT_SECRET`, `POLAR_REDIRECT_URI`, and (if using webhooks)
  `PFS_WEBHOOK_SECRET`. Committed as example only.
- `k8s/pvc.yaml` — `polar-fit-sync-data`. Only one pod mounts it, so RWO is
  sufficient (RWX not required); no node-affinity gymnastics.
- `k8s/deployment.yaml` — 1 replica, `strategy: Recreate` (single SQLite writer),
  mounts PVC at `/data`, env from Secret + defaults (`PFS_SYNC_MODE`,
  `PFS_SYNC_INTERVAL_MINUTES`, `PFS_BASE_URL`), `livenessProbe`/`readinessProbe`
  → `/healthz`, CMD `web`.
- `k8s/service.yaml` — ClusterIP on port 8080 (reachable in-cluster + via
  `kubectl port-forward` for first-time setup).
- `k8s/ingress.yaml` — optional external exposure; **required** if using webhook
  mode (Polar must reach `/webhook/polar` publicly). Commented out by default;
  port-forward remains the default for the one-time link in poll mode.

There is **no** `k8s/cronjob.yaml`: sync is driven in-process by the Deployment.

Secrets: never in image or manifests-with-values; the Secret is created out of
band (`kubectl create secret` or sealed-secrets). `secret.example.yaml` documents
the required keys only.

## 10. Test strategy

- **Unit tests (yes):**
  - `polar.py` — authorize URL construction; `exchange_code`/`register_user`/
    `list_exercises`/`get_exercise`/`download_fit` against a mocked `httpx`
    transport, including 409-on-register, 429 backoff, and 401 (raises typed
    `TokenExpiredError`) paths.
  - `db.py` — schema init, single-row token upsert (with `expires_in`/`created_at`/
    `status`), token-status update to `token_expired`, `INSERT OR IGNORE` dedup,
    `is_downloaded`/`record_downloaded`, state create/consume, and **state TTL
    expiry using `OAUTH_STATE_TTL_SECONDS = 600`** (a state older than 600s is
    rejected).
  - `sync.py` — with fake `PolarClient` + temp SQLite + temp dir: first run
    downloads all; second run downloads none (idempotency); partial-failure leaves
    the failed id unrecorded and records `partial`; no-token records `no_token`
    without raising; **computed-expiry pre-check records `token_expired` and makes
    no calls**; **a 401 mid-run flips token status to `token_expired`**; and the
    **atomic-write path** (a download that fails after the `.tmp` write leaves no
    final file and records no id).
  - `webhook.py` — `verify_signature` accepts a correct HMAC-SHA256 and rejects a
    wrong/absent signature.
  - `scheduler.py` — `build_scheduler` schedules a job at the configured interval
    for `poll`/`both` and schedules none for `webhook` (assert job count).
  - `web.py` — via FastAPI `TestClient`: `/healthz` 200; `/oauth/start` redirects
    and stores state; `/oauth/callback` rejects bad/expired state (400) and stores
    token on good state (with `polar.py` mocked); index shows expiry warning when
    token expired; index shows webhook URL when mode includes `webhook`;
    `POST /webhook/polar` returns 200 on valid signature and triggers a sync, 401
    on invalid signature.
- **Integration tests (limited):** A single opt-in test (skipped by default,
  `PFS_LIVE_TEST=1`) hitting the real API with a real token, to sanity-check the
  endpoints have not changed. Not part of CI.
- **Skipped:** real Polar OAuth interactive flow (requires a human + browser);
  real Polar webhook delivery (requires public ingress + a registered webhook);
  Kubernetes manifest application (validated by deployment verification, below).

## 11. Verification strategy

**Option C — automated tests AND deployment verification.**

1. `pytest` green (unit suite above) — primary correctness gate.
2. `docker build` succeeds; `docker run ... web` starts, the in-process scheduler
   initialises for the configured mode, and `GET /healthz` returns 200 (smoke test
   in a throwaway container with a tmp `/data`).
3. `python -m polar_fit_sync sync` with **no token** returns cleanly, logs the
   "run web setup first" warning, and records a `no_token` run (proves the
   fail-soft path without credentials).
4. In-cluster (poll mode): `kubectl port-forward svc/polar-fit-sync 8080:8080`,
   open `/`, complete the real Polar link once, confirm the scheduler triggers a
   sync within the interval (or trigger a manual `sync` invocation), confirm
   `.fit` files appear on the PVC and a subsequent run adds none.
5. In-cluster (webhook mode, optional): with an ingress and a registered Polar
   webhook + matching `PFS_WEBHOOK_SECRET`, perform an exercise upload and confirm
   `POST /webhook/polar` returns 200 and the file is fetched.

Why both: unit tests prove logic + idempotency + expiry + signature verification
cheaply; deployment verification proves the image runs, the scheduler starts, the
health probe works, and the real OAuth + download round-trip succeeds (the parts
that cannot be unit-tested).

## 12. Temporary adjustments

- For the deployment smoke test, run the container with `PFS_DB_PATH` and
  `PFS_OUTPUT_DIR` pointed at a writable tmp dir (no PVC needed locally) and
  `PFS_SYNC_MODE=poll` with a short `PFS_SYNC_INTERVAL_MINUTES` to observe the
  scheduler firing during the smoke window.
- The optional live integration test is gated behind `PFS_LIVE_TEST=1` and a real
  token in the local DB — off by default, never in CI.

## 13. Out of scope

- TCX/GPX downloads (FIT only).
- Multiple Polar accounts / multi-tenant (single-row token by design).
- Pagination of the exercise list (accepted as sufficient for personal use).
- Parsing/analyzing FIT contents (we only archive raw files).
- Re-uploading to other platforms (Strava, etc.).
- Token refresh logic (Polar does not support refresh; we only detect expiry and
  prompt for re-link).
- Automatic Polar webhook registration (the owner registers the webhook URL with
  Polar manually; the app only receives and verifies deliveries).
- External database; auth on the web UI beyond cluster network isolation.
