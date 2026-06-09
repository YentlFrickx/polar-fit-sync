# Polar FIT Sync — Implementation Plan

Companion to `SPEC.md`. Build strictly in the phase order below; each phase has
its tests written alongside (TDD where practical). Stack: Python 3.12, FastAPI,
httpx, APScheduler, pydantic-settings, Jinja2, pytest + respx (httpx mock).

## Patterns & conventions

Greenfield repo (only `AGENTS.md` exists, empty). No prior code to extend.
Establish these conventions:
- `src/` layout, package `polar_fit_sync`, importable as `python -m polar_fit_sync`.
- All Polar HTTP isolated in `polar.py`; all SQL isolated in `db.py`.
- Config only via `config.py` (pydantic-settings); fail fast on missing secrets.
- UTC ISO8601 timestamps everywhere.
- OAuth implemented manually with `httpx` (Basic auth header + form body) — **no
  `authlib` or any OAuth client library**.
- `OAUTH_STATE_TTL_SECONDS = 600` lives in `db.py` and is the single source of the
  state TTL.

## Architecture decision (committed)

Single image, **single long-running Deployment** that hosts the FastAPI web UI and
runs the sync engine in-process (APScheduler `AsyncIOScheduler` for poll/both, and
`POST /webhook/polar` for webhook/both). Shared SQLite on a PVC, mounted by the one
pod — the only SQLite writer. See SPEC §5. No external services. **No K8s CronJob.**
Tokens are not refreshable: detect computed expiry and 401, flag `token_expired`,
prompt re-link.

## Component dependency order

```
config.py → db.py → polar.py → webhook.py → sync.py → scheduler.py → web.py → __main__.py
(packaging/docker/k8s after the code compiles & tests pass)
```

## Files to create (ordered)

### Phase 0 — Project scaffolding
1. `pyproject.toml` — project metadata, deps:
   `fastapi`, `uvicorn[standard]`, `httpx`, `apscheduler`, `pydantic-settings`,
   `jinja2`, `python-multipart`; dev deps: `pytest`, `respx`, `pytest-asyncio`,
   `httpx`. **Do NOT include `authlib`.** `apscheduler` is mandatory.
2. `.gitignore` — `__pycache__/`, `.venv/`, `*.db`, `data/`, `.pytest_cache/`, `.env`.
3. `README.md` — quickstart: configure Polar app, set redirect URI, choose sync
   mode (poll/webhook/both), build, run web for setup, deploy the single
   Deployment; webhook registration note.
4. `.env.example` — placeholder values for all required env vars used by docker-compose
   (`POLAR_CLIENT_ID=`, `POLAR_CLIENT_SECRET=`, `POLAR_REDIRECT_URI=http://localhost:8080/oauth/callback`);
   copy to `.env` and fill in before running `docker compose up`.
5. `src/polar_fit_sync/__init__.py` — version constant.

### Phase 1 — Config + DB (foundation, no network)
5. `src/polar_fit_sync/config.py` — `Settings(BaseSettings)` with all vars from
   SPEC §8 (`PFS_SYNC_MODE`, `PFS_SYNC_INTERVAL_MINUTES`, `PFS_WEBHOOK_SECRET`,
   `PFS_BASE_URL` included); `require_oauth()` raising a clear error if web secrets
   absent; mode validation that fails fast when mode includes `webhook` but
   `PFS_WEBHOOK_SECRET` is unset.
6. `src/polar_fit_sync/db.py` — `Db` class + `OAUTH_STATE_TTL_SECONDS = 600`:
   - `init_schema()` (DDL from SPEC §7, including `expires_in` + `status` on token,
     and `trigger` on `sync_run`)
   - `get_token() -> Token | None`, `set_token(...)` (stores `expires_in`,
     `created_at`, `status='active'`)
   - `set_token_status(status)` — flip to `token_expired`
   - `create_state()`, `consume_state(state) -> bool` (validate + delete + reject
     if `created_at` older than `OAUTH_STATE_TTL_SECONDS`)
   - `is_downloaded(id) -> bool`, `record_downloaded(id, path, sport, start)`
   - `start_run(trigger)`/`finish_run(id, new_files, errors, status)`
   - `INSERT OR IGNORE` / `INSERT OR REPLACE` for idempotency.
