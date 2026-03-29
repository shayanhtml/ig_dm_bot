import os
import sys
import json
import sqlite3
import shutil

# Enable absolute imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import database, settings

def get_json_data(file_path):
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
                return data
            except json.JSONDecodeError:
                pass
    return None


def run_migration():
    print("Initializing Database...")
    database.init_db()

    # 1. Accounts
    print(f"Migrating {settings.ACCOUNTS_FILE}...")
    accounts = get_json_data(settings.ACCOUNTS_FILE)
    if accounts and isinstance(accounts, list):
        database.save_accounts(accounts, include_all=True)

    # 2. Models
    print(f"Migrating {settings.MODELS_FILE}...")
    models = get_json_data(settings.MODELS_FILE)
    if models and isinstance(models, list):
        database.save_models(models)

    # 3. Messages
    print(f"Migrating {settings.MESSAGES_FILE}...")
    msgs = get_json_data(settings.MESSAGES_FILE)
    if msgs and isinstance(msgs, list):
        database.save_messages(msgs)

    # 4. Settings
    print(f"Migrating {settings.SETTINGS_FILE}...")
    sets = get_json_data(settings.SETTINGS_FILE)
    if sets and isinstance(sets, dict):
        database.save_settings(sets)

    # 5. DM Logs
    print(f"Migrating {settings.DM_LOG_FILE}...")
    dm_log = get_json_data(settings.DM_LOG_FILE)
    if dm_log and isinstance(dm_log, dict):
        conn = database._get_connection()
        try:
            for username, log_data in dm_log.items():
                timestamp = log_data.get("timestamp", "") if isinstance(log_data, dict) else str(log_data)
                conn.execute(
                    "INSERT INTO dm_log (username, timestamp) VALUES (?, ?) ON CONFLICT(username) DO UPDATE SET timestamp=excluded.timestamp",
                    (username, timestamp)
                )
            conn.commit()
        finally:
            conn.close()

    # 6. Cookies
    print(f"Migrating Cookies in {settings.COOKIES_DIR}...")
    if os.path.exists(settings.COOKIES_DIR):
        for fname in os.listdir(settings.COOKIES_DIR):
            if fname.endswith(".json"):
                username = fname[:-5]
                cookie_data = get_json_data(os.path.join(settings.COOKIES_DIR, fname))
                if cookie_data and isinstance(cookie_data, list):
                    database.save_cookies(username, cookie_data)

    print("Migration Complete.")
    print("Files can now be safely removed.")

if __name__ == "__main__":
    run_migration()
