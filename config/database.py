import sqlite3
import json
import os
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
DATABASE_PATH = os.path.join(DATA_DIR, "app_data.db")

# Canonical setting keys live in the database. These values are only used to
# seed missing rows during initialization.
DEFAULT_DB_SETTINGS = {
    "TELEGRAM_BOT_TOKEN": "8671289565:AAFxbYRSVvPkFRUaymh2T7BG6hyE-oIXXnE",
    "TELEGRAM_CHAT_IDS": ["128663994"],
    "MODEL_MESSAGE_MAP": {},
    "MODEL_MESSAGE_META": {},
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
    "CHALLENGE_POLL_INTERVAL": 5,
    "WEB_UI_USERNAME": "beyinstabot",
    "WEB_UI_PASSWORD": "#beymedia!",
}

def _get_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _table_columns(conn, table_name: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {r["name"] for r in rows}


def _get_setting_from_conn(conn, key: str, default=None):
    row = conn.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value_json"])
    except Exception:
        return default


def _ensure_default_settings(conn):
    for key, value in DEFAULT_DB_SETTINGS.items():
        exists = conn.execute(
            "SELECT 1 FROM settings WHERE key = ?",
            (key,),
        ).fetchone()
        if exists:
            continue

        conn.execute(
            "INSERT INTO settings (key, value_json) VALUES (?, ?)",
            (key, json.dumps(value)),
        )


def _ensure_account_owner_column(conn):
    if "owner_username" in _table_columns(conn, "accounts"):
        return

    conn.execute("ALTER TABLE accounts ADD COLUMN owner_username TEXT NOT NULL DEFAULT 'master'")
    conn.execute("UPDATE accounts SET owner_username = 'master' WHERE owner_username IS NULL OR owner_username = ''")


def _ensure_account_model_label_column(conn):
    if "model_label" in _table_columns(conn, "accounts"):
        return

    conn.execute("ALTER TABLE accounts ADD COLUMN model_label TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE accounts SET model_label = '' WHERE model_label IS NULL")


def _ensure_account_custom_messages_column(conn):
    columns = _table_columns(conn, "accounts")
    if "custom_messages_json" not in columns:
        conn.execute("ALTER TABLE accounts ADD COLUMN custom_messages_json TEXT NOT NULL DEFAULT '[]'")

    conn.execute(
        "UPDATE accounts SET custom_messages_json = '[]' "
        "WHERE custom_messages_json IS NULL OR TRIM(custom_messages_json) = ''"
    )


def _ensure_account_proxy_column(conn):
    if "proxy" in _table_columns(conn, "accounts"):
        return
    conn.execute("ALTER TABLE accounts ADD COLUMN proxy TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE accounts SET proxy = '' WHERE proxy IS NULL")


def _normalize_account_model_label(value) -> str:
    clean = str(value or "").strip().lstrip("@")
    key = clean.lower()
    if key in ("", "generic", "any", "all", "*", "none"):
        return ""
    return clean


def _normalize_account_custom_messages(raw_messages) -> list:
    if not isinstance(raw_messages, list):
        return []

    clean_messages = []
    for msg in raw_messages:
        if not isinstance(msg, str):
            continue
        trimmed = msg.strip()
        if trimmed:
            clean_messages.append(trimmed)
    return clean_messages


def _ensure_master_user(conn):
    users_count_row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    users_count = int(users_count_row["c"] if users_count_row else 0)

    if users_count > 0:
        master_row = conn.execute("SELECT id FROM users WHERE role = 'master' LIMIT 1").fetchone()
        if master_row:
            return

    seed_username = str(
        _get_setting_from_conn(conn, "WEB_UI_USERNAME", DEFAULT_DB_SETTINGS["WEB_UI_USERNAME"]) or ""
    ).strip().lower()
    seed_password = str(
        _get_setting_from_conn(conn, "WEB_UI_PASSWORD", DEFAULT_DB_SETTINGS["WEB_UI_PASSWORD"]) or ""
    )
    if not seed_username:
        seed_username = "beyinstabot"
    if not seed_password:
        seed_password = "#beymedia!"

    existing_named = conn.execute("SELECT id FROM users WHERE username = ?", (seed_username,)).fetchone()
    now_str = datetime.now().isoformat()

    if existing_named:
        conn.execute("UPDATE users SET role = 'master', is_active = 1 WHERE id = ?", (existing_named["id"],))
        return

    conn.execute(
        """
        INSERT INTO users (username, password_hash, role, is_active, created_at)
        VALUES (?, ?, 'master', 1, ?)
        """,
        (seed_username, generate_password_hash(seed_password), now_str),
    )

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = _get_connection()
    try:
        # Accounts Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT,
                owner_username TEXT NOT NULL DEFAULT 'master',
                model_label TEXT NOT NULL DEFAULT '',
                custom_messages_json TEXT NOT NULL DEFAULT '[]',
                proxy TEXT NOT NULL DEFAULT ''
            )
        """)
        
        # Models Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL
            )
        """)
        
        # Messages Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT UNIQUE NOT NULL
            )
        """)
        
        # Settings Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            )
        """)

        _ensure_default_settings(conn)
        
        # DM Logs Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dm_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)

        # DM Event Log Table (full per-attempt history for reporting)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dm_event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_account TEXT NOT NULL,
                target_username TEXT NOT NULL,
                model_username TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'sent',
                timestamp TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dm_event_log_timestamp ON dm_event_log(timestamp DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dm_event_log_sender ON dm_event_log(sender_account, timestamp DESC)"
        )

        # Dashboard Users Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                CHECK(role IN ('master', 'employee'))
            )
        """)
        
        # Cookies Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cookies (
                username TEXT PRIMARY KEY,
                cookie_json TEXT NOT NULL
            )
        """)

        # Employee Activity Log Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_username TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_value TEXT NOT NULL DEFAULT '',
                details_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_log_created_at ON activity_log(created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activity_log_actor ON activity_log(actor_username)"
        )

        # Schema/data migrations
        _ensure_account_owner_column(conn)
        _ensure_account_model_label_column(conn)
        _ensure_account_custom_messages_column(conn)
        _ensure_account_proxy_column(conn)
        _ensure_master_user(conn)

        conn.commit()
    finally:
        conn.close()

