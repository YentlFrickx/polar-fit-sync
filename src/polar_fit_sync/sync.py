# sync.py — the sync orchestration layer.
#
# Why this file exists: run_sync is the single function that coordinates
# everything needed to download new Polar exercise files. Keeping it separate
# from web.py means it can be called from the scheduler, from the webhook
# handler, and from the CLI sync command without duplicating logic.
#
# Key design decisions:
# - Token expiry is checked at the START of every run, before any API call.
#   This means we never make a network call with a known-expired token, and the
#   run is recorded as token_expired with zero API calls made.
# - Every FIT file is written atomically: bytes go to {path}.tmp first, then
#   os.rename() moves the file to its final name. os.rename is atomic on POSIX
#   (same filesystem), so a crash mid-write never leaves a truncated file where
#   a complete one should be.
# - The database record is inserted only AFTER the rename succeeds. This ensures
#   that a crash between write and rename leaves no DB record, so the file will
#   be retried on the next run.
# - Per-item try/except means that one failed download does not abort the rest.
#   The run status is 'partial' if any item errored.
# - A TokenExpiredError from any API call during a run is caught, the token is
#   flagged in the DB, and the run is closed as token_expired. The service
#   continues running.
#
# What this file does NOT do: it does not handle HTTP routing, scheduler
# lifecycle, or OAuth.

import io
import logging
import os
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import fitparse

from polar_fit_sync.db import Db
from polar_fit_sync.polar import PolarClient, TokenExpiredError

logger = logging.getLogger(__name__)


def _passes_filter(sport: Optional[str], sport_filter: frozenset, mode: str) -> bool:
    """Decide whether one exercise should be kept given the active sport filter.

    An empty sport_filter means no filtering is in effect — always keep the
    exercise (FR3). This is the fast path for the common case where no filter
    env vars are set.

    In include mode a null sport cannot match any named sport, so it is
    dropped (FR5). In exclude mode a null sport is not in any block-list, so
    it is kept (FR6). Comparison is always done in uppercase so the caller
    does not need to normalise before calling (FR4).
    """
    if not sport_filter:
        return True
    sport_upper = sport.upper() if sport else None
    if mode == "include":
        return sport_upper in sport_filter if sport_upper is not None else False
    else:  # exclude
        return sport_upper not in sport_filter if sport_upper is not None else True


def _after_start_date(start_time: Optional[str], start_date: Optional[datetime]) -> bool:
    """Decide whether one exercise is on or after the configured sync cutoff.

    No start_date configured is the fast path (FR7): return True immediately
    without touching start_time at all, so a run with the feature disabled
    pays zero extra cost per exercise.

    A null/empty start_time is kept rather than excluded (FR5) — we never
    drop an exercise we cannot date, mirroring the codebase's existing
    tolerance for null dates in _build_path (falls back to "00000000" rather
    than erroring).

    Polar documents start_time as local time with NO offset in practice (e.g.
    '2008-10-13T10:40:02', no trailing Z) alongside a separate
    start_time_utc_offset field we don't consume. fromisoformat on such a
    value yields a naive datetime, which would raise TypeError when compared
    to the aware start_date below. We normalize naive -> UTC before
    comparing, the same idiom already used for token-expiry parsing at
    sync.py:138-140 (NOT _build_path, which never compares datetimes and so
    never needed this normalization).

    Any parse/compare failure (including a non-string start_time, hence
    catching TypeError as well as ValueError) is treated as "keep, but tell
    someone" — logged at WARNING rather than DEBUG so a systemic Polar
    date-format drift is visible under this project's default
    PFS_LOG_LEVEL=INFO instead of silently neutering the whole feature.
    """
    if start_date is None:
        return True
    if not start_time:
        return True
    try:
        parsed = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed >= start_date
    except (ValueError, TypeError):
        logger.warning(
            "Could not parse start_time %r for date-gate comparison; keeping exercise.",
            start_time,
        )
        return True


