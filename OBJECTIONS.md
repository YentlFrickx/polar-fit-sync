---
spec: SPEC.md / PLAN.md
date: 2026-06-04
objections:
  - id: O1
    category: premise
    severity: critical
    claim: "The spec asserts that the Polar v3 /exercises endpoint is non-transactional and the transaction flow is deprecated — but provides no evidence, and the established documentation contradicts it."
    evidence: "\"Exercises are NOT transaction-based in current v3. Simple list + fetch\" and \"Note: older docs describe a create/list/commit transaction flow. The modern v3 endpoints above are non-transactional\""
    disposition: rejected
    disposition_rationale: "User accepts the non-transactional v3 endpoint claim as correct."

  - id: O2
    category: implementation
    severity: critical
    claim: "SQLite on an NFS-backed RWX PVC — the spec's preferred storage topology — is a well-documented reliability anti-pattern that can produce silent database corruption under concurrent writes."
    evidence: "\"Both the DB file and the FIT output dir live on the same PVC\"; \"k8s/pvc.yaml — polar-fit-sync-data (RWX or RWO; if RWO, web Deployment and CronJob must land on the same node — RWX preferred, e.g. NFS)\""
    disposition: accepted
    disposition_rationale: "Architecture changed: single Deployment with internal scheduler replaces the web Deployment + CronJob split. Only one pod accesses SQLite at a time, eliminating concurrent-write corruption risk. K8s CronJob and multi-pod PVC concern are eliminated."

  - id: O3
    category: premise
    severity: high
    claim: "The API response includes expires_in, which contradicts the spec's assertion that tokens never expire. If this is wrong, the app will silently stop syncing with no diagnostic path."
    evidence: "\"Response: access_token, token_type, expires_in, x_user_id\" followed immediately by \"Tokens do NOT expire unless revoked → no refresh-token logic needed.\""
    disposition: accepted
    disposition_rationale: "Store expires_in and created_at; detect 401 responses and surface a re-link prompt on the web UI status page rather than silently failing."

  - id: O4
    category: scope
    severity: high
    claim: "list_exercises is spec'd to return list[Exercise] with no pagination handling. Polar list endpoints commonly paginate; a user with years of workouts will silently receive only the first page on every sync."
    evidence: "\"list_exercises(access_token) -> list[Exercise]\" with no mention of cursor, offset, or Link headers."
    disposition: rejected
    disposition_rationale: "User accepts current non-paginated list as sufficient for this personal tool."

  - id: O5
    category: implementation
    severity: high
    claim: "The OAuth redirect_uri bootstrapping creates a circular dependency: the redirect_uri stored in the K8s Secret must exactly match the URL Polar redirects to, but port-forward changes the effective base URL at setup time."
    evidence: "\"kubectl port-forward svc/polar-fit-sync 8080:8080; open http://localhost:8080; click Connect; complete Polar auth (redirect URI must resolve to this)\" and \"POLAR_REDIRECT_URI yes (web) — Must match Polar app config\""
    disposition: rejected
    disposition_rationale: "User accepts the port-forward bootstrap approach as sufficient for this personal tool."

  - id: O6
    category: specification quality
    severity: high
    claim: "authlib is cited in the architecture rationale as a core dependency but does not appear in the pyproject.toml dependency list in PLAN Phase 0. An implementation that follows the plan faithfully will fail to import it."
    evidence: "SPEC §5: \"Python's httpx + authlib cover it in very little code\"; PLAN Phase 0 deps do not include authlib."
    disposition: accepted
    disposition_rationale: "Remove authlib from the rationale. OAuth flow is implemented manually with httpx (Basic auth header, form body). All deps must be consistent between SPEC rationale and PLAN pyproject.toml."

  - id: O7
    category: implementation
    severity: medium
    claim: "File writes are not atomic: a crash mid-write leaves a truncated FIT file that will be silently overwritten on the next run without error."
    evidence: "\"The sync SHALL record a downloaded id only after the file is fully written\" (FR 7); no mention of write-to-tmp-then-rename."
    disposition: accepted
    disposition_rationale: "Use write-to-tmp-then-atomic-rename (POSIX rename) for all FIT file writes. Spec and tests must reflect this."

  - id: O8
    category: specification quality
    severity: medium
    claim: "The oauth_state TTL is referenced three times but never given a concrete value."
    evidence: "oauth_state DDL comment \"short-lived; cleaned on use/expiry\"; db.py spec \"consume_state(state) -> bool (validate + delete + TTL)\" — no TTL value defined."
    disposition: accepted
    disposition_rationale: "TTL is explicitly 10 minutes. Spec, implementation, and tests must all use this value."

  - id: O9
    category: specification quality
    severity: medium
    claim: "POLAR_CLIENT_ID is marked required:yes for all commands, but the sync command only needs the access_token already stored in SQLite."
    evidence: "Config table: POLAR_CLIENT_ID required: yes (no web-only caveat); sync data flow uses Bearer token only."
    disposition: accepted
    disposition_rationale: "Mark POLAR_CLIENT_ID and POLAR_CLIENT_SECRET as required only for the web command (matching the existing POLAR_CLIENT_SECRET pattern). Sync only requires DB access."

  - id: O10
    category: risk
    severity: medium
    claim: "429 backoff honours RateLimit-Reset without capping sleep duration, potentially pinning a CronJob pod indefinitely."
    evidence: "\"_request_with_backoff(...) honouring 429 RateLimit-Reset, bounded retries\" — no max sleep cap defined."
    disposition: rejected
    disposition_rationale: "Single-Deployment architecture eliminates the CronJob lock concern. User accepts current backoff description."

  - id: O11
    category: risk
    severity: medium
    claim: "RWX PVC unavailable by default on common personal K8s distributions; RWO fallback needs node affinity not in the manifests."
    evidence: "\"RWX preferred, e.g. NFS\"; no nodeAffinity in manifest list."
    disposition: rejected
    disposition_rationale: "Single-Deployment architecture means only one pod mounts the PVC; RWX vs RWO is no longer a multi-pod concern."

  - id: O12
    category: risk
    severity: low
    claim: "GET /oauth/start creates state rows with no cleanup of abandoned flows."
    evidence: "No periodic cleanup path described; cleanup only on consume_state."
    disposition: rejected
    disposition_rationale: "Negligible for personal use; deferred."

  - id: NEW1
    category: scope
    severity: high
    claim: "New requirement: single Deployment with internal scheduler instead of Deployment + CronJob split."
    evidence: "User feedback: 'I don't like a split between deployment and k8s cronjob, just have 1 deployment and do the cron within the deployment'"
    disposition: accepted
    disposition_rationale: "Single long-running Deployment runs both the web UI and the sync scheduler in-process (APScheduler or asyncio periodic task). Eliminates the CronJob, shared-PVC concurrency risk, and ops complexity."

  - id: NEW2
    category: scope
    severity: high
    claim: "New requirement: webhook support as an alternative to polling."
    evidence: "User feedback: 'there is also the option for webhooks that should be supported'"
    disposition: accepted
    disposition_rationale: "Add webhook receiver endpoint to the web UI. Polar can push exercise notifications to a registered webhook URL. The app should support both modes: polling (default, no public ingress needed) and webhook (faster, requires public endpoint). UI should allow configuring the preferred mode."
