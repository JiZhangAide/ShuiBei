import os
import subprocess
import sys


def test_package_entrypoint_help_runs_in_clean_checkout():
    result = subprocess.run(
        [sys.executable, "-m", "audit_source", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "bot" in result.stdout
    assert "web" in result.stdout
    assert "all" in result.stdout


def test_bot_entrypoint_fails_only_for_missing_local_token():
    env = os.environ.copy()
    env.pop("SHUIBEI_BOT_TOKEN", None)
    env["SHUIBEI_ENV_FILE"] = os.devnull
    result = subprocess.run(
        [sys.executable, "-m", "audit_source", "bot"],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 78
    combined = result.stdout + result.stderr
    assert "Bot Token" in combined
    assert "private runtime" not in combined.lower()
    assert "entitlement" not in combined.lower()