# ── Auth/User CRUD ──
def authenticate_user(username: str, password: str):
    clean_username = str(username or "").strip().lower()
    if not clean_username or not password:
        return None

    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT username, password_hash, role, is_active FROM users WHERE username = ?",
            (clean_username,),
        ).fetchone()
        if not row or int(row["is_active"] or 0) != 1:
            return None

        stored_hash = row["password_hash"] or ""
        valid = False

        # Fallback allows one-time migration from legacy plain-text values if any exist.
        if stored_hash.startswith("scrypt:") or stored_hash.startswith("pbkdf2:"):
            valid = check_password_hash(stored_hash, password)
        else:
            valid = stored_hash == password
            if valid:
                conn.execute(
                    "UPDATE users SET password_hash = ? WHERE username = ?",
                    (generate_password_hash(password), clean_username),
                )
                conn.commit()

        if not valid:
            return None

        return {"username": row["username"], "role": row["role"]}
    finally:
        conn.close()


def get_users():
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT username, role, is_active, created_at
            FROM users
            ORDER BY CASE role WHEN 'master' THEN 0 ELSE 1 END, username
            """
        ).fetchall()
        return [
            {
                "username": r["username"],
                "role": r["role"],
                "is_active": bool(r["is_active"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]
    finally:
        conn.close()


def create_user(username: str, password: str, role: str = "employee"):
    clean_username = str(username or "").strip().lower()
    clean_password = str(password or "")
    clean_role = str(role or "employee").strip().lower()

    if clean_role not in ("master", "employee"):
        raise ValueError("Invalid role")
    if not clean_username:
        raise ValueError("Username is required")
    if len(clean_password) < 4:
        raise ValueError("Password must be at least 4 characters")

    conn = _get_connection()
    try:
        now_str = datetime.now().isoformat()
        conn.execute(
            """
            INSERT INTO users (username, password_hash, role, is_active, created_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (clean_username, generate_password_hash(clean_password), clean_role, now_str),
        )
        conn.commit()
        return {"username": clean_username, "role": clean_role, "is_active": True, "created_at": now_str}
    except sqlite3.IntegrityError:
        raise ValueError("User already exists")
    finally:
        conn.close()


def update_user_password(username: str, new_password: str):
    clean_username = str(username or "").strip().lower()
    clean_password = str(new_password or "")
    if not clean_username:
        raise ValueError("Username is required")
    if len(clean_password) < 4:
        raise ValueError("Password must be at least 4 characters")

    conn = _get_connection()
    try:
        updated = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (generate_password_hash(clean_password), clean_username),
        ).rowcount
        conn.commit()
        return updated > 0
    finally:
        conn.close()


