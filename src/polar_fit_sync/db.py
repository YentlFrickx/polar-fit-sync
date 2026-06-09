# db.py — the only module that touches SQLite.
#
# Why this file exists: isolating all SQL in one class means every other module
# works through a clean Python interface. Tests can swap in an in-memory or
# tmp-file database without touching any other layer.
#
# Key design decisions:
# - The token table is deliberately a single-row table (CHECK id = 1 + INSERT OR
#   REPLACE). There is exactly one Polar account per instance — a second row would
#   be a programming error, not a feature.
# - oauth_state rows carry a created_at timestamp so that OAUTH_STATE_TTL_SECONDS
#   can be enforced in consume_state without a separate background sweeper.
# - downloaded_exercise uses INSERT OR IGNORE so that a re-run of record_downloaded
#   for an id that is already present is a silent no-op rather than an error.
# - sync_run has a trigger column (poll | webhook | manual) so that dashboards can
#   distinguish automated from manually invoked syncs.
#
# What this file does NOT do: it does not make network calls, read environment
# variables, or contain any sync business logic.

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

# Single source of truth for the OAuth state lifetime.
# consume_state rejects any state row older than this many seconds.
OAUTH_STATE_TTL_SECONDS = 600


@dataclass
class Token:
    """Mirrors the single row in the token table."""

    access_token: str
    token_type: str
    x_user_id: str
    member_id: str
    expires_in: Optional[int]
    created_at: str   # ISO8601 UTC string
    status: str       # active | token_expired


