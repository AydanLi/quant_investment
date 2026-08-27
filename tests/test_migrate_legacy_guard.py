from __future__ import annotations

import pytest

from scripts.migrate_legacy_to_v2 import migrate


def test_legacy_migration_refuses_in_place_database(tmp_path):
    database = tmp_path / "database.db"
    with pytest.raises(ValueError, match="must be different"):
        migrate(database, database)

