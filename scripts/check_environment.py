"""Verify that Python and installed dependencies match the validated lock."""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
import re
import sys

from packaging.markers import default_environment
from packaging.requirements import Requirement


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXACT_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")


def normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def load_exact_pins(path: Path) -> dict[str, str]:
    """Read a simple exact-pin file and reject ambiguous constraint syntax."""
    pins: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = EXACT_PIN.fullmatch(line)
        if match is None:
            raise ValueError(f"{path.name}:{line_number} is not an exact pin: {line}")
        name = normalize_package_name(match.group(1))
        if name in pins:
            raise ValueError(f"{path.name} contains duplicate package {name}")
        pins[name] = match.group(2)
    if not pins:
        raise ValueError(f"{path.name} contains no package pins")
    return pins


def load_project_roots(project_root: Path) -> set[str]:
    roots = set(load_exact_pins(project_root / "requirements.txt"))
    dev_path = project_root / "requirements-dev.txt"
    for line_number, raw_line in enumerate(
        dev_path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        match = EXACT_PIN.fullmatch(line)
        if match is None:
            raise ValueError(
                f"{dev_path.name}:{line_number} is not an exact pin: {line}"
            )
        roots.add(normalize_package_name(match.group(1)))
    return roots


def dependency_closure(
    roots: set[str],
    distributions: dict[str, metadata.Distribution],
) -> tuple[set[str], list[str]]:
    """Resolve installed required dependencies for the current platform."""
    environment = default_environment()
    environment["extra"] = ""
    pending = list(roots)
    resolved: set[str] = set()
    errors: list[str] = []
    while pending:
        name = pending.pop()
        if name in resolved:
            continue
        resolved.add(name)
        distribution = distributions.get(name)
        if distribution is None:
            errors.append(f"Cannot inspect missing root or dependency: {name}.")
            continue
        for raw_requirement in distribution.requires or ():
            requirement = Requirement(raw_requirement)
            if requirement.marker is not None and not requirement.marker.evaluate(
                environment
            ):
                continue
            dependency = normalize_package_name(requirement.name)
            if dependency not in resolved:
                pending.append(dependency)
    return resolved, errors


def validate_environment(project_root: Path = PROJECT_ROOT) -> list[str]:
    """Return actionable Python or package-version drift errors."""
    errors: list[str] = []
    expected_python = (project_root / ".python-version").read_text(
        encoding="utf-8"
    ).strip()
    actual_python = ".".join(str(value) for value in sys.version_info[:3])
    if actual_python != expected_python:
        errors.append(
            f"Python mismatch: expected {expected_python}, found {actual_python}."
        )

    locked = load_exact_pins(project_root / "constraints.lock")
    distributions = {
        normalize_package_name(distribution.metadata["Name"]): distribution
        for distribution in metadata.distributions()
        if distribution.metadata.get("Name")
    }
    installed = {name: item.version for name, item in distributions.items()}
    for name, expected_version in locked.items():
        actual_version = installed.get(name)
        if actual_version is None:
            errors.append(f"Missing package: {name}=={expected_version}.")
        elif actual_version != expected_version:
            errors.append(
                f"Package mismatch: {name} expected {expected_version}, "
                f"found {actual_version}."
            )

    closure, closure_errors = dependency_closure(
        load_project_roots(project_root),
        distributions,
    )
    errors.extend(closure_errors)
    unlocked = sorted(closure - set(locked))
    if unlocked:
        errors.append("Dependency lock is incomplete: " + ", ".join(unlocked) + ".")
    stale = sorted(set(locked) - closure)
    if stale:
        errors.append("Dependency lock has stale entries: " + ", ".join(stale) + ".")
    return errors


def main() -> int:
    errors = validate_environment()
    if errors:
        print("Environment does not match the validated lock:")
        for error in errors:
            print(f"- {error}")
        return 1

    locked_count = len(load_exact_pins(PROJECT_ROOT / "constraints.lock"))
    python_version = (PROJECT_ROOT / ".python-version").read_text(
        encoding="utf-8"
    ).strip()
    print(
        f"Environment matches Python {python_version} and "
        f"all {locked_count} locked packages."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
