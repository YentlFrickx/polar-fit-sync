# test___main__.py — unit tests for the CLI entry point's logging setup.
#
# We test _HealthzLogFilter in isolation by constructing logging.LogRecord
# instances directly (mimicking uvicorn's access-log format), rather than
# spinning up a real server. This keeps the test fast and decoupled from
# uvicorn's internals.

import logging

from polar_fit_sync.__main__ import _HealthzLogFilter


def _access_log_record(path: str, method: str = "GET", status_code: int = 200) -> logging.LogRecord:
    """Build a LogRecord mimicking uvicorn's access-log message format.

    uvicorn.access records look like:
        '%s - "%s %s HTTP/%s" %d' % (client_addr, method, path, http_version, status_code)
    """
    message = '%s - "%s %s HTTP/%s" %d' % (
        "127.0.0.1:12345", method, path, "1.1", status_code,
    )
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_healthz_request_is_suppressed():
    record = _access_log_record("/healthz")
    assert _HealthzLogFilter().filter(record) is False


def test_other_path_is_not_suppressed():
    record = _access_log_record("/oauth/callback")
    assert _HealthzLogFilter().filter(record) is True


def test_root_path_is_not_suppressed():
    record = _access_log_record("/")
    assert _HealthzLogFilter().filter(record) is True


def test_path_containing_healthz_as_substring_is_still_suppressed():
    """Documents current (intentionally simple) substring-match behavior.

    A path like '/api/healthz-status' would also be suppressed, since the
    filter checks `"/healthz" in message` rather than an exact path match.
    This is a deliberate simplicity trade-off, not a bug: no such route
    exists in this service, so false-positive suppression is not a real
    concern here.
    """
    record = _access_log_record("/api/healthzzz")
    assert _HealthzLogFilter().filter(record) is False
