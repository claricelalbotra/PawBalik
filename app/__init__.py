"""PawBalik application factory.

``create_app()`` loads the config, binds the extensions, registers one
blueprint per file in ``app/routes/``, security headers, Jinja filters,
template context and the Flask CLI commands (mirrors of ``python run.py``).
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

import click
import sqlalchemy as sa
from flask import Flask, flash, redirect, request, url_for
from flask_login import current_user
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_config
from app.extensions import csrf, db, limiter, login_manager

log = logging.getLogger(__name__)

# One Flask Blueprint per file; each module exposes ``bp`` (page routes + its /api routes).
ROUTE_MODULES: tuple[str, ...] = (
    "main",
    "auth",
    "pets",
    "cases",
    "scanner",
    "sightings",
    "messages",
    "rescue",
    "notifications",
    "admin",
)

# request.blueprint -> header nav item that is highlighted (see base.html).
NAV_BY_BLUEPRINT = {
    "pets": "report",
    "cases": "cases",
    "scanner": "match",
    "sightings": "match",
    "rescue": "rescue",
    "messages": "messages",
    "notifications": "notifications",
    "admin": "admin",
}
NAV_BY_ENDPOINT = {
    "main.index": "home",
    "main.about": "about",
    "main.privacy": "about",
    "auth.profile": "account",
    "cases.detail": "report",
    "cases.by_number": "report",
}


def create_app(config_name: str | None = None, **overrides: Any) -> Flask:
    """Build the Flask app. ``overrides`` replace config keys (used by tests)."""
    app = Flask(__name__)
    config_class = get_config(config_name)
    app.config.from_object(config_class)
    app.config.update(overrides)
    config_class.init_app(app)

    _init_logging(app)
    _init_extensions(app)
    _register_blueprints(app)
    _init_templates(app)

    from app.utils.security import init_security

    init_security(app)
    register_cli(app)

    # Test providers write here (see sms_service.MemorySmsProvider and the conftest fixtures).
    app.extensions.setdefault("sms_outbox", [])
    app.extensions.setdefault("push_outbox", [])
    return app


def _init_logging(app: Flask) -> None:
    if not app.debug and not app.testing:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.session_protection = "basic"

    from app.models import User
    from app.utils.http import json_error, wants_json

    @login_manager.user_loader
    def load_user(user_id: str) -> User | None:
        try:
            user = db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None
        # Deactivated users are logged out on their next request.
        return user if user is not None and user.is_active else None

    @login_manager.unauthorized_handler
    def unauthorized():
        if wants_json():
            return json_error("auth_required", "Please log in to continue.", 401)
        flash("Please log in to continue.", "info")
        next_url = request.full_path if request.query_string else request.path
        return redirect(url_for("auth.login", next=next_url))


def _register_blueprints(app: Flask) -> None:
    for name in ROUTE_MODULES:
        module = importlib.import_module(f"app.routes.{name}")
        app.register_blueprint(module.bp)


# --- Templates ------------------------------------------------------------------------------


def _public_config(app: Flask) -> dict[str, Any]:
    """Non-secret settings the browser needs (rendered as JSON in base.html)."""
    cfg = app.config
    firebase = dict(cfg.get("FIREBASE_WEB_CONFIG") or {})
    firebase_enabled = bool(
        firebase.get("apiKey") and firebase.get("projectId") and cfg.get("FIREBASE_VAPID_KEY")
    )
    return {
        "appName": cfg["APP_NAME"],
        "mapPicker": cfg["MAP_PICKER"],
        "bounds": cfg["SERVICE_AREA_BOUNDS"],
        "center": cfg["SERVICE_AREA_CENTER"],
        "zoom": cfg["MAP_DEFAULT_ZOOM"],
        "serviceAreaName": cfg["SERVICE_AREA_NAME"],
        "outOfAreaPolicy": cfg["OUT_OF_AREA_POLICY"],
        "leaflet": {
            "js": cfg["LEAFLET_JS_URL"],
            "jsSri": cfg["LEAFLET_JS_SRI"],
            "css": cfg["LEAFLET_CSS_URL"],
            "cssSri": cfg["LEAFLET_CSS_SRI"],
            "tiles": cfg["OSM_TILE_URL"],
            "attribution": cfg["OSM_ATTRIBUTION"],
        },
        "upload": {
            "maxBytes": cfg["MAX_UPLOAD_BYTES"],
            "maxEdge": cfg["CLIENT_COMPRESS_MAX_EDGE"],
            "maxPhotosPerReport": cfg["MAX_PHOTOS_PER_REPORT"],
            "maxPhotosPerSighting": cfg["MAX_PHOTOS_PER_SIGHTING"],
        },
        "messagePollSeconds": cfg["MESSAGE_POLL_SECONDS"],
        "firebase": {
            "enabled": firebase_enabled,
            "config": firebase if firebase_enabled else None,
            "vapidKey": cfg.get("FIREBASE_VAPID_KEY") if firebase_enabled else None,
            "jsVersion": cfg.get("FIREBASE_JS_VERSION"),
        },
    }


def _unread_counts() -> dict[str, int]:
    """Unread notifications and messages for the header badges (0 on any DB problem)."""
    counts = {"notifications": 0, "messages": 0}
    if not current_user.is_authenticated:
        return counts
    from app.models import Conversation, Message, Notification

    uid = current_user.id
    try:
        counts["notifications"] = (
            db.session.scalar(
                sa.select(sa.func.count(Notification.id)).where(
                    Notification.user_id == uid, Notification.is_read.is_(False)
                )
            )
            or 0
        )
        counts["messages"] = (
            db.session.scalar(
                sa.select(sa.func.count(Message.id))
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(
                    sa.or_(Conversation.owner_id == uid, Conversation.other_user_id == uid),
                    Message.read_at.is_(None),
                    sa.or_(Message.sender_id.is_(None), Message.sender_id != uid),
                )
            )
            or 0
        )
    except SQLAlchemyError:
        log.debug("Unread counts unavailable", exc_info=True)
    return counts


def _init_templates(app: Flask) -> None:
    from app.utils import strings
    from app.utils.filters import register_filters

    register_filters(app)
    # Globals are visible inside imported macros too (context-processor values are not).
    app.jinja_env.globals.update(S=strings, public_config=_public_config(app))

    @app.context_processor
    def inject_globals() -> dict[str, Any]:
        from app.utils.time import local_now

        endpoint = request.endpoint or ""
        nav_active = NAV_BY_ENDPOINT.get(endpoint) or NAV_BY_BLUEPRINT.get(
            request.blueprint or "", ""
        )
        return {
            "nav_active": nav_active,
            "unread_counts": _unread_counts(),
            "current_year": local_now().year,
        }


# --- CLI (mirrors `python run.py <command>`) ----------------------------------------------------


def cmd_init_db() -> int:
    """Create the tables (MySQL/MariaDB: run database/schema.sql; SQLite: create_all)."""
    from app.utils.schema import friendly_db_error, init_db

    try:
        click.echo(init_db())
    except SQLAlchemyError as exc:
        click.echo(friendly_db_error(exc), err=True)
        return 1
    return 0


def cmd_export_schema() -> int:
    from app.utils.schema import write_schema_sql

    path = write_schema_sql()
    click.echo(f"Wrote {path}")
    return 0


def cmd_seed(reset_demo: bool = False) -> int:
    from app.utils.schema import friendly_db_error

    try:
        from database.seed import run_seed
    except ImportError as exc:  # pragma: no cover - run from the project root
        click.echo(
            f"Cannot import database/seed.py ({exc}). Run from the project folder.", err=True
        )
        return 1
    try:
        summary = run_seed(reset_demo=reset_demo)
    except SQLAlchemyError as exc:
        db.session.rollback()
        click.echo(friendly_db_error(exc), err=True)
        return 1
    click.echo(f"Seed finished: {summary}")
    return 0


def cmd_worker(once: bool = False) -> int:
    from app.services.job_service import run_worker
    from app.utils.schema import friendly_db_error

    click.echo("PawBalik worker running" + (" once" if once else " (Ctrl+C to stop)") + "...")
    try:
        run_worker(once=once)
    except SQLAlchemyError as exc:
        click.echo(friendly_db_error(exc), err=True)
        return 1
    return 0


def cmd_match_test(
    photo: str, mode: str = "found", lat: float | None = None, lng: float | None = None
) -> int:
    from app.services.matching_service import cli_match_test

    return int(cli_match_test(photo, mode, lat, lng) or 0)


def cmd_eval_matching(folder: str) -> int:
    from app.services.matching_service import evaluate_folder

    click.echo(evaluate_folder(folder))
    return 0


def register_cli(app: Flask) -> None:
    """``flask --app run <command>`` equivalents of ``python run.py <command>``."""

    def _exit(code: int) -> None:
        if code:
            raise SystemExit(code)

    @app.cli.command("init-db")
    def init_db_command() -> None:
        """Create the database tables."""
        _exit(cmd_init_db())

    @app.cli.command("export-schema")
    def export_schema_command() -> None:
        """Regenerate database/schema.sql from the models."""
        _exit(cmd_export_schema())

    @app.cli.command("seed")
    @click.option("--reset-demo", is_flag=True, help="Delete and recreate the demo data.")
    def seed_command(reset_demo: bool) -> None:
        """Create the admin, SAMPLE providers and demo data."""
        _exit(cmd_seed(reset_demo))

    @app.cli.command("worker")
    @click.option("--once", is_flag=True, help="Run due jobs once and exit.")
    def worker_command(once: bool) -> None:
        """Run the background job worker."""
        _exit(cmd_worker(once))

    @app.cli.command("match-test")
    @click.argument("photo")
    @click.option("--mode", type=click.Choice(["found", "lost"]), default="found")
    @click.option("--lat", type=float, default=None)
    @click.option("--lng", type=float, default=None)
    def match_test_command(photo: str, mode: str, lat: float | None, lng: float | None) -> None:
        """Try the matching engine on one photo."""
        _exit(cmd_match_test(photo, mode, lat, lng))

    @app.cli.command("eval-matching")
    @click.argument("folder")
    def eval_matching_command(folder: str) -> None:
        """Evaluate similarity scores on labelled photo pairs."""
        _exit(cmd_eval_matching(folder))
