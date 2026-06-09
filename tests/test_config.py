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
