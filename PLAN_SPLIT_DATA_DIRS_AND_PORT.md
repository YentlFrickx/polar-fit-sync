# PLAN: Split FIT Output Dir from DB Dir, and Configurable Listen Port

## Summary
Two independent, unrelated changes:

1. **Truly independent storage paths.** `PFS_DB_PATH` and `PFS_OUTPUT_DIR` are
   already separate Pydantic fields (`config.py:39-40`) with independent defaults
   (`/data/state.db`, `/data/fit`) — there is no shared-base derivation to remove.
   The one real defect is that **nothing creates the DB path's parent directory**:
   `__main__.py:69` and `run_sync` (`sync.py:205`) both `mkdir` only the output
   dir, and `Db._connect` (`db.py:62`) opens the SQLite file with no `mkdir`. It
   works today only because both defaults sit under `/data`, so creating
   `/data/fit` incidentally creates `/data`. Pointing `PFS_DB_PATH` at a *different*
   directory currently crashes with "unable to open database file". The fix makes
   the DB directory get created independently of the output dir, so the two paths
   are genuinely decoupled. Non-breaking: defaults unchanged.

2. **Configurable listen port.** The port `8080` is hardcoded in
   `__main__.py:89` (uvicorn), `Dockerfile:10` (EXPOSE), and `docker-compose.yml`
   (ports mapping + healthcheck URL). Add `PFS_PORT` (default `8080`) and thread it
   through uvicorn, the Dockerfile, and docker-compose so a single change stays
   consistent.

Both changes preserve current behaviour when the new/existing vars are left at
their defaults.

---

## Patterns & conventions found

- Config is centralised in `config.py` as typed Pydantic `Settings` fields with
  `PFS_*` env names, `SettingsConfigDict(env_file=".env", extra="ignore")`
  (`config.py:28-72`). Non-string coercion is handled by pydantic-settings (see
  `pfs_sync_interval_minutes: int = 60`, `config.py:47`).
- Fail-fast validation lives in the `_validate_settings` `model_validator`
  (`config.py:74-102`).
- Storage paths are already split: `pfs_db_path` / `pfs_output_dir`
  (`config.py:39-40`), consumed at `web.py:50` (`Db(settings.pfs_db_path)`),
  `web.py:92` / `web.py:238` (`run_sync(..., settings.pfs_output_dir, ...)`),
  `__main__.py:69` (output-dir mkdir), `__main__.py:103` (`Db(...)`).
- Output-dir creation idiom `pathlib.Path(x).mkdir(parents=True, exist_ok=True)`
  appears at `__main__.py:69` and `sync.py:205`.
- `Db.__init__(path)` stores `self._path` (`db.py:56-59`); `_connect`
  (`db.py:61-68`) is the single `sqlite3.connect` call site; `init_schema`
  (`db.py:70+`) is called once right after construction on both entry paths.
- uvicorn launch is the single line `__main__.py:89`.
- README config table format: `README.md:75-91` (Markdown table, one row per var).
- Tests: `tests/test_config.py` constructs `Settings(**_base_kwargs(...),
  _env_file=None)` to dodge local-`.env` bleed-through (see the GOTCHA in
  `AGENTS.md:25`). `tests/test_db.py` uses `Db(str(tmp_path / "test.db"))`.
- No `k8s/` directory exists (README references it aspirationally, tracked in
  BACKLOG). Only Dockerfile + docker-compose need deployment edits.

## Architecture decision

**Feature 1:** Create the DB parent directory inside `Db.__init__` (in `db.py`),
not in the callers. Rationale: `db.py` is the module that owns the DB path and is
the single place both entry points (`web.py`, `__main__.py`) route through, so one
edit covers every caller (web, sync CLI, and every test) with no duplication. It
also mirrors the existing self-sufficiency of the output dir (created in two
independent spots). Guard against `":memory:"` (used in tests / valid per the
`db.py:57` comment) and against a path with no directory component (bare filename
→ cwd). Trade-off vs. adding a mkdir in `__main__.py` alongside the existing
output-dir mkdir: the `Db.__init__` approach also protects the `web.py`
`create_app` path (`web.py:50`), which never had an explicit DB mkdir, and keeps
the "directory must exist for the DB" invariant co-located with the code that
opens the DB.

**Feature 2:** Add `pfs_port: int = 8080` to `Settings`, pass
`port=settings.pfs_port` to `uvicorn.run`, and parameterise the Docker/compose
port. Rationale: matches the existing `PFS_*` int-field convention
(`pfs_sync_interval_minutes`). No new abstraction needed.

