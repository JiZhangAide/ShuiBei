from pathlib import Path
import sys
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "audit_source"
if str(AUDIT) not in sys.path:
    sys.path.insert(0, str(AUDIT))

import app as bot_app
import public_runtime


def test_public_runtime_cli_dispatches_modes():
    with patch.object(public_runtime, "run_bot") as run_bot:
        public_runtime.main(["bot"])
        run_bot.assert_called_once_with()

    with patch.object(public_runtime, "run_web") as run_web:
        public_runtime.main(["web"])
        run_web.assert_called_once_with()

    with patch.object(public_runtime, "run_all") as run_all:
        public_runtime.main(["all"])
        run_all.assert_called_once_with()


def test_public_runtime_creates_miniapp_web_app():
    response = public_runtime.create_web_app().test_client().get(
        "/ShuiBei/api/miniapp/v1/health"
    )
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "data": {"status": "ok"}}


def test_open_source_bot_run_needs_no_private_runtime():
    class FakeTelegramAPI:
        calls = 0

        def get_me(self):
            return {"id": 123456, "username": "self_hosted_bot"}

        def get_webhook_info(self):
            return {"url": ""}

        def get_updates(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return []
            raise KeyboardInterrupt

    with patch.object(bot_app, "TelegramAPI", return_value=FakeTelegramAPI()), \
         patch.object(bot_app, "BOT_USERNAME", ""), \
         patch.object(bot_app, "init_all"), \
         patch.object(bot_app, "ensure_feature_schema"), \
         patch.object(bot_app, "bootstrap_customer_index"), \
         patch.object(bot_app, "load_offset", return_value=0), \
         patch.object(bot_app, "save_offset"), \
         patch.object(bot_app, "_runtime_clear_ready"), \
         patch.object(bot_app, "_runtime_status"), \
         patch.object(bot_app, "apply_access_control", side_effect=lambda _api, updates: updates):
        with pytest.raises(KeyboardInterrupt):
            bot_app.run()


def test_public_app_does_not_import_private_runtime_gate():
    text = (AUDIT / "app.py").read_text(encoding="utf-8")
    assert "require_moqing_runtime" not in text
    assert "from moqing_gateway import" not in text
    assert not (AUDIT / "runtime_guard.py").exists()
    assert not (AUDIT / "moqing_gateway.py").exists()


def test_miniapp_button_hidden_without_https_url():
    with patch.object(bot_app, "_SHUIBEI_MINIAPP_URL", ""):
        assert bot_app._r31_webapp_button() is None

    with patch.object(bot_app, "_SHUIBEI_MINIAPP_URL", "http://127.0.0.1:8080/ShuiBei/app"):
        assert bot_app._r31_webapp_button() is None

    with patch.object(bot_app, "_SHUIBEI_MINIAPP_URL", "https://example.test/ShuiBei/app"):
        button = bot_app._r31_webapp_button()
        assert button["web_app"]["url"] == "https://example.test/ShuiBei/app"
