import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_local_env_loads_developer_api_key():
    with tempfile.TemporaryDirectory() as td:
        env_file = Path(td) / ".env"
        env_file.write_text(
            "SHUIBEI_DEVELOPER_API_KEY=test_public_key\n"
            "SHUIBEI_BOT_TOKEN=test_bot_token\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.pop("SHUIBEI_DEVELOPER_API_KEY", None)
        env.pop("SHUIBEI_BOT_TOKEN", None)
        env["SHUIBEI_ENV_FILE"] = str(env_file)
        code = (
            "from audit_source import config;"
            "print(config.DEVELOPER_API_KEY);"
            "print(config.BOT_TOKEN)"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout.splitlines() == ["test_public_key", "test_bot_token"]
