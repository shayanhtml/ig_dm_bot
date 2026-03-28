"""
Global settings for the Instagram Model DM Bot.
Loads dynamically from settings.json to support Web UI changes.
"""
import os
import json

# ──────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
COOKIES_DIR = os.path.join(BASE_DIR, "cookies")
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(BASE_DIR, "logs")

# Ensure directories exist
for d in [COOKIES_DIR, DATA_DIR, LOGS_DIR]:
    os.makedirs(d, exist_ok=True)

# Config file paths
ACCOUNTS_FILE = os.path.join(CONFIG_DIR, "accounts.json")
MODELS_FILE = os.path.join(CONFIG_DIR, "models.json")
MESSAGES_FILE = os.path.join(CONFIG_DIR, "messages.json")
DM_LOG_FILE = os.path.join(DATA_DIR, "dm_log.json")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")


# ──────────────────────────────────────────────
# Dynamic Settings Registration
# ──────────────────────────────────────────────
# These are the default values. If settings.json doesn't exist, it will be
# created automatically with these defaults.
DEFAULT_SETTINGS = {
    "TELEGRAM_BOT_TOKEN": "8770603555:AAFZ50LilZHKigpr0wy2jawAjOI3m6qMmoE",
    "TELEGRAM_CHAT_IDS": ["8592007309"],
    "DM_MIN_PER_MODEL": 5,
    "DM_MAX_PER_MODEL": 10,
    "DM_DELAY_MIN": 3,
    "DM_DELAY_MAX": 7,
    "ACTION_DELAY_MIN": 2,
    "ACTION_DELAY_MAX": 5,
    "TYPING_DELAY_MIN": 0.05,
    "TYPING_DELAY_MAX": 0.15,
    "ACCOUNT_SWITCH_DELAY_MIN": 10,
    "ACCOUNT_SWITCH_DELAY_MAX": 20,
    "MODEL_SWITCH_DELAY_MIN": 15,
    "MODEL_SWITCH_DELAY_MAX": 20,
    "COOLDOWN_MIN": 25,
    "COOLDOWN_MAX": 40,
    "POST_AGE_PRIORITY_HOURS": 24,
    "MAX_POSTS_TO_CHECK": 6,
    "MAX_LIKERS_PER_POST": 30,
    "MAX_FOLLOWERS_TO_SCRAPE": 50,
    "CHALLENGE_WAIT_TIMEOUT": 300,
    "CHALLENGE_POLL_INTERVAL": 5
}

# Create settings.json if missing
if not os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_SETTINGS, f, indent=2)

# Load settings from file safely
try:
    with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
        _loaded_settings = json.load(f)
except Exception:
    _loaded_settings = {}

# Expose all settings seamlessly back to the Python globals
# so imports like `from config.settings import DM_DELAY_MIN` continue to work.
for _key, _default_val in DEFAULT_SETTINGS.items():
    globals()[_key] = _loaded_settings.get(_key, _default_val)

# ──────────────────────────────────────────────
# Static URLs
# ──────────────────────────────────────────────
INSTAGRAM_BASE_URL = "https://www.instagram.com"
INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"