---

## O1 — premise — critical — REJECTED

### Claim
The spec asserts the Polar v3 /exercises endpoint is non-transactional but provides no evidence.

### Evidence
> "Exercises are NOT transaction-based in current v3."

### Disposition
Rejected. User accepts the claim as correct.

---

## O2 — implementation — critical — ACCEPTED

### Claim
SQLite on an NFS-backed RWX PVC is a documented recipe for database corruption under concurrent writes from web pod and CronJob pod.

### Evidence
> "Both the DB file and the FIT output dir live on the same PVC"
> "RWX preferred, e.g. NFS"

### Resolution
Architecture changed to a single Deployment running both web UI and the sync scheduler in-process. One pod, one SQLite writer, no NFS locking concern.

---

## O3 — premise — high — ACCEPTED

### Claim
`expires_in` in the token exchange response contradicts the "tokens never expire" assertion.

### Evidence
> "Response: `access_token`, `token_type`, `expires_in`, `x_user_id`" … "Tokens do NOT expire"

### Resolution
Store `expires_in` + `created_at` in the token table. Detect 401 responses during sync and surface a "Token expired — re-link required" warning on the web UI status page.

---

## O4 — scope — high — REJECTED

### Claim
No pagination handling for `list_exercises`.

### Disposition
Rejected. Accepted as sufficient for personal use.