def update_user_credentials(username: str, new_username: str = None, new_password: str = None):
    """Update a dashboard user's username and/or password.

    Returns dict with old/new username and role when successful, None if user is missing.
    """
    clean_current = str(username or "").strip().lower()
    if not clean_current:
        raise ValueError("Username is required")

    candidate_username = str(new_username if new_username is not None else clean_current).strip().lower()
    if not candidate_username:
        raise ValueError("Username is required")

    password_to_set = None
    if new_password is not None and str(new_password) != "":
        password_to_set = str(new_password)
        if len(password_to_set) < 4:
            raise ValueError("Password must be at least 4 characters")

    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT username, role FROM users WHERE username = ?",
            (clean_current,),
        ).fetchone()
        if not row:
            return None

        if candidate_username != clean_current:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE username = ?",
                (candidate_username,),
            ).fetchone()
            if exists:
                raise ValueError("User already exists")

        if password_to_set is not None:
            conn.execute(
                "UPDATE users SET username = ?, password_hash = ? WHERE username = ?",
                (candidate_username, generate_password_hash(password_to_set), clean_current),
            )
        else:
            conn.execute(
                "UPDATE users SET username = ? WHERE username = ?",
                (candidate_username, clean_current),
            )

        if candidate_username != clean_current:
            conn.execute(
                "UPDATE accounts SET owner_username = ? WHERE owner_username = ?",
                (candidate_username, clean_current),
            )

        conn.commit()
        return {
            "old_username": clean_current,
            "username": candidate_username,
            "role": row["role"],
        }
    finally:
        conn.close()


def delete_user(username: str):
    clean_username = str(username or "").strip().lower()
    if not clean_username:
        raise ValueError("Username is required")

    conn = _get_connection()
    try:
        row = conn.execute("SELECT role FROM users WHERE username = ?", (clean_username,)).fetchone()
        if not row:
            return False
        if row["role"] == "master":
            raise ValueError("Cannot delete master user")

        # Keep account ownership valid by re-assigning to master.
        conn.execute("UPDATE accounts SET owner_username = 'master' WHERE owner_username = ?", (clean_username,))
        conn.execute("DELETE FROM users WHERE username = ?", (clean_username,))
        conn.commit()
        return True
    finally:
        conn.close()


def is_valid_admin_login(username, password):
    """Backward-compatible alias used by older server routes."""
    return authenticate_user(username, password) is not None

# ── Accounts CRUD ──
def get_accounts(owner_username: str = None, include_all: bool = False):
    conn = _get_connection()
    try:
        if include_all:
            rows = conn.execute(
                "SELECT username, password, owner_username, model_label, custom_messages_json, proxy "
                "FROM accounts ORDER BY owner_username, username"
            ).fetchall()
        else:
            clean_owner = str(owner_username or "").strip().lower()
            if not clean_owner:
                return []
            rows = conn.execute(
                "SELECT username, password, owner_username, model_label, custom_messages_json, proxy "
                "FROM accounts WHERE owner_username = ? ORDER BY username",
                (clean_owner,),
            ).fetchall()

        accounts = []
        for r in rows:
            try:
                raw_custom_messages = json.loads(r["custom_messages_json"] or "[]")
            except Exception:
                raw_custom_messages = []

            accounts.append(
                {
                    "username": r["username"],
                    "password": r["password"] or "",
                    "owner_username": r["owner_username"] or "master",
                    "model_label": str(r["model_label"] or "").strip(),
                    "custom_messages": _normalize_account_custom_messages(raw_custom_messages),
                    "proxy": str(r["proxy"] or "").strip(),
                }
            )

        return accounts
    finally:
        conn.close()

