from pathlib import Path


def test_mirror_launcher_restarts_only_managed_process_when_source_changes():
    project_root = Path(__file__).resolve().parents[1]
    launcher = (project_root / "scripts" / "launch_robinhood_mirror.ps1").read_text(
        encoding="utf-8"
    )

    assert "Get-MirrorSourceState" in launcher
    assert "Get-FileHash" in launcher
    assert '"services"' in launcher
    assert '"scripts\\optimize_mirrored_portfolio.py"' in launcher
    assert "$sourceState -ne $storedSourceState.TrimEnd()" in launcher
    assert "Stop-ManagedMirrorProcess -Process $managedProcess" in launcher
    assert "Set-Content -LiteralPath $sourceStateFile" in launcher
    assert "$process.StartTime -gt $pidWrittenAt.AddSeconds(5)" in launcher
    assert "healthy service that is not managed by this launcher" in launcher
    assert "Port $Port is already in use by another application" in launcher