---

## O5 — implementation — high — REJECTED

### Claim
OAuth redirect URI bootstrapping is circular with port-forward.

### Disposition
Rejected. Port-forward bootstrap is accepted as sufficient.

---

## O6 — specification quality — high — ACCEPTED

### Claim
`authlib` in rationale but absent from deps.

### Resolution
Remove `authlib` from the architecture rationale. OAuth is implemented manually with `httpx` Basic auth and form-body POST. `pyproject.toml` deps must match the rationale exactly.

---

## O7 — implementation — medium — ACCEPTED

### Claim
Non-atomic file writes leave truncated FIT files silently on disk.

### Resolution
All FIT downloads written to `{filename}.tmp` first, then atomically renamed to final path via `os.rename()`. Record in DB only after successful rename.

---

## O8 — specification quality — medium — ACCEPTED

### Claim
`oauth_state` TTL undefined.

### Resolution
TTL = **10 minutes**. Defined in spec, implemented in `consume_state`, tested in `test_db.py`.

---

## O9 — specification quality — medium — ACCEPTED

### Claim
`POLAR_CLIENT_ID` incorrectly marked required for sync command.

### Resolution
Both `POLAR_CLIENT_ID` and `POLAR_CLIENT_SECRET` marked as `yes (web)` in the config table. Sync command requires only DB access (reads stored token).

---

## O10 — risk — medium — REJECTED

Rejected. Architecture change eliminates CronJob concern.

---

## O11 — risk — medium — REJECTED

Rejected. Single Deployment eliminates multi-pod PVC concern.

---

## O12 — risk — low — REJECTED

Rejected. Negligible for personal use.

---

## NEW1 — scope — high — ACCEPTED

### Claim / Requirement
Replace Deployment + CronJob with a single Deployment running web UI and sync scheduler in-process.

### Resolution
Use APScheduler (or asyncio background task with `asyncio.create_task`) within the FastAPI app. The scheduler runs on the configured interval (env var `PFS_SYNC_INTERVAL_MINUTES`, default `60`). Eliminates the K8s CronJob manifest. Single pod, single SQLite writer.

---

## NEW2 — scope — high — ACCEPTED

### Claim / Requirement
Support webhooks from Polar as an alternative to polling.

### Resolution
Add `POST /webhook/polar` endpoint to the web UI. When Polar sends an exercise notification webhook, the app immediately runs a sync for the new exercise. Support both modes:
- **poll** (default): APScheduler runs sync on interval — no public ingress required.
- **webhook**: Polar sends POST to configured URL; app syncs on receipt. Requires `PFS_WEBHOOK_SECRET` env var for signature verification.

UI settings page shows current mode and webhook URL to register with Polar.

---

## Explicitly not objecting to

- **Single-account design**: The one-row `token` table and explicit "personal tool"
  framing make single-account reasonable.

- **Python over Go**: The rationale is sound for this scale of project.

- **FIT-only downloads**: FIT is the richest format; TCX/GPX exclusion is defensible.