7. `tests/test_db.py` — temp-file DB: schema init; single-row token upsert with
   `expires_in`/`status`; `set_token_status('token_expired')`; dedup via
   `record_downloaded` + `is_downloaded`; state create/consume; **state TTL: a row
   with `created_at` 601s in the past is rejected, 599s is accepted**.

### Phase 2 — Polar client (network, mocked in tests)
8. `src/polar_fit_sync/polar.py` — `PolarClient`:
   - constants: `AUTH_URL`, `TOKEN_URL`, `API_BASE = https://www.polaraccesslink.com`
   - `authorize_url(state) -> str`
   - `exchange_code(code) -> TokenResponse` (Basic auth header from
     `base64(client_id:client_secret)`, form body) — manual, no authlib; response
     carries `expires_in`
   - `register_user(access_token, member_id)` — POST `/v3/users`, 409 → ok
   - `list_exercises(access_token) -> list[Exercise]`
   - `get_exercise(access_token, id) -> Exercise` (for targeted webhook sync)
   - `download_fit(access_token, exercise_id) -> bytes` (GET `/v3/exercises/{id}/fit`)
   - `_request_with_backoff(...)` honouring 429 `RateLimit-Reset`, bounded retries;
     on **401 raises a typed `TokenExpiredError`**.
9. `tests/test_polar.py` — `respx`-mocked: authorize URL params; token exchange
   success (asserts Basic auth header + `expires_in` parsed); register 200 and 409
   both succeed; list parsing; get_exercise; fit download bytes; 429-then-200 retry;
   **401 raises `TokenExpiredError`**.

### Phase 3 — Webhook verification + Sync orchestration
10. `src/polar_fit_sync/webhook.py` — pure helpers:
    - `verify_signature(secret, raw_body, header) -> bool` — HMAC-SHA256 over the
      raw body, constant-time compare against `Polar-Webhook-Signature`.
    - `parse_event(body) -> WebhookEvent` — extract `event`, `entity_id`,
      `user_id`; detect registration ping.
11. `src/polar_fit_sync/sync.py` — `run_sync(db, client, output_dir, target_id=None,
    trigger='poll') -> RunResult`:
    - `start_run(trigger)`
    - token check: none → `no_token`, return (no raise); computed expiry
      (`now > created_at + expires_in`) → log warning, `token_expired`, return,
      make no API calls (FR 12)
    - if `target_id`: fetch that exercise (fall back to full list if needed);
      else `list_exercises`
    - filter `not db.is_downloaded(id)`
    - for each new: `download_fit` → write to `{final}.tmp` → `os.rename` to
      `{final}` → `record_downloaded` only after rename (FR 7)
    - per-item try/except: count errors, continue; status `partial` if any failed
    - on `TokenExpiredError` from any call: `db.set_token_status('token_expired')`,
      finish run `token_expired`, return gracefully (FR 13)
    - `_safe_name` filename sanitization helper.
12. `tests/test_webhook.py` — valid HMAC accepted; wrong/missing signature rejected;
    ping detected.
13. `tests/test_sync.py` — fake client + temp dir + temp DB: first run downloads
    all; second run downloads 0 (idempotency); one download raises → others
    succeed, failed id NOT recorded, status `partial`; no-token → `no_token` no
    raise; **expired token (computed) → `token_expired`, zero API calls**; **401
    mid-run → token status `token_expired`**; **atomic write: a failure after the
    `.tmp` write leaves no final file and records no id**; targeted `target_id`
    sync downloads only that exercise.

### Phase 4 — Web UI + webhook endpoint
14. `src/polar_fit_sync/templates/index.html` — status: connected user, last run,
    file count, **current sync mode**, **webhook URL `{base_url}/webhook/polar` when
    mode includes webhook**, **"Token may be expired — re-link recommended" warning
    + re-link button when token expired**, "Connect Polar" / "Re-link" → `/oauth/start`.
