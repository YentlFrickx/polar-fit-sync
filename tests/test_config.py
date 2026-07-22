import pytest
from pydantic import ValidationError

from polar_fit_sync.config import Settings


def _base_kwargs(**overrides) -> dict:
    defaults = dict(
        polar_client_id="test-client",
        polar_client_secret="test-secret",
        polar_redirect_uri="http://localhost/oauth/callback",
        pfs_db_path="/tmp/test.db",
        pfs_output_dir="/tmp/fit",
        pfs_sync_mode="poll",
        pfs_log_level="ERROR",
    )
    defaults.update(overrides)
    return defaults


def test_default_filter_empty():
    settings = Settings(**_base_kwargs())
    assert settings.pfs_sport_filter == ""
    assert settings.pfs_sport_filter_mode == "include"
    assert settings.sport_filter_set() == frozenset()


def test_filter_parsing_trims_and_uppercases():
    settings = Settings(**_base_kwargs(pfs_sport_filter="running, Cycling ,SWIMMING"))
    assert settings.sport_filter_set() == frozenset({"RUNNING", "CYCLING", "SWIMMING"})


def test_invalid_mode_raises():
    with pytest.raises((ValueError, ValidationError)):
        Settings(**_base_kwargs(pfs_sport_filter_mode="banana"))


def test_empty_string_yields_empty_set():
    settings = Settings(**_base_kwargs(pfs_sport_filter=""))
    assert settings.sport_filter_set() == frozenset()


def test_valid_start_date_parses_to_aware_utc_datetime():
    """Scenario 1 (config side): PFS_SYNC_START_DATE=2026-01-01 parses to an
    aware UTC datetime via Settings.sync_start_date()."""
    settings = Settings(**_base_kwargs(pfs_sync_start_date="2026-01-01"), _env_file=None)
    parsed = settings.sync_start_date()
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert parsed.year == 2026 and parsed.month == 1 and parsed.day == 1
    assert parsed.hour == 0 and parsed.minute == 0 and parsed.second == 0


def test_default_start_date_disabled():
    """Empty/unset PFS_SYNC_START_DATE -> sync_start_date() returns None (disabled default)."""
    settings = Settings(**_base_kwargs(), _env_file=None)
    assert settings.pfs_sync_start_date == ""
    assert settings.sync_start_date() is None


def test_malformed_start_date_raises():
    """A malformed PFS_SYNC_START_DATE must fail fast at Settings construction,
    mirroring test_invalid_mode_raises. Explicit kwargs / _env_file=None avoid
    the known local-.env bleed-through gotcha (AGENTS.md GOTCHAS)."""
    with pytest.raises((ValueError, ValidationError)) as excinfo:
        Settings(**_base_kwargs(pfs_sync_start_date="banana"), _env_file=None)
    assert "PFS_SYNC_START_DATE" in str(excinfo.value)


def test_malformed_start_date_invalid_calendar_date_raises():
    """A syntactically date-like but calendrically invalid value (e.g. month 13,
    day 45) must also raise, not silently truncate/wrap."""
    with pytest.raises((ValueError, ValidationError)) as excinfo:
        Settings(**_base_kwargs(pfs_sync_start_date="2026-13-45"), _env_file=None)
    assert "PFS_SYNC_START_DATE" in str(excinfo.value)


def test_sync_on_startup_defaults_true():
    """FR1: PFS_SYNC_ON_STARTUP defaults to True (opt-out, not opt-in)."""
    settings = Settings(**_base_kwargs(), _env_file=None)
    assert settings.pfs_sync_on_startup is True


def test_sync_on_startup_kwarg_false():
    """Settings(pfs_sync_on_startup=False, _env_file=None) yields False."""
    settings = Settings(**_base_kwargs(pfs_sync_on_startup=False), _env_file=None)
    assert settings.pfs_sync_on_startup is False


@pytest.mark.parametrize("raw_value", ["false", "0"])
def test_sync_on_startup_env_string_coerces_to_false(raw_value, monkeypatch):
    """pydantic-settings' standard boolean coercion: env-string "false"/"0"
    must coerce to False, not to a truthy non-empty string."""
    monkeypatch.setenv("PFS_SYNC_ON_STARTUP", raw_value)
    settings = Settings(**_base_kwargs(), _env_file=None)
    assert settings.pfs_sync_on_startup is False