## Component design / implementation map

### `src/polar_fit_sync/config.py`
- For Feature 2, add in the Storage/observability area:
  ```python
  # --- Web server ---
  # PFS_PORT: TCP port the uvicorn server binds (host 0.0.0.0). Default 8080
  # matches the previously-hardcoded value and the Dockerfile EXPOSE / compose
  # port mapping. Change requires a restart (and a matching compose port map).
  pfs_port: int = 8080
  ```
- Optional hardening (recommended, cheap): in `_validate_settings` add a range
  check so an out-of-range port fails fast:
  ```python
  if not (1 <= self.pfs_port <= 65535):
      raise ValueError(f"PFS_PORT must be between 1 and 65535, got {self.pfs_port}")
  ```

### `src/polar_fit_sync/db.py`
- In `__init__` (`db.py:56-59`), after storing `self._path`, create the parent
  directory unless the path is in-memory:
  ```python
  import pathlib  # add to existing imports at top of file
  ...
  def __init__(self, path: str) -> None:
      self._path = path
      # Ensure the DB's parent directory exists so PFS_DB_PATH can point at a
      # location wholly independent of PFS_OUTPUT_DIR. sqlite3.connect does not
      # create missing directories; without this, a non-default PFS_DB_PATH
      # whose dir isn't otherwise created (e.g. by the output-dir mkdir) fails
      # with "unable to open database file". ":memory:" and bare filenames
      # (no directory component) are skipped.
      if path != ":memory:":
          parent = pathlib.Path(path).parent
          if str(parent) not in ("", "."):
              parent.mkdir(parents=True, exist_ok=True)
  ```
  (Keep the existing `import sqlite3` etc.; add `import pathlib`.)

### `src/polar_fit_sync/__main__.py`
- `__main__.py:89`: change
  `uvicorn.run(app, host="0.0.0.0", port=8080, log_level=settings.pfs_log_level.lower())`
  to `port=settings.pfs_port`.
- (No change needed to the output-dir mkdir at `__main__.py:69`; the DB dir is now
  handled in `Db.__init__`.)

### `Dockerfile`
- `Dockerfile:10`: keep `EXPOSE 8080` as documentation of the default. EXPOSE is
  purely informational and does not restrict runtime binding, so leaving the
  default literal is acceptable; add a comment noting it should track `PFS_PORT`.
  (Do NOT try to make EXPOSE dynamic via build ARG unless the user wants it — it
  has no functional effect on port mapping.)

### `docker-compose.yml`
- `docker-compose.yml:4-5` ports: make the mapping and the app's internal port
  driven by a compose variable defaulting to 8080 so host + container + PFS_PORT
  stay in lockstep:
  ```yaml
  ports:
    - "${PFS_PORT:-8080}:${PFS_PORT:-8080}"
  environment:
    PFS_PORT: "${PFS_PORT:-8080}"
    ...
  ```
- `docker-compose.yml:26-37` healthcheck: the URL hardcodes `8080`. Since the
  healthcheck runs *inside* the container, it must hit the port the app actually
  binds. Change the test to read the env var:
  ```yaml
  test:
    [
      "CMD",
      "python",
      "-c",
      "import os,urllib.request; urllib.request.urlopen(f\"http://localhost:{os.environ.get('PFS_PORT','8080')}/healthz\")",
    ]
  ```
- Add a commented example near the other env vars documenting how to override.

### `README.md`
- Add a row to the config table (`README.md:75-91`):
  `| PFS_PORT | No | 8080 | TCP port the web server binds inside the container. When changed, update the docker-compose port mapping (or set PFS_PORT in your environment so the compose ${PFS_PORT:-8080} interpolation follows) so the host mapping stays consistent. |`
- Clarify the existing `PFS_OUTPUT_DIR` / `PFS_DB_PATH` rows (`README.md:85-86`)
  with a note that the two paths are fully independent and may live on different
  volumes/mounts; the DB directory is created automatically if missing.

## Data flow

- Feature 1: `Settings.pfs_db_path` → `Db(path)` (`web.py:50`, `__main__.py:103`)
  → `Db.__init__` now `mkdir`s parent → `_connect`/`init_schema` succeed on any
  independent directory. `Settings.pfs_output_dir` path unchanged.
- Feature 2: env `PFS_PORT` → `Settings.pfs_port` → `uvicorn.run(port=...)`
  (`__main__.py:89`). Compose `${PFS_PORT:-8080}` sets both the host↔container map
  and the `PFS_PORT` env consumed by the app + healthcheck.

## Build sequence (checklist for developer-executor)

