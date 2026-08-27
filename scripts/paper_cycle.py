from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path

from config.settings import Config
from services.models import SignalDecision
from services.paper_cycle import PaperCycle
from storage.db import create_db_engine


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("Timestamp must include a UTC offset.")
    return parsed


def _json_file(value: str) -> dict[str, object]:
    return dict(json.loads(Path(value).read_text(encoding="utf-8")))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persistent local REPLAY_OPEN paper cycle")
    parser.add_argument("--db-url", default=Config().db_url)
    parser.add_argument("--account-ref", default="local-paper")
    parser.add_argument("--strategy-version", required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-account")
    init.add_argument("--cash", type=float, default=10_000.0)

    save = sub.add_parser("save-decision")
    save.add_argument("--decision", required=True)
    save.add_argument("--recorded-at", required=True, type=_datetime)

    draft = sub.add_parser("draft")
    draft.add_argument("--decision-id", required=True, type=int)
    draft.add_argument("--prices", required=True)
    draft.add_argument("--adv", required=True)
    draft.add_argument("--at", required=True, type=_datetime)

    approve = sub.add_parser("approve")
    approve.add_argument("--decision-id", required=True, type=int)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--at", required=True, type=_datetime)

    liquidation_approve = sub.add_parser("approve-liquidation")
    liquidation_approve.add_argument("--decision-id", required=True, type=int)
    liquidation_approve.add_argument("--approved-by", required=True)
    liquidation_approve.add_argument("--at", required=True, type=_datetime)

    liquidation_redraft = sub.add_parser("redraft-liquidation")
    liquidation_redraft.add_argument("--decision-id", required=True, type=int)
    liquidation_redraft.add_argument("--prices", required=True)
    liquidation_redraft.add_argument("--requested-by", required=True)
    liquidation_redraft.add_argument("--reason", required=True)
    liquidation_redraft.add_argument("--at", required=True, type=_datetime)

    fill = sub.add_parser("replay-open")
    fill.add_argument("--decision-id", required=True, type=int)
    fill.add_argument("--opens", required=True)
    fill.add_argument("--published-at", required=True, type=_datetime)

    liquidation_fill = sub.add_parser("replay-liquidation-open")
    liquidation_fill.add_argument("--decision-id", required=True, type=int)
    liquidation_fill.add_argument("--opens", required=True)
    liquidation_fill.add_argument("--published-at", required=True, type=_datetime)

    reconcile = sub.add_parser("reconcile-halt")
    reconcile.add_argument("--decision-id", required=True, type=int)
    reconcile.add_argument("--prices", required=True)
    reconcile.add_argument("--at", required=True, type=_datetime)

    recovery = sub.add_parser("authorize-recovery")
    recovery.add_argument("--reconciliation-id", required=True, type=int)
    recovery.add_argument("--authorized-by", required=True)
    recovery.add_argument("--note", required=True)
    recovery.add_argument("--at", required=True, type=_datetime)

    settle = sub.add_parser("settle")
    settle.add_argument("--session", required=True)
    settle.add_argument("--at", type=_datetime)

    sub.add_parser("retry-notifications")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = Config(db_url=args.db_url, strategy_version=args.strategy_version)
    cycle = PaperCycle(
        config,
        engine=create_db_engine(args.db_url),
        account_ref=args.account_ref,
    )
    if args.command == "init-account":
        result = cycle.initialize_account(
            strategy_version=args.strategy_version, initial_cash=args.cash
        )
    elif args.command == "save-decision":
        result = cycle.persist_decision(
            SignalDecision.from_dict(_json_file(args.decision)),
            recorded_at=args.recorded_at,
        )
    elif args.command == "draft":
        result = cycle.draft_orders(
            args.decision_id,
            reference_prices=_json_file(args.prices),
            median_daily_dollar_volume=_json_file(args.adv),
            verified_at=args.at,
        )
    elif args.command == "approve":
        result = cycle.approve(
            args.decision_id,
            approved_by=args.approved_by,
            approved_at=args.at,
        )
    elif args.command == "approve-liquidation":
        result = cycle.approve_liquidation(
            args.decision_id,
            approved_by=args.approved_by,
            approved_at=args.at,
        )
    elif args.command == "redraft-liquidation":
        result = cycle.redraft_liquidation(
            args.decision_id,
            reference_prices=_json_file(args.prices),
            requested_by=args.requested_by,
            reason=args.reason,
            requested_at=args.at,
        )
    elif args.command == "replay-open":
        result = cycle.materialize_open(
            args.decision_id,
            open_prices=_json_file(args.opens),
            published_at=args.published_at,
        )
    elif args.command == "replay-liquidation-open":
        result = cycle.materialize_liquidation_open(
            args.decision_id,
            open_prices=_json_file(args.opens),
            published_at=args.published_at,
        )
    elif args.command == "reconcile-halt":
        result = cycle.reconcile_halted_account(
            args.decision_id,
            prices=_json_file(args.prices),
            at=args.at,
        )
    elif args.command == "authorize-recovery":
        result = cycle.authorize_risk_recovery(
            reconciliation_id=args.reconciliation_id,
            authorized_by=args.authorized_by,
            note=args.note,
            at=args.at,
        )
    elif args.command == "settle":
        result = cycle.settle(session=args.session, at=args.at)
    else:
        result = cycle.flush_notifications()
    payload = asdict(result) if hasattr(result, "__dataclass_fields__") else result
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
