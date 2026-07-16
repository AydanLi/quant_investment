from pathlib import Path

from scripts.check_environment import load_exact_pins, validate_environment


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PACKAGES = {
    "alembic",
    "matplotlib",
    "numpy",
    "pandas",
    "sqlalchemy",
    "streamlit",
    "yfinance",
}


def test_requirement_layers_are_exact_and_covered_by_lock():
    runtime = load_exact_pins(PROJECT_ROOT / "requirements.txt")
    locked = load_exact_pins(PROJECT_ROOT / "constraints.lock")
    dev_lines = {
        line.strip()
        for line in (PROJECT_ROOT / "requirements-dev.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert set(runtime) == RUNTIME_PACKAGES
    assert dev_lines == {"-r requirements.txt", "pytest==9.1.1"}
    assert locked["pytest"] == "9.1.1"
    assert all(locked[name] == version for name, version in runtime.items())
    assert (PROJECT_ROOT / ".python-version").read_text(
        encoding="utf-8"
    ).strip() == "3.14.3"


def test_current_environment_matches_dependency_lock():
    assert validate_environment(PROJECT_ROOT) == []
