# web.py — the FastAPI application layer.
#
# Why this file exists: web.py owns the HTTP interface — routing, OAuth flow,
# status page, webhook endpoint, and health probe. It delegates all business
# logic to db.py, polar.py, sync.py, webhook.py, and scheduler.py.
#
# Key design decisions:
# - create_app(settings) is a factory function rather than a module-level app
#   object, so tests can instantiate a fresh app with a custom settings object
#   (different DB path, mode, etc.) without global state leaking between tests.
# - The FastAPI lifespan context manager starts the APScheduler on startup and
#   shuts it down cleanly on shutdown, so the scheduler is always tied to the
#   app lifecycle.
# - POST /webhook/polar reads the raw body before JSON parsing so we can verify
#   the HMAC-SHA256 signature over the exact bytes Polar sent. If we parsed JSON
#   first, whitespace normalisation could invalidate the signature.
# - We return HTTP 404 for /webhook/polar when the mode excludes webhook rather
#   than 405, so that the endpoint appears to not exist at all in poll-only mode.
#
# What this file does NOT do: it does not contain sync business logic, SQL, or
# direct HTTP calls to Polar.

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from polar_fit_sync.config import Settings
from polar_fit_sync.db import Db
from polar_fit_sync.polar import PolarClient
from polar_fit_sync.scheduler import build_scheduler
from polar_fit_sync.sync import run_sync
from polar_fit_sync.webhook import is_ping, parse_event, verify_signature

logger = logging.getLogger(__name__)


def create_app(settings: Settings) -> FastAPI:
    """Build and return a fully configured FastAPI application.

    The settings object determines which routes are active, which DB is used,
    and whether the scheduler is started at lifespan startup.
    """

    db = Db(settings.pfs_db_path)
    db.init_schema()

    client = PolarClient(
        client_id=settings.polar_client_id,
        client_secret=settings.polar_client_secret,
        redirect_uri=settings.polar_redirect_uri,
    )

    # Templates are located relative to this file so they work regardless of the
    # current working directory.
    import pathlib

    templates_dir = str(pathlib.Path(__file__).parent / "templates")
    templates = Jinja2Templates(directory=templates_dir)

    # Build the scheduler now so the lifespan can start/stop it.
    # _sync_runner is an async def so AsyncIOScheduler awaits it directly.
    # This surfaces exceptions through APScheduler's error handling rather than
    # silently swallowing them into orphan tasks. It also avoids the deprecated
    # asyncio.get_event_loop() pattern that raises a DeprecationWarning in
    # Python 3.12+ when called outside a running loop.
    async def _sync_runner():
        """Await run_sync inside APScheduler's async job dispatch."""
        await run_sync(
            db, client, settings.pfs_output_dir,
            trigger="poll",
            sport_filter=settings.sport_filter_set(),
            filter_mode=settings.pfs_sport_filter_mode,
        )

    scheduler = build_scheduler(settings, _sync_runner)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Start the scheduler (poll/both modes add an interval job; webhook mode
        # creates a scheduler with no jobs but still starts/stops cleanly).
        if settings.pfs_sync_mode in ("poll", "both"):
            scheduler.start()
            logger.info(
                "APScheduler started (mode=%s, interval=%dm).",
                settings.pfs_sync_mode,
                settings.pfs_sync_interval_minutes,
            )
        yield
        if settings.pfs_sync_mode in ("poll", "both") and scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("APScheduler stopped.")

    app = FastAPI(title="Polar FIT Sync", lifespan=lifespan)

    # -------------------------------------------------------------------------
    # Health probe
    # -------------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        return JSONResponse({"status": "ok"})

    # -------------------------------------------------------------------------
    # Status page
    # -------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        token = db.get_token()
        token_expired = False

        if token is not None:
            # Compute expiry from stored timestamps.
            if token.status == "token_expired":
                token_expired = True
            elif token.expires_in is not None:
                issued = datetime.fromisoformat(token.created_at.replace("Z", "+00:00"))
                if issued.tzinfo is None:
                    issued = issued.replace(tzinfo=timezone.utc)
                if (
                    datetime.now(timezone.utc) - issued
                ).total_seconds() > token.expires_in:
                    token_expired = True

        show_webhook_url = settings.pfs_sync_mode in ("webhook", "both")
        webhook_url = (
            f"{settings.pfs_base_url.rstrip('/')}/webhook/polar"
            if show_webhook_url and settings.pfs_base_url
            else "/webhook/polar"
        )

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "token": token,
                "token_expired": token_expired,
                "file_count": db.count_downloaded(),
                "last_run": db.last_run(),
                "sync_mode": settings.pfs_sync_mode,
                "show_webhook_url": show_webhook_url,
                "webhook_url": webhook_url,
            },
        )

    # -------------------------------------------------------------------------
    # OAuth flow
    # -------------------------------------------------------------------------

    @app.get("/oauth/start")
    async def oauth_start():
        state = secrets.token_urlsafe(32)
        db.create_state(state)
        url = client.authorize_url(state)
        return RedirectResponse(url=url, status_code=302)

    @app.get("/oauth/callback", response_class=HTMLResponse)
    async def oauth_callback(request: Request, code: str = "", state: str = ""):
        if not state or not db.consume_state(state):
            return HTMLResponse(
                "<h1>Bad Request</h1><p>Invalid or expired state parameter.</p>",
                status_code=400,
            )

        token_resp = client.exchange_code(code)
        client.register_user(token_resp.access_token, settings.pfs_member_id)

        db.set_token(
            access_token=token_resp.access_token,
            token_type=token_resp.token_type,
            x_user_id=token_resp.x_user_id,
            member_id=settings.pfs_member_id,
            expires_in=token_resp.expires_in,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        return templates.TemplateResponse(request, "connected.html")

    # -------------------------------------------------------------------------
    # Webhook endpoint
    # -------------------------------------------------------------------------

    @app.post("/webhook/polar")
    async def webhook_polar(request: Request):
        if settings.pfs_sync_mode not in ("webhook", "both"):
            return Response(status_code=404)

        raw = await request.body()
        signature = request.headers.get("Polar-Webhook-Signature")

        if not verify_signature(settings.pfs_webhook_secret, raw, signature):
            return Response(status_code=401)

        try:
            body = await request.json()
        except Exception:
            import json as _json

            body = _json.loads(raw)

        event = parse_event(body)

        if is_ping(event):
            return Response(status_code=200)

        # Trigger the sync asynchronously so we can return 200 immediately and
        # not block Polar's delivery timeout.
        asyncio.create_task(
            run_sync(
                db,
                client,
                settings.pfs_output_dir,
                target_id=event.entity_id,
                trigger="webhook",
                sport_filter=settings.sport_filter_set(),
                filter_mode=settings.pfs_sport_filter_mode,
            )
        )

        return Response(status_code=200)

    return app
