"""Content-addressed economic configuration shared by research and execution.

The manifest describes the frozen strategy, not a daily market-data snapshot.
Operational connection settings can change without changing trading policy.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from hashlib import sha256
from importlib.metadata import distributions
import json
from pathlib import Path
import platform
import subprocess
from typing import Mapping

from config.settings import Config


OPERATIONAL_FIELDS = frozenset({
    "db_url", "operating_mode", "broker_connectivity_enabled",
    "live_order_submission_enabled",
})
SOURCE_DIRECTORIES = (
    "backtest", "config", "data", "execution", "research", "risk",
    "services", "storage", "strategy", "utils", "report", "scripts",
)


def canonical_hash(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def config_payload(config: Config) -> dict[str, object]:
    """Keep every configuration field; only operational fields are unbound."""
    payload = asdict(config)
    return json.loads(json.dumps(payload, allow_nan=False))


def capture_code_identity(project_root: str | Path | None = None) -> dict[str, object]:
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
    files = [path for folder in SOURCE_DIRECTORIES
             for path in (root / folder).rglob("*.py")]
    files.extend(path for path in root.glob("requirements*.txt"))
    files.extend(path for path in root.glob("constraints*.lock"))
    files.extend(path for path in root.glob("*.py"))
    for name in ("pyproject.toml", "uv.lock", "poetry.lock"):
        if (root / name).is_file():
            files.append(root / name)
    digest = sha256()
    for path in sorted(set(files)):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    return {
        "code_commit": commit,
        "source_hash": digest.hexdigest(),
        "python": platform.python_version(),
        "dependencies": dict(sorted(
            (item.metadata["Name"].lower(), item.version)
            for item in distributions() if item.metadata["Name"]
        )),
    }


def assert_code_identity(expected: Mapping[str, object]) -> None:
    if dict(expected) != capture_code_identity():
        raise ValueError("Runtime code/dependency identity differs from the frozen research.")


@dataclass(frozen=True)
class FrozenRuntimeManifest:
    config: Mapping[str, object]
    code_identity: Mapping[str, object]
    research_cutoff: str
    dataset_snapshot_id: int
    protocol_hash: str = ""
    selected_label: str = ""
    parent_runtime_hash: str | None = None
    schema_version: int = 1

    def _payload(self) -> dict[str, object]:
        return json.loads(json.dumps(asdict(self), allow_nan=False))

    @property
    def runtime_hash(self) -> str:
        payload = self._payload()
        payload["config"] = {key: value for key, value in payload["config"].items()
                             if key not in OPERATIONAL_FIELDS}
        return canonical_hash(payload)

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "runtime_hash": self.runtime_hash}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "FrozenRuntimeManifest":
        payload = dict(value)
        expected_hash = payload.pop("runtime_hash", None)
        result = cls(**payload)
        if expected_hash != result.runtime_hash:
            raise ValueError("Frozen runtime manifest hash is missing or invalid.")
        result.to_config()
        if not result.research_cutoff or result.dataset_snapshot_id < 1:
            raise ValueError("Frozen runtime requires research cutoff and data identity.")
        return result

    def to_config(self, **operational_overrides: object) -> Config:
        if set(operational_overrides) - OPERATIONAL_FIELDS:
            raise ValueError("Frozen economic configuration cannot be overridden.")
        if set(self.config) != {field.name for field in fields(Config)}:
            raise ValueError("Frozen configuration is incomplete or uses a different schema.")
        payload = {**self.config, **{key: value for key, value in operational_overrides.items()
                                   if value is not None}}
        payload["universe"] = list(payload["universe"])
        payload["cost_scenarios_bps"] = tuple(payload["cost_scenarios_bps"])
        config = Config(**payload)
        config.validate_risk_constraints()
        return config


def build_runtime_manifest(
    config: Config, *, code_identity: Mapping[str, object], research_cutoff: str,
    dataset_snapshot_id: int, protocol_hash: str = "", selected_label: str = "",
    parent_runtime_hash: str | None = None,
) -> FrozenRuntimeManifest:
    config.validate_risk_constraints()
    return FrozenRuntimeManifest(
        config=config_payload(config), code_identity=dict(code_identity),
        research_cutoff=research_cutoff, dataset_snapshot_id=dataset_snapshot_id,
        protocol_hash=protocol_hash, selected_label=selected_label,
        parent_runtime_hash=parent_runtime_hash,
    )


def assert_runtime_matches(
    config: Config, manifest: FrozenRuntimeManifest, *, verify_code: bool = True,
) -> None:
    actual = config_payload(config)
    differences = [key for key in manifest.config if key not in OPERATIONAL_FIELDS
                   and actual.get(key) != manifest.config[key]]
    if differences:
        raise ValueError("Runtime configuration differs from frozen fields: " + ", ".join(differences))
    if verify_code:
        assert_code_identity(manifest.code_identity)
