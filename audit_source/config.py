# -*- coding: utf-8 -*-
"""Public audit configuration.

Production credentials, internal paths and private API routes are intentionally
not stored in this repository. They are injected by the private MoQing runtime.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(os.environ.get("SHUIBEI_STATE_DIR", "./runtime-data")).resolve()

BOT_TOKEN = os.environ.get("SHUIBEI_BOT_TOKEN", "")
BOT_USERNAME = os.environ.get("SHUIBEI_BOT_USERNAME", "@ShuiBei_bot")
BOT_DISPLAY_NAME = "水杯记账"

LEDGER_DB_PATH = BASE_DIR / "ledger.db"
APP_DB_PATH = BASE_DIR / "shuibei.db"
ARCHIVE_DB_PATH = BASE_DIR / "archive.db"
SYNC_DB_PATH = BASE_DIR / "sync.db"
MESSAGE_PROTECTION_KEY_PATH = BASE_DIR / ".message_protection_key"
MESSAGE_PROTECTION_TTL_SECONDS = 7 * 86400
CHANNEL_VERIFIER_TOKEN_PATH = BASE_DIR / ".channel_verifier_token"

# Shared production data sources are not exposed in the public audit snapshot.
MAIN_LEDGER_DB_PATH = None
SCAM_DB_PATH = None
FAKEBOT_OFFICIAL_FILE = None

REQUIRED_CHANNEL = "@jizhangaide"
REQUIRED_CHANNEL_URL = "https://t.me/jizhangaide"
MEMBERSHIP_FALLBACK_BOT = "@jizhangaide_bot"
MEMBERSHIP_TRIAL_DAYS = 30
MEMBERSHIP_REFERRAL_TTL_SECONDS = 3 * 86400
MEMBERSHIP_REMINDER_SECONDS = 2 * 86400
MEMBERSHIP_7D_USDT = "3"
MEMBERSHIP_30D_USDT = "6.66"
SHUIBEI_ADMIN_USER_IDS: tuple[int, ...] = ()

# Public code never hardcodes provider credentials.
TRONGRID_API_KEY = os.environ.get("TRONGRID_API_KEY", "")
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

# The actual MoQing endpoint/route is injected by the private runtime.
MOQING_API_ENDPOINT = os.environ.get("MOQING_API_ENDPOINT", "")
MOQING_RUNTIME_TOKEN = os.environ.get("MOQING_RUNTIME_TOKEN", "")