class Db:
    """Thread-safe (single-writer) SQLite access layer for polar-fit-sync.

    Every public method opens a connection with the same path, performs its
    operation, and closes. Because the application runs one process with one
    SQLite writer at a time this is sufficient — we never need a connection pool.
    """

    def __init__(self, path: str) -> None:
        # path is the filesystem path to the SQLite file. Passing ":memory:" is
        # valid for tests.
        self._path = path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for better concurrent read performance, even though we
        # have a single writer. It also makes partial-write crashes less likely to
        # leave the DB in an inconsistent state.
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init_schema(self) -> None:
        """Create all tables if they do not yet exist.

        Safe to call on every startup — uses CREATE TABLE IF NOT EXISTS.
        """
        ddl = """
        CREATE TABLE IF NOT EXISTS token (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            access_token TEXT NOT NULL,
            token_type   TEXT NOT NULL DEFAULT 'bearer',
            x_user_id    TEXT NOT NULL,
            member_id    TEXT NOT NULL,
            expires_in   INTEGER,
            created_at   TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS downloaded_exercise (
            exercise_id   TEXT PRIMARY KEY,
            file_path     TEXT NOT NULL,
            sport         TEXT,
            start_time    TEXT,
            downloaded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS oauth_state (
            state      TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sync_run (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at  TEXT NOT NULL,
            finished_at TEXT,
            new_files   INTEGER NOT NULL DEFAULT 0,
            errors      INTEGER NOT NULL DEFAULT 0,
            trigger     TEXT NOT NULL DEFAULT 'poll',
            status      TEXT NOT NULL
        );
        """
        with self._connect() as conn:
            conn.executescript(ddl)

    # -------------------------------------------------------------------------
    # Token operations
    # -------------------------------------------------------------------------

    def get_token(self) -> Optional[Token]:
        """Return the stored token row, or None if no account is linked."""
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM token WHERE id = 1").fetchone()
        if row is None:
            return None
        return Token(
            access_token=row["access_token"],
            token_type=row["token_type"],
            x_user_id=row["x_user_id"],
            member_id=row["member_id"],
            expires_in=row["expires_in"],
            created_at=row["created_at"],
            status=row["status"],
        )

    def set_token(
        self,
        access_token: str,
        token_type: str,
        x_user_id: str,
        member_id: str,
        expires_in: Optional[int],
        created_at: str,
    ) -> None:
        """Upsert the single token row.

        INSERT OR REPLACE ensures that re-linking (a second OAuth flow) silently
        overwrites the old token rather than raising a uniqueness error.
        """
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO token
                    (id, access_token, token_type, x_user_id, member_id,
                     expires_in, created_at, status)
                VALUES (1, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (access_token, token_type, x_user_id, member_id, expires_in, created_at),
            )

    def set_token_status(self, status: str) -> None:
        """Update only the status column on the token row.

        Used when a 401 response from Polar signals that the stored token is no
        longer valid, so the UI can prompt the user to re-link without a full
        token write.
        """
        with self._connect() as conn:
            conn.execute("UPDATE token SET status = ? WHERE id = 1", (status,))

    # -------------------------------------------------------------------------
    # OAuth state (CSRF protection)
    # -------------------------------------------------------------------------

    def create_state(self, state: str) -> None:
        """Insert a new oauth_state row with the current UTC timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO oauth_state (state, created_at) VALUES (?, ?)",
                (state, now),
            )

    def consume_state(self, state: str) -> bool:
        """Validate and delete a state token.

        Returns True only when:
        - the state exists in the DB, AND
        - the row is no older than OAUTH_STATE_TTL_SECONDS seconds.

        The row is deleted on a valid consume so it cannot be replayed. On any
        failure the row is also deleted to avoid leaving orphaned state tokens.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT created_at FROM oauth_state WHERE state = ?", (state,)
            ).fetchone()

            if row is None:
                return False

            # Always clean up the state row — whether it is valid or expired.
            conn.execute("DELETE FROM oauth_state WHERE state = ?", (state,))

            created_at = datetime.fromisoformat(row["created_at"])
            # Ensure the stored timestamp is timezone-aware before comparing.
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)

            age = (datetime.now(timezone.utc) - created_at).total_seconds()
            return age <= OAUTH_STATE_TTL_SECONDS

    # -------------------------------------------------------------------------
    # Downloaded exercise tracking
    # -------------------------------------------------------------------------

    def is_downloaded(self, exercise_id: str) -> bool:
        """Return True if this exercise id has already been recorded."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM downloaded_exercise WHERE exercise_id = ?",
                (exercise_id,),
            ).fetchone()
        return row is not None

    def record_downloaded(
        self,
        exercise_id: str,
        file_path: str,
        sport: Optional[str],
        start_time: Optional[str],
    ) -> None:
        """Record that a FIT file was successfully downloaded and written.

        INSERT OR IGNORE means a duplicate call is silently dropped — safe for
        the retry paths in run_sync.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO downloaded_exercise
                    (exercise_id, file_path, sport, start_time, downloaded_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (exercise_id, file_path, sport, start_time, now),
            )

    # -------------------------------------------------------------------------
    # Sync run log
    # -------------------------------------------------------------------------

    def start_run(self, trigger: str = "poll") -> int:
        """Insert a new sync_run row and return its id.

        The row is created with no finished_at so callers can detect an
        in-progress run after a crash, though we do not currently do this.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO sync_run (started_at, trigger, status)
                VALUES (?, ?, 'running')
                """,
                (now, trigger),
            )
            return cur.lastrowid  # type: ignore[return-value]

    def finish_run(
        self, run_id: int, new_files: int, errors: int, status: str
    ) -> None:
        """Update the sync_run row with the final counts and status."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sync_run
                SET finished_at = ?, new_files = ?, errors = ?, status = ?
                WHERE id = ?
                """,
                (now, new_files, errors, status, run_id),
            )

    # -------------------------------------------------------------------------
    # Aggregate queries used by the web UI
    # -------------------------------------------------------------------------

    def count_downloaded(self) -> int:
        """Return the total number of downloaded exercise files."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM downloaded_exercise"
            ).fetchone()
        return row["n"] if row else 0

    def last_run(self) -> Optional[dict]:
        """Return the most recent finished sync_run row as a plain dict, or None."""
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM sync_run
                WHERE finished_at IS NOT NULL
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return dict(row)
