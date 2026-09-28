"""database/schema.sql must match the models (regenerate: python run.py export-schema)."""

from __future__ import annotations

import sqlalchemy as sa

from app.extensions import db
from app.utils.schema import (
    SCHEMA_PATH,
    generate_schema_sql,
    schema_is_current,
    split_sql_statements,
    table_statements,
)

SPEC_TABLES = {
    "users",
    "lost_reports",
    "case_counters",
    "sightings",
    "photos",
    "case_status_history",
    "scans",
    "scan_results",
    "image_comparisons",
    "conversations",
    "messages",
    "notifications",
    "device_tokens",
    "sms_log",
    "push_log",
    "phone_verifications",
    "assistance_providers",
    "rescue_requests",
    "sighting_access_log",
    "abuse_reports",
    "audit_logs",
    "jobs",
}


def test_schema_sql_is_up_to_date(app):
    assert SCHEMA_PATH.exists(), "Run: python run.py export-schema"
    assert (
        schema_is_current()
    ), "database/schema.sql is out of date with app/models. Run: python run.py export-schema"


def test_all_spec_tables_exist(app):
    assert SPEC_TABLES <= set(db.metadata.tables)
    tables = set(sa.inspect(db.engine).get_table_names())
    assert SPEC_TABLES <= tables


def test_schema_sql_shape(app):
    sql = generate_schema_sql()
    assert "CREATE DATABASE IF NOT EXISTS pawbalik" in sql
    assert "USE pawbalik;" in sql
    creates = table_statements(sql)
    assert len(creates) == len(db.metadata.tables)
    for statement in creates:
        assert statement.startswith("CREATE TABLE IF NOT EXISTS")
        assert statement.endswith(
            "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
        )
    assert (
        "CREATE INDEX" not in sql
    )  # indexes are inline (MySQL 8 has no CREATE INDEX IF NOT EXISTS)
    assert len(split_sql_statements(sql)) == len(creates) + 2


def test_schema_order_is_fk_safe(app):
    created: list[str] = []
    for statement in table_statements(generate_schema_sql()):
        name = statement.split()[5]
        for ref in __import__("re").findall(r"REFERENCES (\w+)", statement):
            assert ref in created or ref == name, f"{name} references {ref} before it exists"
        created.append(name)


def test_required_indexes(app):
    sql = generate_schema_sql()
    for index in (
        "ix_lost_reports_status",
        "ix_lost_reports_species",
        "ix_lost_reports_last_seen_lat_lng",
        "ix_sightings_status",
        "ix_sightings_species",
        "ix_sightings_seen_lat_lng",
        "ix_assistance_providers_lat_lng",
        "ix_photos_owner",
    ):
        assert index in sql, index
    assert "uq_conversations_lost_report_id_other_user_id" in sql
    assert "uq_image_comparisons_query_sha256_candidate_photo_id_model_name" in sql