15. `src/polar_fit_sync/templates/connected.html` — success confirmation.
16. `src/polar_fit_sync/web.py` — `create_app(settings) -> FastAPI` with a lifespan
    that builds the scheduler (Phase 5 helper) and starts it for poll/both, stops it
    on shutdown:
    - `GET /healthz` → `{"status":"ok"}`
    - `GET /` → render index from DB state (compute token-expired flag; pass mode +
      webhook URL)
    - `GET /oauth/start` → create state, 302 to `client.authorize_url(state)`
    - `GET /oauth/callback` → validate state (400 if bad/expired), `exchange_code`,
      `register_user`, `set_token` (with `expires_in`), render connected page
    - `POST /webhook/polar` → if mode excludes webhook, 404; read raw body;
      if registration ping → 200; else `verify_signature` (401 on failure) →
      `parse_event` → trigger `run_sync(..., target_id=entity_id, trigger='webhook')`
      → 200.
17. `tests/test_web.py` — `TestClient`: `/healthz` 200; `/oauth/start` 302 + state
    stored; callback bad/expired-state → 400; callback good-state (client mocked) →
    token stored + 200; **index shows expiry warning when token expired**; **index
    shows webhook URL when mode includes webhook**; **`POST /webhook/polar` 200 +
    triggers sync on valid signature, 401 on invalid**.

### Phase 5 — Scheduler + Entrypoint + packaging
18. `src/polar_fit_sync/scheduler.py` — `build_scheduler(settings, runner) ->
    AsyncIOScheduler`:
    - for `poll`/`both`: add an interval job firing `runner` every
      `PFS_SYNC_INTERVAL_MINUTES`
    - for `webhook`: add no interval job (scheduler may still be created but empty)
    - `runner` wraps `run_sync(db, client, output_dir, trigger='poll')`.
19. `tests/test_scheduler.py` — `build_scheduler` yields one job for `poll`/`both`
    and zero jobs for `webhook`; interval equals `PFS_SYNC_INTERVAL_MINUTES`.
20. `src/polar_fit_sync/__main__.py` — argparse/sys.argv dispatch:
    - `web` → `uvicorn` serve `create_app` on `0.0.0.0:8080` (the lifespan starts
      the in-process scheduler per mode)
    - `sync` → build Db + PolarClient, call `run_sync(trigger='manual')`,
      `sys.exit(0 if status in {ok,no_token,token_expired} else 1)` (partial/error
      surface non-zero for the manual CLI path)
    - init schema + ensure output dir on both paths.
21. `Dockerfile` — `python:3.12-slim`; install from `pyproject.toml`; copy `src/`;
    non-root `app` user; `WORKDIR /app`; `EXPOSE 8080`;
    `ENTRYPOINT ["python","-m","polar_fit_sync"]`; `CMD ["web"]`.
23. `.dockerignore` — `tests/`, `.venv/`, `data/`, `*.db`, `k8s/`, `.env`.
23. `docker-compose.yml` — local test compose file:
    - `polar-fit-sync` service built from `Dockerfile` (or pulled from local tag)
    - port `8080:8080` mapped for browser access
    - named volume `pfs-data` mounted at `/data` (persists SQLite + FIT files across restarts)
    - env vars for all required config with sensible local defaults:
      `POLAR_CLIENT_ID`, `POLAR_CLIENT_SECRET`, `POLAR_REDIRECT_URI` read from
      a `.env` file (`.env.example` committed with placeholder values, `.env` in
      `.gitignore`); `PFS_SYNC_MODE=poll`, `PFS_SYNC_INTERVAL_MINUTES=60`,
      `PFS_DB_PATH=/data/state.db`, `PFS_OUTPUT_DIR=/data/fit`, `PFS_LOG_LEVEL=DEBUG`
    - `healthcheck` → `GET http://localhost:8080/healthz`
    - For webhook testing: instructions in a comment to set `PFS_SYNC_MODE=both`
      and `PFS_WEBHOOK_SECRET`, then use `ngrok http 8080` to expose the endpoint
      and register `https://<ngrok-host>/webhook/polar` with Polar.

### Phase 6 — Kubernetes manifests (single Deployment, NO CronJob)
25. `k8s/secret.example.yaml` — keys only: `POLAR_CLIENT_ID`, `POLAR_CLIENT_SECRET`,
    `POLAR_REDIRECT_URI`, and `PFS_WEBHOOK_SECRET` (for webhook mode).