def save_accounts(accounts_list, owner_username: str = None, include_all: bool = False):
    if not isinstance(accounts_list, list):
        raise ValueError("Accounts payload must be a list")

    conn = _get_connection()
    try:
        if include_all:
            conn.execute("DELETE FROM accounts")
            for acc in accounts_list:
                username = str(acc.get("username", "")).strip()
                if not username:
                    continue
                password = str(acc.get("password", "")).strip() or None
                owner = str(acc.get("owner_username") or acc.get("owner") or "master").strip().lower()
                owner = owner or "master"
                model_label = _normalize_account_model_label(acc.get("model_label", ""))
                custom_messages_json = json.dumps(
                    _normalize_account_custom_messages(acc.get("custom_messages", []))
                )
                proxy = str(acc.get("proxy", "") or "").strip()
                conn.execute(
                    "INSERT INTO accounts (username, password, owner_username, model_label, custom_messages_json, proxy) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, password, owner, model_label, custom_messages_json, proxy),
                )
        else:
            clean_owner = str(owner_username or "").strip().lower()
            if not clean_owner:
                raise ValueError("Owner username is required")

            conn.execute("DELETE FROM accounts WHERE owner_username = ?", (clean_owner,))
            for acc in accounts_list:
                username = str(acc.get("username", "")).strip()
                if not username:
                    continue
                password = str(acc.get("password", "")).strip() or None
                model_label = _normalize_account_model_label(acc.get("model_label", ""))
                custom_messages_json = json.dumps(
                    _normalize_account_custom_messages(acc.get("custom_messages", []))
                )
                proxy = str(acc.get("proxy", "") or "").strip()
                conn.execute(
                    "INSERT INTO accounts (username, password, owner_username, model_label, custom_messages_json, proxy) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (username, password, clean_owner, model_label, custom_messages_json, proxy),
                )

        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError("One or more account usernames are already assigned")
    finally:
        conn.close()


def update_account_proxy(account_username: str, proxy_value: str) -> bool:
    """Update proxy for an existing IG account username."""
    clean_username = str(account_username or "").strip()
    if not clean_username:
        raise ValueError("Account username is required")

    clean_proxy = str(proxy_value or "").strip()

    conn = _get_connection()
    try:
        result = conn.execute(
            "UPDATE accounts SET proxy = ? WHERE username = ?",
            (clean_proxy, clean_username),
        )
        conn.commit()
        return (result.rowcount or 0) > 0
    finally:
        conn.close()


def user_can_access_account(account_username: str, requester_username: str, requester_role: str) -> bool:
    clean_requester = str(requester_username or "").strip().lower()
    clean_role = str(requester_role or "").strip().lower()
    if clean_role == "master":
        return True

    conn = _get_connection()
    try:
        row = conn.execute(
            "SELECT owner_username FROM accounts WHERE username = ?",
            (str(account_username or "").strip(),),
        ).fetchone()
        if not row:
            return False
        return (row["owner_username"] or "").strip().lower() == clean_requester
    finally:
        conn.close()

# ── Models CRUD ──
def get_models():
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT username FROM models").fetchall()
        return [str(r["username"] or "").strip() for r in rows if str(r["username"] or "").strip()]
    finally:
        conn.close()

def save_models(models_list):
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM models")
        for model in models_list:
            clean_model = str(model or "").strip().lstrip("@")
            if not clean_model:
                continue
            conn.execute("INSERT OR IGNORE INTO models (username) VALUES (?)", (clean_model,))
        conn.commit()
    finally:
        conn.close()

# ── Messages CRUD ──
def get_messages():
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT text FROM messages").fetchall()
        return [str(r["text"] or "").strip() for r in rows if str(r["text"] or "").strip()]
    finally:
        conn.close()

def save_messages(messages_list):
    conn = _get_connection()
    try:
        conn.execute("DELETE FROM messages")
        for msg in messages_list:
            clean_msg = str(msg or "").strip()
            if not clean_msg:
                continue
            conn.execute("INSERT OR IGNORE INTO messages (text) VALUES (?)", (clean_msg,))
        conn.commit()
    finally:
        conn.close()

# ── Settings CRUD ──
def get_all_settings():
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT key, value_json FROM settings").fetchall()
        merged = {}
        for row in rows:
            try:
                merged[row["key"]] = json.loads(row["value_json"])
            except: pass
        return merged
    finally:
        conn.close()

def get_setting(key, default=None):
    all_sets = get_all_settings()
    return all_sets.get(key, default)


def get_required_setting(key):
    value = get_setting(key, None)
    if value is None:
        raise KeyError(f"Missing required setting in database: {key}")
    return value

def save_settings(settings_dict):
    conn = _get_connection()
    try:
        for k, v in settings_dict.items():
            conn.execute(
                "INSERT INTO settings (key, value_json) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (k, json.dumps(v))
            )
        conn.commit()
    finally:
        conn.close()