def _parse_fit_sport(content: bytes) -> Optional[str]:
    """Extract the session sport from raw FIT bytes, uppercased.

    Polar's exercise-list API reports a coarse `sport` field that collapses
    distinct activity types (e.g. walking vs. true-generic/uncategorized) into
    the same string (`OTHER`). The FIT file's own `session` message carries a
    finer-grained value, so this helper re-derives sport from the downloaded
    bytes themselves rather than trusting the API label.

    Only the FIRST `session` message is consulted. This is a deliberate
    choice, not an oversight: genuinely multi-session (multisport) FIT files
    are an acknowledged, unhandled edge case for this feature — no attempt is
    made to reconcile or select among multiple sessions. See
    OBJECTIONS_FIT_SPORT_PARSING.md O6 for the disposition that accepted this
    default.

    Returns None (rather than raising) when parsing succeeds but no session
    message carries a usable sport value, so the caller can distinguish
    "parsed fine, nothing to say" from "parsing itself failed". Any exception
    from fitparse (corrupt bytes, wrong format, etc.) is left to propagate —
    this stays a pure transform with no logging of its own, matching the
    existing convention of pure helpers like _passes_filter/_build_path; the
    caller owns the fallback decision and the warning log.
    """
    fit = fitparse.FitFile(io.BytesIO(content))
    for msg in fit.get_messages("session"):
        sport = msg.get_value("sport")
        if sport:
            return str(sport).upper()
        return None
    return None


@dataclass
class RunResult:
    """Summary returned by run_sync to callers (scheduler, webhook, CLI)."""

    run_id: Optional[int]
    status: str       # ok | partial | no_token | token_expired | error
    new_files: int
    errors: int


