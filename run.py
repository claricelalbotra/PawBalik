"""PawBalik - the single entry point.

    python run.py                          start the development server
    python run.py --port 5001              ... on another port (also --host, --no-debug)
    python run.py init-db                  create the tables (MySQL/MariaDB: database/schema.sql)
    python run.py export-schema            regenerate database/schema.sql from the models
    python run.py seed [--reset-demo]      admin account, SAMPLE providers, demo data
    python run.py worker [--once]          background jobs (matching, notifications, expiry)
    python run.py match-test <photo> [--mode found|lost] [--lat LAT --lng LNG]
    python run.py eval-matching <folder>   average scores on labelled photo pairs

Settings come from .env (see .env.example). Use ``--config testing|production``
or the PAWBALIK_CONFIG variable to pick another configuration.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:  # so `import app` and `import database.seed` work from anywhere
    sys.path.insert(0, str(ROOT))

from app import (  # noqa: E402
    cmd_eval_matching,
    cmd_export_schema,
    cmd_init_db,
    cmd_match_test,
    cmd_seed,
    cmd_worker,
    create_app,
)

# `flask --app run <command>` finds this object.
app = create_app()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python run.py",
        description="PawBalik: lost & found dog/cat matching for Tacloban City.",
    )
    parser.add_argument("--host", help="dev server host (default from HOST or 127.0.0.1)")
    parser.add_argument("--port", type=int, help="dev server port (default from PORT or 5000)")
    parser.add_argument("--no-debug", action="store_true", help="dev server without debug mode")
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("init-db", help="create the database tables")
    sub.add_parser("export-schema", help="regenerate database/schema.sql from the models")

    seed = sub.add_parser("seed", help="create the admin account and sample/demo data")
    seed.add_argument("--reset-demo", action="store_true", help="delete and recreate demo data")

    worker = sub.add_parser("worker", help="run the background job worker")
    worker.add_argument("--once", action="store_true", help="run due jobs once and exit")

    match = sub.add_parser("match-test", help="try the matching engine on one photo")
    match.add_argument("photo", help="path to a dog/cat photo")
    match.add_argument("--mode", choices=["found", "lost"], default="found")
    match.add_argument("--lat", type=float)
    match.add_argument("--lng", type=float)

    evaluate = sub.add_parser("eval-matching", help="evaluate similarity on labelled photo pairs")
    evaluate.add_argument("folder", help="folder with same/ and different/ photo pairs")
    return parser


def run_server(args: argparse.Namespace) -> int:
    host = args.host or app.config["RUN_HOST"]
    port = args.port or app.config["RUN_PORT"]
    debug = bool(app.config.get("DEBUG")) and not args.no_debug
    app.run(host=host, port=port, debug=debug)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command is None:
        return run_server(args)
    with app.app_context():
        if args.command == "init-db":
            return cmd_init_db()
        if args.command == "export-schema":
            return cmd_export_schema()
        if args.command == "seed":
            return cmd_seed(reset_demo=args.reset_demo)
        if args.command == "worker":
            return cmd_worker(once=args.once)
        if args.command == "match-test":
            return cmd_match_test(args.photo, args.mode, args.lat, args.lng)
        if args.command == "eval-matching":
            return cmd_eval_matching(args.folder)
    return 1  # pragma: no cover - argparse rejects unknown commands


if __name__ == "__main__":
    sys.exit(main())