# ── DM Logs CRUD ──
def get_dm_logs():
    """Returns a dict mapping username -> timestamp (ISO format)"""
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT username, timestamp FROM dm_log").fetchall()
        return {r["username"]: r["timestamp"] for r in rows}
    finally:
        conn.close()

def log_dm_sent(username):
    conn = _get_connection()
    try:
        now_str = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO dm_log (username, timestamp) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET timestamp=excluded.timestamp",
            (username, now_str)
        )
        conn.commit()
    finally:
        conn.close()


def log_dm_event(sender_account: str, target_username: str, model_username: str = "", status: str = "sent"):
    """Append one DM attempt event for reporting and auditing."""
    clean_sender = str(sender_account or "").strip()
    clean_target = str(target_username or "").strip()
    clean_model = str(model_username or "").strip().lstrip("@")
    clean_status = str(status or "").strip().lower() or "sent"

    if not clean_sender or not clean_target:
        return

    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO dm_event_log (sender_account, target_username, model_username, status, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                clean_sender,
                clean_target,
                clean_model,
                clean_status,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_dm_sent_summary_last_hours(hours: int = 24, include_all_accounts: bool = False):
    """Return sent DM totals for the last N hours, grouped by sender account."""
    safe_hours = max(1, min(int(hours or 24), 720))
    cutoff_dt = datetime.now() - timedelta(hours=safe_hours)
    cutoff_iso = cutoff_dt.isoformat(timespec="seconds")

    conn = _get_connection()
    try:
        total_row = conn.execute(
            "SELECT COUNT(*) AS c FROM dm_event_log WHERE status = 'sent' AND timestamp >= ?",
            (cutoff_iso,),
        ).fetchone()
        total_sent = int(total_row["c"] if total_row and total_row["c"] is not None else 0)

        lifetime_row = conn.execute(
            "SELECT COUNT(*) AS c FROM dm_event_log WHERE status = 'sent'"
        ).fetchone()
        lifetime_total_sent = int(
            lifetime_row["c"] if lifetime_row and lifetime_row["c"] is not None else 0
        )

        # Backward-compatible fallback for historical installs that only had dm_log.
        if lifetime_total_sent <= 0:
            legacy_row = conn.execute(
                "SELECT COUNT(*) AS c FROM dm_log WHERE timestamp IS NOT NULL AND TRIM(timestamp) != ''"
            ).fetchone()
            lifetime_total_sent = int(
                legacy_row["c"] if legacy_row and legacy_row["c"] is not None else 0
            )

        rows = conn.execute(
            """
            SELECT sender_account, COUNT(*) AS sent_count
            FROM dm_event_log
            WHERE status = 'sent' AND timestamp >= ?
            GROUP BY sender_account
            ORDER BY sent_count DESC, sender_account ASC
            """,
            (cutoff_iso,),
        ).fetchall()

        counts_by_account = {}
        for row in rows:
            sender = str(row["sender_account"] or "").strip()
            if not sender:
                continue
            counts_by_account[sender] = int(row["sent_count"] or 0)

        if include_all_accounts:
            account_rows = conn.execute(
                "SELECT username FROM accounts ORDER BY username"
            ).fetchall()
            for row in account_rows:
                username = str(row["username"] or "").strip()
                if not username:
                    continue
                counts_by_account.setdefault(username, 0)

        by_account = [
            {"sender_account": name, "count": count}
            for name, count in sorted(
                counts_by_account.items(),
                key=lambda item: (-item[1], item[0].lower()),
            )
        ]

        return {
            "hours": safe_hours,
            "cutoff": cutoff_iso,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "total_sent": total_sent,
            "lifetime_total_sent": lifetime_total_sent,
            "by_account": by_account,
        }
    finally:
        conn.close()

# ── Cookies CRUD ──
def get_cookies(username):
    conn = _get_connection()
    try:
        row = conn.execute("SELECT cookie_json FROM cookies WHERE username = ?", (username,)).fetchone()
        if row:
            try: return json.loads(row["cookie_json"])
            except: return []
        return []
    finally:
        conn.close()

def save_cookies(username, cookies_list):
    conn = _get_connection()
    try:
        if not cookies_list:
            conn.execute("DELETE FROM cookies WHERE username = ?", (username,))
        else:
            conn.execute(
                "INSERT INTO cookies (username, cookie_json) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET cookie_json=excluded.cookie_json",
                (username, json.dumps(cookies_list))
            )
        conn.commit()
    finally:
        conn.close()


# ── Employee Activity Log ──
def log_activity(
    actor_username: str,
    actor_role: str,
    action: str,
    target_type: str = "",
    target_value: str = "",
    details=None,
):
    """Append an activity event to the audit log."""
    clean_actor = str(actor_username or "").strip().lower()
    clean_role = str(actor_role or "").strip().lower() or "employee"
    clean_action = str(action or "").strip()
    clean_target_type = str(target_type or "").strip()
    clean_target_value = str(target_value or "").strip()

    if not clean_actor or not clean_action:
        return

    payload = details if details is not None else {}
    try:
        details_json = json.dumps(payload)
    except Exception:
        details_json = json.dumps({"raw": str(payload)})

    conn = _get_connection()
    try:
        conn.execute(
            """
            INSERT INTO activity_log (
                actor_username,
                actor_role,
                action,
                target_type,
                target_value,
                details_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                clean_actor,
                clean_role,
                clean_action,
                clean_target_type,
                clean_target_value,
                details_json,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_activity_logs(limit: int = 200, employees_only: bool = False):
    """Read latest activity logs, newest first."""
    safe_limit = max(1, min(int(limit or 200), 500))

    conn = _get_connection()
    try:
        if employees_only:
            rows = conn.execute(
                """
                SELECT id, actor_username, actor_role, action, target_type, target_value, details_json, created_at
                FROM activity_log
                WHERE actor_role = 'employee'
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, actor_username, actor_role, action, target_type, target_value, details_json, created_at
                FROM activity_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (safe_limit,),
            ).fetchall()

        logs = []
        for row in rows:
            details_val = {}
            try:
                details_val = json.loads(row["details_json"])
            except Exception:
                details_val = {"raw": row["details_json"]}

            logs.append(
                {
                    "id": row["id"],
                    "actor_username": row["actor_username"],
                    "actor_role": row["actor_role"],
                    "action": row["action"],
                    "target_type": row["target_type"],
                    "target_value": row["target_value"],
                    "details": details_val,
                    "created_at": row["created_at"],
                }
            )
        return logs
    finally:
        conn.close()


def _parse_activity_datetime(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return None

    # Normalize trailing Z so fromisoformat can parse timezone-aware timestamps too.
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except Exception:
        return None

    if dt.tzinfo is not None:
        try:
            return dt.astimezone().replace(tzinfo=None)
        except Exception:
            return dt.replace(tzinfo=None)
    return dt


def get_activity_logs_recent_hours(hours: int = 24, limit: int = 500, employees_only: bool = False):
    """Read latest activity logs in the last N hours."""
    safe_limit = max(1, min(int(limit or 500), 1000))
    safe_hours = max(1, min(int(hours or 24), 168))
    scan_limit = min(max(safe_limit * 5, 1000), 5000)
    cutoff = datetime.now() - timedelta(hours=safe_hours)

    conn = _get_connection()
    try:
        if employees_only:
            rows = conn.execute(
                """
                SELECT id, actor_username, actor_role, action, target_type, target_value, details_json, created_at
                FROM activity_log
                WHERE actor_role = 'employee'
                ORDER BY id DESC
                LIMIT ?
                """,
                (scan_limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, actor_username, actor_role, action, target_type, target_value, details_json, created_at
                FROM activity_log
                ORDER BY id DESC
                LIMIT ?
                """,
                (scan_limit,),
            ).fetchall()

        logs = []
        for row in rows:
            created_at_raw = row["created_at"]
            created_dt = _parse_activity_datetime(created_at_raw)
            if created_dt is None or created_dt < cutoff:
                continue

            details_val = {}
            try:
                details_val = json.loads(row["details_json"])
            except Exception:
                details_val = {"raw": row["details_json"]}

            logs.append(
                {
                    "id": row["id"],
                    "actor_username": row["actor_username"],
                    "actor_role": row["actor_role"],
                    "action": row["action"],
                    "target_type": row["target_type"],
                    "target_value": row["target_value"],
                    "details": details_val,
                    "created_at": created_at_raw,
                }
            )

            if len(logs) >= safe_limit:
                break

        return logs
    finally:
        conn.close()