26. `k8s/pvc.yaml` — `polar-fit-sync-data`; RWO is sufficient (single pod).
27. `k8s/deployment.yaml` — 1 replica; `strategy: Recreate`; mounts PVC `/data`; env
    from Secret + defaults (`PFS_SYNC_MODE`, `PFS_SYNC_INTERVAL_MINUTES`,
    `PFS_BASE_URL`); `/healthz` probes; CMD `["web"]`.
28. `k8s/service.yaml` — ClusterIP :8080.
29. `k8s/ingress.yaml` — optional external exposure; required for webhook mode so
    Polar can reach `/webhook/polar`; commented out by default.

> Removed: `k8s/cronjob.yaml` — sync now runs in-process inside the Deployment.

## Data flow

```
[web /]            DB.get_token (compute expired?) → index.html (mode, webhook URL, warning)
[web /oauth/start] DB.create_state → 302 Polar authorize
Polar → /oauth/callback?code,state → DB.consume_state(TTL 600s) →
        PolarClient.exchange_code → register_user → DB.set_token(expires_in) → connected.html

[poll/both] scheduler tick → run_sync(trigger=poll)
[webhook]   POST /webhook/polar → verify_signature → parse_event →
            run_sync(target_id=entity_id, trigger=webhook)

run_sync: token check (none→no_token; computed-expiry→token_expired) →
          list/target exercises → filter via DB.is_downloaded →
          download_fit → write {f}.tmp → os.rename({f}) → DB.record_downloaded →
          (401 anywhere → DB.set_token_status(token_expired)) → DB.finish_run
```

## Build sequence checklist

- [ ] Phase 0 scaffolding; `pip install -e .[dev]` works; deps include `apscheduler`, exclude `authlib`.
- [ ] Phase 1 db + tests green (incl. state TTL = 600s, token expires_in/status).
- [ ] Phase 2 polar client + respx tests green (incl. 401 → TokenExpiredError).
- [ ] Phase 3 webhook + sync + tests green (idempotency, atomic write, expiry, 401, targeted).
- [ ] Phase 4 web + webhook endpoint + TestClient tests green.
- [ ] Phase 5 scheduler + `__main__` + Dockerfile + docker-compose.yml; `docker build` ok.
- [ ] Smoke (docker-compose): `docker compose up` → `/healthz` 200, scheduler logs a tick within `PFS_SYNC_INTERVAL_MINUTES`.
- [ ] Smoke: `docker run ... sync` (no token) returns cleanly, records `no_token`.
- [ ] Phase 6 manifests authored (no cronjob.yaml); `kubectl apply --dry-run=client` passes.

## Deployment integration

1. Create Polar app at admin.polaraccesslink.com; set redirect URI to the
   cluster-reachable `POLAR_REDIRECT_URI`. For webhook mode, also register the
   webhook URL `{PFS_BASE_URL}/webhook/polar` with a secret matching
   `PFS_WEBHOOK_SECRET`.
2. `kubectl create secret generic polar-fit-sync-secrets --from-literal=...`
   (include `PFS_WEBHOOK_SECRET` if using webhooks).
3. `kubectl apply -f k8s/pvc.yaml -f k8s/deployment.yaml -f k8s/service.yaml`
   (and `k8s/ingress.yaml` if webhook/external).
4. `kubectl port-forward svc/polar-fit-sync 8080:8080`; open http://localhost:8080;
   click Connect; complete Polar auth (redirect URI must resolve to this).
5. Poll mode: confirm the in-process scheduler triggers a sync within the interval
   (or run a manual `python -m polar_fit_sync sync`); check logs + PVC contents;
   confirm a repeat run adds none. Webhook mode: upload an exercise and confirm
   `/webhook/polar` returns 200 and the file is fetched.

## Test approach

Run `pytest` (all green = logic + idempotency + atomic writes + token-expiry
detection + webhook signature verification + scheduler wiring verified). Then the
deployment smoke tests above. `respx` mocks all Polar HTTP; the one live test is
opt-in via `PFS_LIVE_TEST=1` and excluded from CI. Success = pytest green,
`/healthz` 200 in container with the scheduler initialised, no-token sync returns
cleanly, and (in-cluster) FIT files land on the PVC with a repeat run adding none.