- [ ] Add `pfs_port: int = 8080` + optional range validation to `config.py`.
- [ ] Add DB-parent-dir `mkdir` to `Db.__init__` in `db.py` (guard `:memory:` and
      empty/`.` parent); add `import pathlib`.
- [ ] Change `__main__.py:89` uvicorn `port=8080` → `port=settings.pfs_port`.
- [ ] Update `docker-compose.yml` ports mapping, add `PFS_PORT` env, and make the
      healthcheck URL read `PFS_PORT`.
- [ ] Add Dockerfile comment noting EXPOSE tracks the PFS_PORT default.
- [ ] Add README `PFS_PORT` row; clarify independent-dirs note.
- [ ] Add tests (see Test approach).
- [ ] Run `pytest`; expect the pre-existing `test_config.py` `.env`-bleed flake
      to be the ONLY failure if the developer has a local `.env` (see
      AGENTS.md:25) — confirm via `git stash` that any failure is that known
      one, not a regression.

## Deployment integration (Docker / docker-compose)

- Build: `docker compose build`.
- Run: `docker compose up -d`.
- Verify the named volume, NOT the host `data/` dir (AGENTS.md:24 gotcha):
  the running container writes to the `pfs-data` named volume.

## Test approach

Existing `tests/test_config.py` covers config fields with the `_env_file=None`
idiom. Add:

**Feature 2 (config):** in `tests/test_config.py`
- `test_port_defaults_8080`: `Settings(**_base_kwargs(), _env_file=None).pfs_port == 8080`.
- `test_port_env_string_coerces_to_int`: monkeypatch `PFS_PORT=9090` →
  `pfs_port == 9090` (int, not str), mirroring
  `test_sync_on_startup_env_string_coerces_to_false`.
- If range validation added: `test_port_out_of_range_raises` (e.g. `70000`)
  asserting `PFS_PORT` in the error, mirroring `test_invalid_mode_raises`.

**Feature 1 (DB parent dir):** in `tests/test_db.py`
- `test_init_creates_missing_parent_dir(tmp_path)`: `nested = tmp_path /
  "does" / "not" / "exist" / "state.db"`; `Db(str(nested))`; then assert
  `nested.parent.is_dir()` and `init_schema()` + a round-trip
  (`set_token`/`get_token` or `count_downloaded`) succeed — this is the exact
  scenario that fails today.
- `test_init_memory_db_ok`: `Db(":memory:").init_schema()` does not raise (guards
  the `:memory:` branch).
- Optionally `test_db_and_output_dirs_independent` at the `Settings` level: set
  `pfs_db_path` and `pfs_output_dir` under two disjoint temp subtrees, construct
  `Db`, assert only the DB tree was created by `Db` (output dir creation stays the
  caller's job).

Success = new tests pass; full suite shows no NEW failures beyond the documented
`.env`-bleed flake.

## Verification strategy

**Option C — both automated tests AND deployment smoke test.**
Rationale: the config/path logic is pure and unit-testable (tests give fast,
deterministic proof of the DB-dir fix and port coercion), while the port change
also touches Docker/compose wiring that only a running container can prove.

Deployment smoke test:
1. `docker compose build && docker compose up -d`.
2. Confirm the app bound the port: `docker compose logs polar-fit-sync` shows
   uvicorn on the configured port; `curl http://localhost:8080/healthz` → 200
   with defaults.
3. Custom-port check: `PFS_PORT=9090 docker compose up -d --force-recreate`, then
   `curl http://localhost:9090/healthz` → 200 and confirm the healthcheck reports
   healthy (`docker compose ps`) — proving the in-container healthcheck followed
   the new port.
4. Independent-DB check: set `PFS_DB_PATH=/db/state.db` (a directory NOT under the
   output dir) alongside a volume/mount for `/db`, restart, and confirm the
   container starts and the DB file is created — inspect via
   `docker run --rm -v polar-fit-sync_pfs-data:/data alpine ls /data/fit` for FIT
   output and the corresponding DB mount for `state.db` (never the host-side
   `data/` dir — AGENTS.md:24).

## Temporary adjustments
None required.

## Out of scope
- Kubernetes manifests (do not exist in-repo; tracked in BACKLOG).
- Making `EXPOSE` dynamic (informational only; no functional effect).
- Live-reload of any config (all `PFS_*` vars remain restart-only, consistent with
  every existing var).
- Migrating or relocating existing DB/FIT files when paths change (forward-only,
  same stance as `PFS_SYNC_START_DATE`).
- TLS / host binding changes (still `0.0.0.0`).
