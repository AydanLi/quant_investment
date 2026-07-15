from datetime import datetime, timezone

import pytest

from scripts.import_brokerage_snapshot import validate_snapshot_payload
from storage.db import create_all, create_db_engine
from storage.repositories.brokerage_mirror import BrokerageMirrorRepository


def _repo():
    engine = create_db_engine("sqlite:///:memory:")
    create_all(engine)
    return BrokerageMirrorRepository(engine)


def test_saves_and_reads_latest_snapshot():
    repo = _repo()
    old = datetime(2026, 7, 14, tzinfo=timezone.utc)
    new = datetime(2026, 7, 15, tzinfo=timezone.utc)
    repo.save_snapshot(
        provider="robinhood",
        account_ref="0908",
        account_type="individual",
        captured_at=old,
        positions=[
            {
                "symbol": "SPY",
                "quantity": "1.0",
                "average_buy_price": "700",
                "shares_available_for_sells": "1.0",
                "type": "long",
            }
        ],
    )
    snapshot_id = repo.save_snapshot(
        provider="robinhood",
        account_ref="0908",
        account_type="individual",
        captured_at=new,
        positions=[
            {
                "symbol": "qqq",
                "quantity": "2.5",
                "average_buy_price": None,
                "shares_available_for_sells": "2.0",
                "type": "long",
            }
        ],
    )

    latest = repo.get_latest("robinhood", "0908")
    assert snapshot_id == 2
    assert latest["symbol"].tolist() == ["QQQ"]
    assert latest.iloc[0]["quantity"] == 2.5
    assert latest.iloc[0]["account_ref"] == "0908"


def test_rejects_duplicate_symbols():
    repo = _repo()
    row = {
        "symbol": "SPY",
        "quantity": 1,
        "shares_available_for_sells": 1,
    }
    with pytest.raises(ValueError, match="duplicate"):
        repo.save_snapshot(
            provider="robinhood",
            account_ref="0908",
            account_type="individual",
            positions=[row, row],
        )


def test_normalizes_masked_account_reference_to_last_four():
    repo = _repo()
    repo.save_snapshot(
        provider="Robinhood",
        account_ref="****-0908",
        account_type="Individual",
        positions=[],
    )

    latest = repo.get_latest("robinhood", "xxxx0908")
    assert latest.empty
    with repo.engine.connect() as connection:
        stored = connection.exec_driver_sql(
            "select provider, account_ref, account_type "
            "from brokerage_mirror_snapshots"
        ).one()
    assert tuple(stored) == ("robinhood", "0908", "individual")


@pytest.mark.parametrize(
    "account_ref",
    [
        "12345",
        "RH-SYNTHETIC-FULL-123456789",
        "12-34",
        "",
        None,
        True,
        908,
    ],
)
def test_rejects_unmasked_or_invalid_account_references(account_ref):
    repo = _repo()

    with pytest.raises(ValueError, match="account_ref"):
        repo.save_snapshot(
            provider="robinhood",
            account_ref=account_ref,
            account_type="individual",
            positions=[],
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", -1),
        ("quantity", float("nan")),
        ("quantity", float("inf")),
        ("quantity", True),
        ("average_buy_price", -10),
        ("shares_available_for_sells", -1),
    ],
)
def test_rejects_invalid_position_numbers(field, value):
    repo = _repo()
    position = {
        "symbol": "SPY",
        "quantity": 1,
        "average_buy_price": 500,
        "shares_available_for_sells": 1,
        "type": "long",
    }
    position[field] = value

    with pytest.raises(ValueError, match=field):
        repo.save_snapshot(
            provider="robinhood",
            account_ref="0908",
            account_type="individual",
            positions=[position],
        )


def test_rejects_sellable_shares_above_quantity_and_invalid_type():
    repo = _repo()
    position = {
        "symbol": "SPY",
        "quantity": 1,
        "shares_available_for_sells": 2,
    }
    with pytest.raises(ValueError, match="cannot exceed"):
        repo.save_snapshot(
            provider="robinhood",
            account_ref="0908",
            account_type="individual",
            positions=[position],
        )

    position["shares_available_for_sells"] = 1
    position["type"] = "unsupported"
    with pytest.raises(ValueError, match="type"):
        repo.save_snapshot(
            provider="robinhood",
            account_ref="0908",
            account_type="individual",
            positions=[position],
        )


def test_invalid_snapshot_is_atomic():
    repo = _repo()
    positions = [
        {"symbol": "SPY", "quantity": 1, "shares_available_for_sells": 1},
        {"symbol": "QQQ", "quantity": -1, "shares_available_for_sells": 0},
    ]

    with pytest.raises(ValueError, match="quantity"):
        repo.save_snapshot(
            provider="robinhood",
            account_ref="0908",
            account_type="individual",
            positions=positions,
        )

    with repo.engine.connect() as connection:
        count = connection.exec_driver_sql(
            "select count(*) from brokerage_mirror_snapshots"
        ).scalar_one()
    assert count == 0


@pytest.mark.parametrize(
    "sensitive_field",
    [
        "password",
        "access_token",
        "accessToken",
        "refresh-token",
        "refreshToken",
        "account_number",
        "accountNumber",
        "clientSecret",
    ],
)
def test_import_rejects_sensitive_fields_at_any_depth(sensitive_field):
    payload = {
        "provider": "robinhood",
        "account_ref": "0908",
        "account_type": "individual",
        "positions": [],
        "metadata": {sensitive_field: "must-not-be-imported"},
    }

    with pytest.raises(ValueError, match="Sensitive field"):
        validate_snapshot_payload(payload)


def test_import_requires_normalized_snapshot_shape():
    with pytest.raises(ValueError, match="must be an object"):
        validate_snapshot_payload([])
    with pytest.raises(ValueError, match="missing fields"):
        validate_snapshot_payload({"provider": "robinhood"})
    with pytest.raises(ValueError, match="positions must be an array"):
        validate_snapshot_payload(
            {
                "provider": "robinhood",
                "account_ref": "0908",
                "account_type": "individual",
                "positions": {},
            }
        )


def test_rejects_invalid_snapshot_metadata_and_symbols():
    repo = _repo()
    position = {
        "symbol": "SPY",
        "quantity": 1,
        "shares_available_for_sells": 1,
    }
    with pytest.raises(ValueError, match="provider"):
        repo.save_snapshot(
            provider="",
            account_ref="0908",
            account_type="individual",
            positions=[position],
        )
    with pytest.raises(ValueError, match="account_type"):
        repo.save_snapshot(
            provider="robinhood",
            account_ref="0908",
            account_type="",
            positions=[position],
        )
    with pytest.raises(ValueError, match="symbol"):
        repo.save_snapshot(
            provider="robinhood",
            account_ref="0908",
            account_type="individual",
            positions=[{**position, "symbol": None}],
        )
    with pytest.raises(ValueError, match="timezone"):
        repo.save_snapshot(
            provider="robinhood",
            account_ref="0908",
            account_type="individual",
            captured_at=datetime(2026, 7, 15),
            positions=[position],
        )
