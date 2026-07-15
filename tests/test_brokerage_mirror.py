from datetime import datetime, timezone

import pytest

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
