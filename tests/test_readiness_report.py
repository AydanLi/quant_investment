"""Readiness reporting must inspect evidence without manufacturing it."""
import sqlite3
from scripts.check_readiness import build_readiness_report


def test_empty_legacy_database_remains_blocked_and_unchanged(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_marker (value TEXT)")
        connection.execute("INSERT INTO legacy_marker VALUES ('keep')")
    before = path.read_bytes()
    report = build_readiness_report(path)
    assert report["ready_for_live"] is False
    assert "SCHEMA_MIGRATION_REQUIRED" in report["blockers"]
    assert "BROKER_EVIDENCE_REQUIRED" in report["blockers"]
    assert path.read_bytes() == before
