"""
Global settings for the Instagram Model DM Bot.
"""
import os

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

# ──────────────────────────────────────────────
# Telegram
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "8770603555:AAFZ50LilZHKigpr0wy2jawAjOI3m6qMmoE"
TELEGRAM_CHAT_IDS = ["8592007309"] # Add multiple IDs here, e.g., ["123", "456"]

# ──────────────────────────────────────────────
# DM Limits
# ──────────────────────────────────────────────
DM_MIN_PER_MODEL = 5           # Min DMs to send per model target
DM_MAX_PER_MODEL = 10          # Max DMs to send per model target

# ──────────────────────────────────────────────
# Delays (seconds) — human-like random intervals
# ──────────────────────────────────────────────
DM_DELAY_MIN = 3               # Min seconds between DMs
DM_DELAY_MAX = 7              # Max seconds between DMs
ACTION_DELAY_MIN = 2           # Min seconds between page actions
ACTION_DELAY_MAX = 5           # Max seconds between page actions
TYPING_DELAY_MIN = 0.05        # Min seconds between keystrokes
TYPING_DELAY_MAX = 0.15        # Max seconds between keystrokes
ACCOUNT_SWITCH_DELAY_MIN = 10  # Min seconds before switching accounts
ACCOUNT_SWITCH_DELAY_MAX = 20 # Max seconds before switching accounts
MODEL_SWITCH_DELAY_MIN = 15    # Min seconds before switching models
MODEL_SWITCH_DELAY_MAX = 20    # Max seconds before switching models
COOLDOWN_MIN = 25              # Min minutes between bot sessions
COOLDOWN_MAX = 40              # Max minutes between bot sessions

# ──────────────────────────────────────────────
# Post Age Priority
# ──────────────────────────────────────────────
POST_AGE_PRIORITY_HOURS = 24

# ──────────────────────────────────────────────
# Scraping Limits
# ──────────────────────────────────────────────
MAX_POSTS_TO_CHECK = 6         # Max recent posts to check per model
MAX_LIKERS_PER_POST = 30       # Max likers to scrape per post
MAX_FOLLOWERS_TO_SCRAPE = 50   # Max followers to scrape per model

# ──────────────────────────────────────────────
# Challenge Handling
# ──────────────────────────────────────────────
CHALLENGE_WAIT_TIMEOUT = 300   # Seconds to wait for employee to respond via Telegram
CHALLENGE_POLL_INTERVAL = 5    # Seconds between Telegram polls for code

# ──────────────────────────────────────────────
# Instagram URLs
# ──────────────────────────────────────────────
INSTAGRAM_BASE_URL = "https://www.instagram.com"
INSTAGRAM_LOGIN_URL = "https://www.instagram.com/accounts/login/"
