# Compound Learning

<!-- Persistent memory across AI sessions. Agents read this at pipeline start
     and update it when they discover new patterns, gotchas, or decisions.
     Review new entries with the same scepticism you'd apply to generated content.
     Only record observed reality — not aspirations. -->

## STYLE

- Atomic file writes: always write to `{path}.fit.tmp` then `os.rename()` to final path; DB record inserted only after rename. Pattern used in `sync.py:_atomic_write`.
- APScheduler async jobs: register `async def` callables with `AsyncIOScheduler` directly — it awaits them and surfaces exceptions. Never wrap in a sync function calling `ensure_future` (swallows exceptions into orphan tasks).
- OAuth manual implementation: use raw `httpx` with `Authorization: Basic base64(client_id:client_secret)` header and form-body POST. No `authlib` needed for a single authorization-code exchange.

## GOTCHAS

- `Dockerfile` editable install: `pip install -e .` with hatchling requires `src/` to be present first. Either use `pip install .` (non-editable) or `COPY src/ src/` before the pip install step. The original Dockerfile had this wrong and was fixed by the deploy agent.
- SQLite + NFS: never put a SQLite DB on an NFS-backed RWX PVC with multiple writers. The architecture was redesigned to use a single Deployment (one pod, one writer) with an RWO PVC to avoid this.
- `AsyncIOScheduler` + sync wrapper: a synchronous job calling `asyncio.get_event_loop()` is deprecated in Python 3.12+ and loses exceptions. Always register async job functions.
- Polar `expires_in`: the token exchange response includes `expires_in` even though long-lived. Store it and detect expiry by computation + 401 responses. Never assume tokens are permanent.
- `with_suffix(".tmp")` replaces the extension; `with_name(name + ".tmp")` appends. Use the latter for temp files so the original extension is preserved.

## ARCH_DECISIONS

- Single Deployment with in-process APScheduler instead of a separate K8s CronJob. Reason: two pods sharing a SQLite PVC risks concurrent-write corruption (especially on NFS). One pod, one writer, no coordination needed.
- Sync modes (`poll`, `webhook`, `both`) controlled by `PFS_SYNC_MODE` env var. Webhook mode requires `PFS_WEBHOOK_SECRET` and a public ingress; poll mode works with no public exposure.
- Token expiry handling: detect computed expiry (`now > created_at + expires_in`) at run start and 401 responses mid-run; flip `token.status` to `token_expired`; show re-link prompt on web UI. No token refresh (Polar does not support it).
- `POLAR_CLIENT_ID` / `POLAR_CLIENT_SECRET` required only for the `web` command. The `sync` command reads the stored access token from SQLite and does not need client credentials.

## DESIGN_DECISIONS

- `OAUTH_STATE_TTL_SECONDS = 600` (10 minutes) — defined in `db.py`, the single source. States older than 600s are rejected by `consume_state`.
- Dedup key for downloaded exercises = `exercise_id` (Polar's stable hashed id). `INSERT OR IGNORE` on `downloaded_exercise` table.
- Token table is a single-row table (`id=1` CHECK constraint). Only one Polar account supported.
- `sync_run.trigger` values: `poll` | `webhook` | `manual`. `sync_run.status` values: `ok` | `partial` | `no_token` | `token_expired` | `error`.
- Sync exit code (manual CLI path): 0 for `ok`/`no_token`/`token_expired`; 1 for `partial`/`error`. This prevents the Kubernetes container from hard-failing on auth issues that require human re-link.
- Polar webhook signature: HMAC-SHA256 over the raw request body using `PFS_WEBHOOK_SECRET`; verified via `hmac.compare_digest` (constant-time). Header name: `Polar-Webhook-Signature`.
- Sport filtering: `PFS_SPORT_FILTER` (comma list) + `PFS_SPORT_FILTER_MODE` (`include`|`exclude`). Empty list = no filter. Matching is case-insensitive on uppercased sport name. `include` + null sport → dropped; `exclude` + null sport → kept. Applied in `run_sync` via `_passes_filter`; parsed once at call time by `Settings.sport_filter_set()`. Filter is forward-only — never deletes or backfills already-downloaded files.
