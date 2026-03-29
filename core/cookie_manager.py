"""
Cookie manager for per-account session persistence.
Saves, loads, and refreshes cookies to avoid repeated logins.
"""
import json
import os
import time
import logging

from config import database

logger = logging.getLogger("model_dm_bot")


def cookies_exist(account_name: str) -> bool:
    """Check if a cookie exists mapping for the given account."""
    data = database.get_cookies(account_name)
    return isinstance(data, list) and len(data) > 0


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

        database.save_cookies(account_name, cookies)

        logger.info(f"[{account_name}] Saved {len(cookies)} cookies to Database")
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
    cookies = database.get_cookies(account_name)
    if not cookies:
        logger.info(f"[{account_name}] No cookies found in database")
        return False

    try:

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
    """Delete the cookie entry for an account."""
    if cookies_exist(account_name):
        database.save_cookies(account_name, [])
        logger.info(f"[{account_name}] Deleted cookies from Database")
        return True
    return False
