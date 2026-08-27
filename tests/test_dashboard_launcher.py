from pathlib import Path


def test_launcher_restarts_managed_dashboard_when_source_state_changes():
    project_root = Path(__file__).resolve().parents[1]
    launcher = (project_root / "scripts" / "launch_dashboard.ps1").read_text(
        encoding="utf-8"
    )

    assert "Get-DashboardSourceState" in launcher
    assert "Get-FileHash" in launcher
    assert '"services"' in launcher
    assert '$pythonExe = Join-Path $projectRoot ".venv\\Scripts\\python.exe"' in launcher
    assert '$dashboardFile = "streamlit_dashboard_db_v1_1_save_experiment.py"' in launcher
    assert '"-m"' in launcher and '"streamlit"' in launcher
    assert "$sourceState -ne $storedSourceState.TrimEnd()" in launcher
    assert "Stop-ManagedDashboardProcess -Process $managedProcess" in launcher
    assert "Set-Content -LiteralPath $sourceStateFile" in launcher
