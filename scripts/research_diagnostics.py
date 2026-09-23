"""Offline factor ablations and measured resource use on one immutable snapshot."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
from statistics import median
from time import perf_counter
import tracemalloc

from config.settings import Config
from data.features import FeatureEngineer
from research.core_evaluator import evaluate_config_path
from scripts.run_core_admission import load_immutable_snapshot, _jsonable
from storage.db import create_db_engine
from storage.repositories.governance import GovernanceRepository


def ablation_configs(config: Config) -> dict[str, Config]:
    """Remove one contribution while preserving the remaining score scale."""
    result = {"baseline": config}
    weight_names = ("weight_mom_20", "weight_mom_60", "weight_mom_120", "weight_low_vol")
    for removed in weight_names:
        weights = {name: getattr(config, name) if name != removed else 0.0 for name in weight_names}
        total = sum(weights.values())
        if getattr(config, removed) == 0.0 or total <= 0.0:
            continue
        result[f"without_{removed}"] = replace(config, **{name: value / total for name, value in weights.items()})
    # Positive prices have price/MA200 - 1 > -1. VIX's two finite thresholds
    # are raised together, avoiding infinity in governed JSON/config payloads.
    result["without_ma200_deviation_filter"] = replace(config, max_allowed_drawdown_from_200d=-1.0)
    result["without_vix_filter"] = replace(config, vix_high_threshold=1_000_000.0,
                                            vix_risk_off_threshold=1_000_001.0)
    return result


def run_diagnostics(config, data, *, evaluation_start, repeats=3, evaluator=evaluate_config_path):
    import pandas as pd

    if repeats < 1:
        raise ValueError("Performance measurements require at least one repetition.")
    start = pd.Timestamp(evaluation_start)
    training = {ticker: frame.loc[frame.index < start] for ticker, frame in data.items()}
    validation = {ticker: frame.loc[frame.index >= start] for ticker, frame in data.items()}
    if not any(not frame.empty for frame in training.values()) or not any(not frame.empty for frame in validation.values()):
        raise ValueError("Diagnostics require both feature history and an evaluation interval.")
    feature_times = []
    for _ in range(repeats):
        began = perf_counter()
        engineer = FeatureEngineer(data, config)
        prices = engineer.make_price_frame()
        returns = engineer.make_returns_frame(prices)
        engineer.compute_features(prices, returns)
        feature_times.append(perf_counter() - began)
    ablations = {}
    for label, candidate in ablation_configs(config).items():
        tracemalloc.start()
        began = perf_counter()
        try:
            result = evaluator(candidate, training, validation)
            elapsed = perf_counter() - began
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        ablations[label] = {"metrics": asdict(result["metrics"]),
                            "seconds": elapsed, "peak_python_bytes": peak_bytes}
    return {"diagnostic_only": True, "admitted": False,
            "interpretation": "Conditional historical ablations; no independent performance or causal claim.",
            "ma200_deviation_definition": "price / 200-session moving average - 1; not peak-to-trough drawdown",
            "evaluation_start": str(start.date()),
            "universe": list(config.universe), "input_assets": len(data),
            "input_rows": sum(len(frame) for frame in data.values()),
            "feature_timing_seconds": {"repetitions": repeats, "median": median(feature_times), "samples": feature_times},
            "measurement_limits": "Wall time is local; tracemalloc covers Python allocations, not all native-library/RSS memory.",
            "ablations": ablations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="quant_research.db")
    parser.add_argument("--snapshot-id", required=True, type=int)
    parser.add_argument("--strategy-version", required=True)
    parser.add_argument("--evaluation-start", required=True)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    database = args.database if "://" in args.database else f"sqlite:///{Path(args.database).as_posix()}"
    manifest = GovernanceRepository(engine=create_db_engine(database)).load_frozen_runtime(args.strategy_version)
    output = run_diagnostics(manifest.to_config(), load_immutable_snapshot(args.database, args.snapshot_id),
                             evaluation_start=args.evaluation_start, repeats=args.repeats)
    output.update(runtime_hash=manifest.runtime_hash, dataset_snapshot_id=args.snapshot_id)
    print(json.dumps(_jsonable(output), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
