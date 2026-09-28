"""Flask extension singletons.

Import these from anywhere (``from app.extensions import db, limiter``); they
are bound to the application inside ``create_app()``.
"""

from __future__ import annotations

from collections.abc import Callable

from flask import current_app, request
from flask_limiter import Limiter
from flask_login import LoginManager, current_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Deterministic index/constraint names, so database/schema.sql is stable and
# identical on SQLite, MariaDB 10.4 and MySQL 8.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


db = SQLAlchemy(model_class=Base)
login_manager = LoginManager()
csrf = CSRFProtect()


def rate_limit_key() -> str:
    """Rate-limit per logged-in user, otherwise per client IP."""
    try:
        if current_user.is_authenticated:
            return f"user:{current_user.get_id()}"
    except Exception:  # noqa: BLE001 - outside a request / no login manager
        pass
    return f"ip:{request.remote_addr or 'unknown'}"


limiter = Limiter(key_func=rate_limit_key)


def config_limit(key: str) -> Callable[[], str]:
    """Return a callable that reads a rate-limit string from config at request time.

    Usage::

        @bp.post("/api/scans")
        @limiter.limit(config_limit("RATE_LIMIT_SCANS"))
        def api_create(): ...
    """

    def _limit() -> str:
        return current_app.config[key]

    return _limit
