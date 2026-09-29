# -*- coding: utf-8 -*-
"""Public/source-available ShuiBei configuration."""
from __future__ import annotations

import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]


def _load_local_env() -> None:
    """Load repository-local .env without adding a python-dotenv dependency."""
    env_path = Path(os.environ.get("SHUIBEI_ENV_FILE") or (SOURCE_ROOT / ".env"))
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw[7:].strip()
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def _admin_ids() -> tuple[int, ...]:
    out: list[int] = []
    for raw in str(os.environ.get("SHUIBEI_ADMIN_IDS") or "").split(","):
        raw = raw.strip()
        if raw.isdigit():
            out.append(int(raw))
    return tuple(out)


_load_local_env()

BASE_DIR = Path(
    os.environ.get("SHUIBEI_DATA_DIR")
    or os.environ.get("SHUIBEI_AUDIT_STATE_DIR")
    or (SOURCE_ROOT / "runtime-data")
).resolve()

BOT_TOKEN = str(os.environ.get("SHUIBEI_BOT_TOKEN") or "").strip()
BOT_USERNAME = str(os.environ.get("SHUIBEI_BOT_USERNAME") or "@ShuiBei_bot").strip()
BOT_DISPLAY_NAME = "水杯记账"

LEDGER_DB_PATH = BASE_DIR / "ledger.db"
APP_DB_PATH = BASE_DIR / "shuibei.db"
ARCHIVE_DB_PATH = BASE_DIR / "archive.db"
SYNC_DB_PATH = BASE_DIR / "sync.db"
MESSAGE_PROTECTION_KEY_PATH = BASE_DIR / ".message_protection_key"
MESSAGE_PROTECTION_TTL_SECONDS = 7 * 86400
CHANNEL_VERIFIER_TOKEN_PATH = BASE_DIR / ".channel_verifier_token"
MAIN_LEDGER_DB_PATH = BASE_DIR / ".moqing-private-main-ledger-unavailable"

REQUIRED_CHANNEL = "@jizhangaide"
REQUIRED_CHANNEL_URL = "https://t.me/jizhangaide"
MEMBERSHIP_FALLBACK_BOT = "@jizhangaide_bot"
MEMBERSHIP_TRIAL_DAYS = 30
MEMBERSHIP_REFERRAL_TTL_SECONDS = 3 * 86400
MEMBERSHIP_REMINDER_SECONDS = 2 * 86400
MEMBERSHIP_7D_USDT = "3"
MEMBERSHIP_30D_USDT = "6.66"
SHUIBEI_ADMIN_USER_IDS: tuple[int, ...] = _admin_ids()

# Public MoQing Developer API used only by the source-available build.
DEVELOPER_API_BASE = "https://api.jizhang.org"
DEVELOPER_SCAM_API_PATH = "/api/v1/fanzha/records"
DEVELOPER_FAKEBOT_API_PATH = "/api/v1/fakebot/check"
DEVELOPER_API_KEY = str(os.environ.get("SHUIBEI_DEVELOPER_API_KEY") or "").strip()

TRONGRID_API_KEY = str(
    os.environ.get("SHUIBEI_TRONGRID_API_KEY")
    or os.environ.get("TRONGRID_API_KEY")
    or ""
).strip()
TRONGRID_TRC20_API = "https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
TRONGRID_TX_API = "https://api.trongrid.io/v1/accounts/{address}/transactions"
TRONGRID_ACCOUNT_API = "https://api.trongrid.io/v1/accounts/{address}"
TRC20_USDT_CONTRACT = "TXLAQ63Xg1NAzckPwKHvzw7CSEmLMEqcdj"
TONAPI_BASE = "https://tonapi.io/v2"
HEXARATE_RATE_API = "https://hexarate.paikama.co/api/rates/{base}/{target}/latest"
COINGECKO_SIMPLE_PRICE_API = "https://api.coingecko.com/api/v3/simple/price"

HTTP_TIMEOUT = 12
POLL_TIMEOUT = 30
POLL_RETRY_SECONDS = 2
SCALE = 10000
MAX_DECIMALS = 4
DEFAULT_CURRENCY = "USDT"
ALLOWED_CURRENCIES = ("USDT", "USDC", "TRX", "CNY", "TON", "PEP")
SYNC_DEFAULT_ENABLED = 0
RISK_ALERT_COOLDOWN_SECONDS = 3600
FAKEBOT_ALERT_COOLDOWN_SECONDS = 600
OFFSET_FILE = BASE_DIR / ".updates_offset"
LOCK_FILE = BASE_DIR / ".shuibei.lock"
PID_FILE = BASE_DIR / ".shuibei.pid"
LOG_FILE = BASE_DIR / "shuibei.log"
