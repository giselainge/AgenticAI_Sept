"""Transactional SQLite persistence for provider-scoped invoice memory."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
_PROVIDER_LISTS = {
    "known_invoice_layouts": "known_invoice_layouts",
    "observed_invoices": "observed_invoices",
    "previously_validated_invoices": "validated_invoices",
    "human_reviewer_feedback": "reviewer_feedback",
    "validation_history": "validation_history",
}
_PROVIDER_COLUMNS = {
    "provider_id",
    "provider_name",
    "aliases",
    "provider_specific_extraction_tips",
    "common_ocr_corrections",
    "field_correction_patterns",
    *_PROVIDER_LISTS,
}


def is_sqlite_path(path: str | Path) -> bool:
    return Path(path).suffix.casefold() in SQLITE_SUFFIXES


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decoded(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _connect(path: str | Path) -> sqlite3.Connection:
    database = Path(path)
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS providers (
            provider_id TEXT PRIMARY KEY,
            provider_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL,
            extraction_tips_json TEXT NOT NULL,
            ocr_corrections_json TEXT NOT NULL,
            correction_patterns_json TEXT NOT NULL,
            extra_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS known_invoice_layouts (
            provider_id TEXT NOT NULL REFERENCES providers(provider_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            layout_id TEXT,
            invoice_type TEXT,
            fields_seen_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (provider_id, position)
        );
        CREATE TABLE IF NOT EXISTS observed_invoices (
            provider_id TEXT NOT NULL REFERENCES providers(provider_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            signature TEXT,
            source_file TEXT,
            invoice_type TEXT,
            review_status TEXT NOT NULL,
            observed_fields_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (provider_id, position)
        );
        CREATE TABLE IF NOT EXISTS validated_invoices (
            provider_id TEXT NOT NULL REFERENCES providers(provider_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            signature TEXT,
            source_file TEXT,
            valid_invoice TEXT,
            review_decision TEXT,
            validated_fields_json TEXT NOT NULL,
            reviewer_note TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (provider_id, position)
        );
        CREATE TABLE IF NOT EXISTS reviewer_feedback (
            provider_id TEXT NOT NULL REFERENCES providers(provider_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            field_name TEXT,
            old_value TEXT,
            corrected_value TEXT,
            note TEXT,
            recorded_at TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (provider_id, position)
        );
        CREATE TABLE IF NOT EXISTS validation_history (
            provider_id TEXT NOT NULL REFERENCES providers(provider_id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            event TEXT,
            source_file TEXT,
            recorded_at TEXT,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (provider_id, position)
        );
        CREATE INDEX IF NOT EXISTS idx_observed_signature
            ON observed_invoices(provider_id, signature);
        CREATE INDEX IF NOT EXISTS idx_validated_signature
            ON validated_invoices(provider_id, signature);
        CREATE INDEX IF NOT EXISTS idx_feedback_field
            ON reviewer_feedback(provider_id, field_name);
        """
    )
    return connection


def _payload_rows(connection: sqlite3.Connection, table: str, provider_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT payload_json FROM {table} WHERE provider_id = ? ORDER BY position",  # noqa: S608
        (provider_id,),
    ).fetchall()
    return [_decoded(row["payload_json"], {}) for row in rows]


def load_kb_database(path: str | Path) -> dict[str, Any]:
    with _connect(path) as connection:
        version_row = connection.execute("SELECT value FROM metadata WHERE key = 'kb_version'").fetchone()
        kb: dict[str, Any] = {
            "version": int(version_row["value"]) if version_row else 1,
            "providers": {},
        }
        for row in connection.execute("SELECT * FROM providers ORDER BY provider_id"):
            provider_id = row["provider_id"]
            provider = {
                "provider_id": provider_id,
                "provider_name": row["provider_name"],
                "aliases": _decoded(row["aliases_json"], []),
                "provider_specific_extraction_tips": _decoded(row["extraction_tips_json"], []),
                "common_ocr_corrections": _decoded(row["ocr_corrections_json"], {}),
                "field_correction_patterns": _decoded(row["correction_patterns_json"], {}),
                **_decoded(row["extra_json"], {}),
            }
            for key, table in _PROVIDER_LISTS.items():
                provider[key] = _payload_rows(connection, table, provider_id)
            kb["providers"][provider_id] = provider
        return kb


