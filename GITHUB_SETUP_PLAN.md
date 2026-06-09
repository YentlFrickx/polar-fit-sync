# GitHub Setup & CI/CD Plan — polar-fit-sync

## Codebase findings

- **Stack**: Python 3.12, FastAPI, packaged with `hatchling`. Dev deps under `[project.optional-dependencies].dev` — `pip install ".[dev]"` installs pytest + pytest-asyncio + respx.
- **Tests**: pytest with `asyncio_mode = "auto"`, `testpaths = ["tests"]`. Files: `tests/test_polar.py`, `tests/test_webhook.py`, `tests/test_web.py`, `tests/test_db.py`, `tests/test_scheduler.py`, `tests/test_sync.py`.
- **Docker**: single-stage `python:3.12-slim`, `EXPOSE 8080`, `COPY src/ src/` then `pip install .`.
- **Dual version source**: `pyproject.toml:3` (`version = "0.1.0"`) AND `src/polar_fit_sync/__init__.py` (`__version__ = "0.1.0"`). Release Please must update both.
- **Secrets hygiene**: `.gitignore` ignores `.env`; `.env.example` exists (safe to commit — placeholders only).
- **Not a git repo yet**; README.md exists and is clean.

---

## User Stories

- **US-1** — As the maintainer, I want every push and PR to `main` to run the test suite automatically.
- **US-2** — As the maintainer, I want conventional-commit-driven automated releases with version bumps and a changelog.
- **US-3** — As an operator, I want a published, version-tagged Docker image on GHCR for each release.
- **US-4** — As the maintainer, I want the project public on GitHub with no secrets ever committed.

## Acceptance Scenarios

- **AS-1 (CI on PR)** — Given a PR to `main`, CI runs checkout → Python 3.12 → `pip install ".[dev]"` → `pytest` and reports pass/fail.
- **AS-2 (CI on push)** — Given a push to `main`, CI runs the same steps.
- **AS-3 (Release PR)** — Given conventional commits on `main` with no open release, Release Please opens/updates a release PR bumping `pyproject.toml` + `__init__.py`; Docker job is skipped.
- **AS-4 (Release published)** — Given a release PR is merged, Release Please creates the GitHub release/tag; Docker job builds and pushes `ghcr.io/yentlfrickx/polar-fit-sync:<tag>` and `:latest`.
- **AS-5 (Public repo)** — After setup completes, `github.com/YentlFrickx/polar-fit-sync` is public with workflows visible under Actions.
- **AS-6 (No secrets)** — `.env` is absent from the working tree and all history; `.env.example` is present.

---

## Architecture Decision

Two separate workflow files (CI vs. release). Rationale: CI must run on PRs (no write perms, fast feedback); release logic runs only on `main` pushes with elevated `contents`/`pull-requests`/`packages` write perms. Separation keeps least-privilege scoping clean.

Release Please version sync handled via committed `release-please-config.json` + `.release-please-manifest.json` (v4 config-driven approach) so both `pyproject.toml` and `__init__.py` bump together.

---

## Files to Create / Modify

### 1. `.github/workflows/ci.yml`
```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
          cache: pip
      - run: pip install ".[dev]"
      - run: pytest
```

### 2. `.github/workflows/release.yml`
```yaml
name: Release

on:
  push:
    branches: [main]

permissions:
  contents: write
  pull-requests: write
  packages: write

jobs:
  release-please:
    runs-on: ubuntu-latest
    outputs:
      release_created: ${{ steps.release.outputs.release_created }}
      tag_name: ${{ steps.release.outputs.tag_name }}
    steps:
      - uses: googleapis/release-please-action@v4
        id: release
        with:
          token: ${{ secrets.GITHUB_TOKEN }}
          config-file: release-please-config.json
          manifest-file: .release-please-manifest.json

  docker:
    needs: release-please
    if: ${{ needs.release-please.outputs.release_created == 'true' }}
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: |
            ghcr.io/yentlfrickx/polar-fit-sync:${{ needs.release-please.outputs.tag_name }}
            ghcr.io/yentlfrickx/polar-fit-sync:latest
```

### 3. `release-please-config.json`
```json
{
  "packages": {
    ".": {
      "release-type": "python",
      "extra-files": ["src/polar_fit_sync/__init__.py"]
    }
  }
}
```

### 4. `.release-please-manifest.json`
```json
{ ".": "0.0.0" }
```
Starting at `0.0.0` so the first `feat:` commit bumps to `0.1.0`.

### 5. `src/polar_fit_sync/__init__.py` — add version-tracking comment
Add `  # x-release-please-version` at the end of the `__version__` line so Release Please knows which line to bump.

---

## Deployment Strategy

Two-commit strategy: infrastructure first, implementation second.

```
# Commit 1 — infra only (no release trigger)
git init -b main
git add .gitignore .github/ release-please-config.json .release-please-manifest.json
git commit -m "chore: add CI/CD workflows and release automation"

# Create repo + push commit 1
gh repo create YentlFrickx/polar-fit-sync --public --source=. --remote=origin
git remote set-url origin git@github.com:YentlFrickx/polar-fit-sync.git
git push -u origin main

# Commit 2 — implementation (feat: triggers Release Please → v0.1.0 PR)
git add -A   # everything else; .env excluded by .gitignore
git commit -m "feat: initial polar-fit-sync implementation"
git push
```

Manifest starts at `0.0.0` so the `feat:` in commit 2 bumps the first release to `v0.1.0`.

## Test Strategy

**No automated tests.** Reason: deliverables are GitHub Actions YAML and repo configuration. No local test framework exists for Actions workflows — they only execute on GitHub's runners after push. Existing pytest suite is unchanged and is what CI itself runs.

## Verification Strategy

Option B (deployment verification) — after push:
1. `gh repo view YentlFrickx/polar-fit-sync --json visibility` → `"PUBLIC"`
2. `git ls-files | grep -E '^\.env$'` → empty; `.env.example` present
3. `git log --all --full-history -- .env` → empty
4. `gh run list --limit 5` → CI run created; check green with `gh run watch`
5. `gh pr list` → Release Please opened a release PR
6. Workflows visible at `github.com/YentlFrickx/polar-fit-sync/actions`

---

## Estimated Scope

5 files: 4 new + 1 one-line comment edit in `src/polar_fit_sync/__init__.py`.