async def run_sync(
    db: Db,
    client: PolarClient,
    output_dir: str,
    target_id: Optional[str] = None,
    trigger: str = "poll",
    sport_filter: frozenset = frozenset(),
    filter_mode: str = "include",
    start_date: Optional[datetime] = None,
) -> RunResult:
    """Download new Polar exercise FIT files incrementally.

    Guarantees:
    - Returns without raising under all expected error conditions.
    - Records a sync_run row for every invocation (even no_token / token_expired).
    - Writes each FIT file atomically: .tmp → rename → DB record.
    - Idempotent: a second call with no new exercises writes zero files and
      records 'ok'.
    """
    run_id = db.start_run(trigger)

    # --- Check: is an account linked? ---
    token = db.get_token()
    if token is None:
        logger.warning("No token found. Run the web setup to link a Polar account first.")
        db.finish_run(run_id, new_files=0, errors=0, status="no_token")
        return RunResult(run_id=run_id, status="no_token", new_files=0, errors=0)

    # --- Check: has the token expired by computation? ---
    if token.expires_in is not None:
        issued = datetime.fromisoformat(token.created_at.replace("Z", "+00:00"))
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        age_seconds = (now - issued).total_seconds()
        if age_seconds > token.expires_in:
            logger.warning(
                "Token has expired (issued %.0fs ago, expires_in=%d). Re-link required.",
                age_seconds,
                token.expires_in,
            )
            db.finish_run(run_id, new_files=0, errors=0, status="token_expired")
            return RunResult(
                run_id=run_id, status="token_expired", new_files=0, errors=0
            )

    # --- Also bail early if the token was previously flagged by a 401 ---
    if token.status == "token_expired":
        logger.warning("Token is flagged as expired. Re-link required.")
        db.finish_run(run_id, new_files=0, errors=0, status="token_expired")
        return RunResult(run_id=run_id, status="token_expired", new_files=0, errors=0)

    pathlib.Path(output_dir).mkdir(parents=True, exist_ok=True)

    new_files = 0
    errors = 0

    try:
        # --- Fetch exercise list (targeted or full) ---
        if target_id is not None:
            try:
                exercise = client.get_exercise(token.access_token, target_id)
                exercises = [exercise]
            except TokenExpiredError:
                raise
            except Exception:
                logger.exception(
                    "Failed to fetch targeted exercise %s; falling back to full list.",
                    target_id,
                )
                exercises = client.list_exercises(token.access_token)
        else:
            exercises = client.list_exercises(token.access_token)

        # --- Filter to exercises we have not already downloaded, and skip
        # --- exercises we remember filtering out (unless the filter has
        # --- since loosened enough to let their stored sport through).
        #
        # skipped_sports is loaded once per run, not once per exercise, to
        # avoid doubling the per-item DB round trips the existing
        # is_downloaded check already makes. It is only queried when a
        # filter is active — with no filter, _passes_filter is always True,
        # so nothing is ever recorded as skipped and the lookup would be
        # pure overhead (FR7).
        skipped_sports = db.list_skipped_sports() if sport_filter else {}

        # Dedup runs against the FULL, unfiltered exercise list. Sport
        # filtering used to happen here, before dedup — it now happens
        # per-exercise inside the download loop below, after the FIT bytes
        # are downloaded and parsed (see the loop for why: Polar's coarse API
        # sport is ambiguous, e.g. walking vs. generic both report "OTHER",
        # and only the downloaded FIT session data can disambiguate them).
        # Dedup itself only needs the exercise id, so it is unaffected by
        # where filtering happens and stays first.
        new_exercises = []
        already_downloaded = 0
        before_start_date = 0
        remembered_skips = 0
        for ex in exercises:
            if db.is_downloaded(ex.id):
                # Download-dedup takes strict precedence over skip-exclusion:
                # checked first and short-circuits before any skip state is
                # consulted for this id. This is what makes a stale
                # skipped_exercise row left behind by a crash between
                # record_downloaded and delete_skipped harmless (FR6) — it is
                # simply never read once the exercise is downloaded.
                already_downloaded += 1
                continue
            if not _after_start_date(ex.start_time, start_date):
                # Excluded by the configured start-date floor. This check
                # runs before the skip-tracking block below on purpose: a
                # date-excluded exercise is not a sport-filter skip, and
                # recording it in skipped_exercise would pollute that table
                # with rows the sport filter never produced (FR8).
                before_start_date += 1
                continue
            if sport_filter and ex.id in skipped_sports:
                stored_sport = skipped_sports[ex.id]
                # Recompute against the CURRENT filter rather than trusting a
                # persisted signature. Still failing -> stays excluded with
                # no re-download. Now passing (the filter loosened) -> falls
                # through below to be re-downloaded and re-verified against
                # live FIT bytes (FR3/FR4).
                if not _passes_filter(stored_sport, sport_filter, filter_mode):
                    remembered_skips += 1
                    continue
            new_exercises.append(ex)

        logger.info(
            "Sync run (trigger=%s): %d total, %d new, %d already downloaded, "
            "%d before start date, %d remembered skips.",
            trigger,
            len(exercises),
            len(new_exercises),
            already_downloaded,
            before_start_date,
            remembered_skips,
        )

        # --- Download each new exercise ---
        filtered_count = 0
        for ex in new_exercises:
            try:
                content = client.download_fit(token.access_token, ex.id)

                # Determine the effective sport from the FIT bytes themselves,
                # falling back to the coarse API sport on any parse failure.
                # This lookup is deliberately isolated in a local try/except
                # (rather than the outer per-exercise handler below) so that a
                # parse failure never counts as an error (FR3/FR9) — it is an
                # expected, resilient fallback path, not a sync failure.
                effective_sport = ex.sport
                try:
                    parsed_sport = _parse_fit_sport(content)
                    if parsed_sport:
                        effective_sport = parsed_sport
                    else:
                        logger.warning(
                            "No session sport found in FIT data for exercise %s; "
                            "falling back to API sport %r.", ex.id, ex.sport,
                        )
                except Exception:
                    logger.warning(
                        "Failed to parse FIT data for exercise %s; "
                        "falling back to API sport %r.", ex.id, ex.sport, exc_info=True,
                    )

                # Filtering now happens here — post-download, post-parse —
                # rather than on the coarse API sport before dedup, so that
                # exercises whose true sport only the FIT bytes can reveal
                # (e.g. walking vs. generic, both API sport="OTHER") are
                # classified correctly before the filter decision is made.
                if not _passes_filter(effective_sport, sport_filter, filter_mode):
                    filtered_count += 1
                    # Remember this skip so future runs don't re-download and
                    # re-parse it just to reach the same conclusion. INSERT OR
                    # REPLACE inside record_skipped means a re-evaluation that
                    # skips again (e.g. FR4's re-verify path) refreshes the
                    # stored sport/timestamp rather than erroring.
                    db.record_skipped(ex.id, effective_sport)
                    logger.info(
                        "Exercise %s (effective sport=%s) filtered out post-download; discarding.",
                        ex.id, effective_sport,
                    )
                    continue

                final_path = _build_path(output_dir, ex.start_time, effective_sport, ex.id)
                _atomic_write(final_path, content)
                db.record_downloaded(
                    exercise_id=ex.id,
                    file_path=str(final_path),
                    sport=effective_sport,
                    start_time=ex.start_time,
                )
                # Clean up any stale skip row now that the exercise has
                # actually been written (FR5). Two separate, non-atomic Db
                # calls, matching this codebase's one-connection-per-call
                # convention (FR6) — a crash between them is harmless because
                # is_downloaded is checked before skip state on every future
                # run (see the work-list loop above).
                db.delete_skipped(ex.id)
                new_files += 1
                logger.info("Downloaded %s → %s", ex.id, final_path)
            except TokenExpiredError:
                # Re-raise so the outer handler can flag the token.
                raise
            except Exception:
                logger.exception("Failed to download exercise %s.", ex.id)
                errors += 1

        if sport_filter:
            logger.info(
                "Sport filter (%s): %d of %d exercises filtered out post-download "
                "this run (%d remembered from earlier runs, not re-downloaded).",
                filter_mode, filtered_count, len(new_exercises), remembered_skips,
            )

    except TokenExpiredError:
        logger.warning("Received 401 from Polar. Flagging token as expired.")
        db.set_token_status("token_expired")
        db.finish_run(run_id, new_files=new_files, errors=errors, status="token_expired")
        return RunResult(
            run_id=run_id, status="token_expired", new_files=new_files, errors=errors
        )
    except Exception:
        logger.exception("Unexpected error during sync run.")
        db.finish_run(run_id, new_files=new_files, errors=errors + 1, status="error")
        return RunResult(
            run_id=run_id, status="error", new_files=new_files, errors=errors + 1
        )

    status = "ok" if errors == 0 else "partial"
    db.finish_run(run_id, new_files=new_files, errors=errors, status=status)
    return RunResult(run_id=run_id, status=status, new_files=new_files, errors=errors)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_name(value: str) -> str:
    """Replace non-alphanumeric characters (except underscores) with underscores.

    Used to sanitize sport names and exercise ids so they are safe as filename
    components on all filesystems.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", value)


def _build_path(
    output_dir: str,
    start_time: Optional[str],
    sport: Optional[str],
    exercise_id: str,
) -> pathlib.Path:
    """Construct the deterministic, human-readable path for one FIT file.

    Format: {YYYYMMDD}_{sport}_{id}.fit
    The exercise_id is always present, guaranteeing uniqueness even when two
    exercises share a start date and sport.
    """
    date_part = "00000000"
    if start_time:
        try:
            dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            date_part = dt.strftime("%Y%m%d")
        except ValueError:
            pass

    sport_part = _safe_name(sport) if sport else "UNKNOWN"
    id_part = _safe_name(exercise_id)
    filename = f"{date_part}_{sport_part}_{id_part}.fit"
    return pathlib.Path(output_dir) / filename


def _atomic_write(final_path: pathlib.Path, content: bytes) -> None:
    """Write content to a .tmp file then rename it to final_path.

    os.rename is atomic on POSIX when source and destination are on the same
    filesystem, which is the case here (both paths are under output_dir on the
    same PVC). A crash after write but before rename leaves the .tmp orphan on
    disk but never the final file — so the exercise id is not recorded and the
    download will be retried on the next sync run.
    """
    # Append .tmp to the full filename (e.g. foo.fit.tmp) rather than replacing
    # the .fit extension (which would yield foo.tmp). The spec requires the
    # pattern {final_path}.tmp so that the extension makes the temp status clear
    # and glob patterns like "*.fit.tmp" can target only these orphan files.
    tmp_path = final_path.with_name(final_path.name + ".tmp")
    tmp_path.write_bytes(content)
    os.rename(tmp_path, final_path)