def _insert_payload_rows(
    connection: sqlite3.Connection,
    table: str,
    provider_id: str,
    items: list[dict[str, Any]],
) -> None:
    for position, item in enumerate(items):
        payload = _json(item)
        if table == "known_invoice_layouts":
            connection.execute(
                "INSERT INTO known_invoice_layouts VALUES (?, ?, ?, ?, ?, ?)",
                (
                    provider_id,
                    position,
                    item.get("layout_id"),
                    item.get("invoice_type"),
                    _json(item.get("fields_seen", [])),
                    payload,
                ),
            )
        elif table == "observed_invoices":
            connection.execute(
                "INSERT INTO observed_invoices VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    provider_id,
                    position,
                    item.get("signature"),
                    item.get("source_file"),
                    item.get("invoice_type"),
                    item.get("review_status", "unreviewed"),
                    _json(item.get("observed_fields", {})),
                    payload,
                ),
            )
        elif table == "validated_invoices":
            connection.execute(
                "INSERT INTO validated_invoices VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    provider_id,
                    position,
                    item.get("signature"),
                    item.get("source_file"),
                    item.get("valid_invoice"),
                    item.get("review_decision"),
                    _json(item.get("validated_fields", {})),
                    item.get("reviewer_note"),
                    payload,
                ),
            )
        elif table == "reviewer_feedback":
            connection.execute(
                "INSERT INTO reviewer_feedback VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    provider_id,
                    position,
                    item.get("field_name"),
                    item.get("old_value"),
                    item.get("corrected_value"),
                    item.get("note"),
                    item.get("recorded_at"),
                    payload,
                ),
            )
        elif table == "validation_history":
            connection.execute(
                "INSERT INTO validation_history VALUES (?, ?, ?, ?, ?, ?)",
                (
                    provider_id,
                    position,
                    item.get("event"),
                    item.get("source_file"),
                    item.get("recorded_at"),
                    payload,
                ),
            )


def save_kb_database(kb: dict[str, Any], path: str | Path) -> None:
    with _connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for table in (
            "known_invoice_layouts",
            "observed_invoices",
            "validated_invoices",
            "reviewer_feedback",
            "validation_history",
        ):
            connection.execute(f"DELETE FROM {table}")  # noqa: S608
        connection.execute("DELETE FROM providers")
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES ('kb_version', ?)",
            (str(kb.get("version", 1)),),
        )
        now = datetime.now(timezone.utc).isoformat()
        for provider_id, provider in sorted(kb.get("providers", {}).items()):
            extra = {key: value for key, value in provider.items() if key not in _PROVIDER_COLUMNS}
            connection.execute(
                """INSERT INTO providers(
                    provider_id, provider_name, aliases_json, extraction_tips_json,
                    ocr_corrections_json, correction_patterns_json, extra_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    provider_id,
                    provider.get("provider_name") or provider_id,
                    _json(provider.get("aliases", [])),
                    _json(provider.get("provider_specific_extraction_tips", [])),
                    _json(provider.get("common_ocr_corrections", {})),
                    _json(provider.get("field_correction_patterns", {})),
                    _json(extra),
                    now,
                ),
            )
            for key, table in _PROVIDER_LISTS.items():
                _insert_payload_rows(connection, table, provider_id, list(provider.get(key, [])))


def database_counts(path: str | Path) -> dict[str, int]:
    with _connect(path) as connection:
        tables = ["providers", *_PROVIDER_LISTS.values()]
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
            for table in tables
        }

