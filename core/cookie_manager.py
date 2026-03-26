"""
Cookie manager for per-account session persistence.
Saves, loads, and refreshes cookies to avoid repeated logins.
"""
import json
import os
import time
import logging

from config.settings import COOKIES_DIR

logger = logging.getLogger("model_dm_bot")


def _cookie_path(account_name: str) -> str:
    """Get the cookie file path for a specific account."""
    safe_name = account_name.replace("@", "").replace(".", "_")
    return os.path.join(COOKIES_DIR, f"{safe_name}.json")


def cookies_exist(account_name: str) -> bool:
    """Check if a cookie file exists for the given account."""
    path = _cookie_path(account_name)
    if not os.path.exists(path):
        return False
    # Check if file has content
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return isinstance(data, list) and len(data) > 0
    except (json.JSONDecodeError, IOError):
        return False


def save_cookies(driver, account_name: str) -> bool:
    """
    Save all browser cookies to a JSON file for the given account.
    
    Args:
        driver: Selenium WebDriver instance
        account_name: Instagram username
    
    Returns:
        True if cookies were saved successfully
    """
    try:
        cookies = driver.get_cookies()
        if not cookies:
            logger.warning(f"[{account_name}] No cookies to save")
            return False

        path = _cookie_path(account_name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f, indent=2)

        logger.info(f"[{account_name}] Saved {len(cookies)} cookies to {os.path.basename(path)}")
        return True
    except Exception as e:
        logger.error(f"[{account_name}] Failed to save cookies: {e}")
        return False


def load_cookies(driver, account_name: str) -> bool:
    """
    Load cookies from file and inject them into the browser.
    The browser must already be on instagram.com before calling this.
    
    Args:
        driver: Selenium WebDriver instance (must be on instagram.com)
        account_name: Instagram username
    
    Returns:
        True if cookies were loaded and injected successfully
    """
    path = _cookie_path(account_name)
    if not os.path.exists(path):
        logger.info(f"[{account_name}] No cookie file found at {path}")
        return False

    try:
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)

        if not cookies:
            logger.info(f"[{account_name}] Cookie file is empty")
            return False

        # Clear existing cookies first
        driver.delete_all_cookies()

        injected = 0
        for cookie in cookies:
            try:
                clean = {
                    "name": cookie["name"],
                    "value": cookie["value"],
                    "domain": cookie.get("domain", ".instagram.com"),
                    "path": cookie.get("path", "/"),
                    "secure": cookie.get("secure", True),
                }
                if "expiry" in cookie:
                    clean["expiry"] = int(cookie["expiry"])
                if "httpOnly" in cookie:
                    clean["httpOnly"] = cookie["httpOnly"]
                if "sameSite" in cookie:
                    if cookie["sameSite"] in ["Strict", "Lax", "None"]:
                        clean["sameSite"] = cookie["sameSite"]

                driver.add_cookie(clean)
                injected += 1
            except Exception:
                pass  # Some cookies may fail (cross-domain, etc.)

        logger.info(f"[{account_name}] Injected {injected}/{len(cookies)} cookies")
        return injected > 0

    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"[{account_name}] Failed to load cookies: {e}")
        return False


def refresh_cookies(driver, account_name: str) -> bool:
    """
    Re-save cookies after a successful session to extend their lifetime.
    Call this after verified login to keep the session fresh.
    """
    logger.info(f"[{account_name}] Refreshing cookies...")
    return save_cookies(driver, account_name)


def delete_cookies(account_name: str) -> bool:
    """Delete the cookie file for an account (e.g. when session is invalid)."""
    path = _cookie_path(account_name)
    if os.path.exists(path):
        os.remove(path)
        logger.info(f"[{account_name}] Deleted cookie file")
        return True
    return False
