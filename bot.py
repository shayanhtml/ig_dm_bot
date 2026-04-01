"""
Main orchestrator — the brain of the Model DM Bot.
Coordinates accounts, models, scraping, DMs, and Telegram alerts.
"""
import json
import os
import sys
import time
import random
import logging
import threading

from config.settings import LOGS_DIR
from config import database
from config.database import get_required_setting
from core.browser import create_driver, close_driver
from core.cookie_manager import save_cookies, refresh_cookies
from core.auth import (
    login_with_cookies, login_with_credentials,
    detect_challenge, handle_two_factor,
    is_logged_in, human_delay, ChallengeType,
)
from core.scraper import get_recent_posts, get_post_interactors, sort_posts_by_priority
from core.followers import get_followers
from core.dm_sender import send_dm, DMResult, wait_between_dms
from telegram.bot import telegram_bot

logger = logging.getLogger("model_dm_bot")
_active_drivers = set()
_active_drivers_lock = threading.Lock()


def _setting_int(key: str) -> int:
    """Read an integer setting from the database."""
    value = get_required_setting(key)
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid integer setting '{key}': {value}")


def _setting_float(key: str) -> float:
    """Read a float setting from the database."""
    value = get_required_setting(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid numeric setting '{key}': {value}")


def _interruptible_sleep(seconds: float, stop_event=None, tick: float = 0.5) -> bool:
    """Sleep in short ticks so stop requests can interrupt long waits."""
    end_time = time.time() + max(0, seconds)
    while time.time() < end_time:
        if stop_event and stop_event.is_set():
            return True
        remaining = end_time - time.time()
        time.sleep(min(tick, max(0.0, remaining)))
    return False


def _register_driver(driver):
    with _active_drivers_lock:
        _active_drivers.add(driver)


def _unregister_driver(driver):
    with _active_drivers_lock:
        _active_drivers.discard(driver)


def force_stop_active_sessions():
    """Force-close active browsers so stop requests interrupt current Selenium tasks."""
    with _active_drivers_lock:
        drivers = list(_active_drivers)
        _active_drivers.clear()

    for drv in drivers:
        try:
            close_driver(drv)
        except Exception:
            pass


def setup_logging():
    """Configure logging to file and console (only once)."""
    root_logger = logging.getLogger("model_dm_bot")
    if root_logger.handlers:
        return  # Already set up

    log_file = os.path.join(LOGS_DIR, "bot.log")
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler
    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    fh.setLevel(logging.INFO)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    ch.setLevel(logging.INFO)

    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(fh)
    root_logger.addHandler(ch)


# Old JSON functions removed. Bot now relies on Database.


def log_and_telegram(msg: str):
    """Log a message and add it to Telegram's log buffer."""
    logger.info(msg)
    telegram_bot.add_log(msg)


def _normalize_model_key(model_username: str) -> str:
    """Normalize model usernames to a stable lookup key."""
    return str(model_username or "").strip().lstrip("@").lower()


def _normalize_account_model_label(raw_label: str) -> str:
    """Normalize an account model label; empty means generic account."""
    key = _normalize_model_key(raw_label)
    if key in ("", "generic", "any", "all", "*", "none"):
        return ""
    return key


def _models_for_account(account: dict, all_models: list) -> list:
    """Return target models for an account.

    Account labels are campaign/model-owner tags, not target usernames,
    so they should not restrict which targets this account can process.
    """
    return list(all_models)


def _build_account_pool_summary(accounts: list, models: list) -> str:
    """Build Telegram text for per-label and generic account availability."""
    display_by_key = {}
    for model_name in models:
        key = _normalize_model_key(model_name)
        if key:
            display_by_key[key] = str(model_name or "").strip().lstrip("@")

    counts_by_model = {}
    generic_count = 0

    for account in accounts:
        label_raw = str(account.get("model_label", "")).strip().lstrip("@")
        label_key = _normalize_account_model_label(label_raw)
        if not label_key:
            generic_count += 1
            continue

        counts_by_model[label_key] = counts_by_model.get(label_key, 0) + 1
        if label_key not in display_by_key:
            display_by_key[label_key] = label_raw or label_key

    lines = ["Model Labels:"]
    for key in sorted(counts_by_model.keys(), key=lambda k: display_by_key.get(k, k).lower()):
        lines.append(f"{display_by_key.get(key, key)}: ({counts_by_model[key]}) IG Accounts Alive")
    lines.append(f"Generic: ({generic_count}) IG Accounts Alive")
    return "\n".join(lines)


def _normalize_proxy_list(raw_proxy_list) -> list:
    """Normalize proxy settings into a de-duplicated list of non-empty strings."""
    if isinstance(raw_proxy_list, str):
        raw_items = [raw_proxy_list]
    elif isinstance(raw_proxy_list, list):
        raw_items = raw_proxy_list
    else:
        return []

    clean = []
    seen = set()
    for raw_item in raw_items:
        text = str(raw_item or "")
        for line in text.splitlines():
            for part in line.split(","):
                proxy = part.strip()
                if not proxy:
                    continue
                key = proxy.lower()
                if key in seen:
                    continue
                seen.add(key)
                clean.append(proxy)
    return clean


def _assign_session_proxies(accounts: list, proxy_pool: list) -> dict:
    """Assign proxies per account for this session, reusing only when pool is smaller."""
    if not accounts or not proxy_pool:
        return {}

    shuffled_pool = list(proxy_pool)
    random.shuffle(shuffled_pool)

    assignments = {}
    pool_size = len(shuffled_pool)
    for idx, account in enumerate(accounts):
        username = str(account.get("username") or "").strip()
        if not username:
            continue
        assignments[username] = shuffled_pool[idx % pool_size]

    return assignments


def _normalize_message_list(raw_messages) -> list:
    """Normalize a raw messages array into non-empty trimmed strings."""
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


def _normalize_model_message_map(raw_map) -> dict:
    """Normalize MODEL_MESSAGE_MAP from settings into {model_key: [messages]} format."""
    if not isinstance(raw_map, dict):
        return {}

    normalized = {}
    for raw_model, raw_messages in raw_map.items():
        model_key = _normalize_model_key(raw_model)
        if not model_key:
            continue

        messages = _normalize_message_list(raw_messages)
        if messages:
            normalized[model_key] = messages

    return normalized


def _messages_for_model(model_username: str, default_messages: list, model_message_map: dict) -> list:
    """Return custom messages for a model when available, otherwise global defaults."""
    custom_messages = model_message_map.get(_normalize_model_key(model_username), [])
    return custom_messages if custom_messages else default_messages


def run_bot(stop_event=None, account_owner=None):
    """Main bot orchestration loop."""
    database.init_db()
    setup_logging()

    logger.info("=" * 60)
    logger.info("  INSTAGRAM MODEL DM BOT — STARTING")
    logger.info("=" * 60)

    # Load config from Database
    try:
        if account_owner:
            accounts = database.get_accounts(owner_username=account_owner)
        else:
            accounts = database.get_accounts(include_all=True)

        models = database.get_models()
        messages = _normalize_message_list(database.get_messages())
        model_message_map = _normalize_model_message_map(
            database.get_setting("MODEL_MESSAGE_MAP") or {}
        )
        proxy_pool = _normalize_proxy_list(database.get_setting("PROXY_LIST", []))

        # If explicit model list is empty, derive targets from model-specific sets.
        if not models and model_message_map:
            models = sorted(model_message_map.keys())
    except Exception as e:
        logger.error(f"Failed to load config from database: {e}")
        return

    if not accounts:
        if account_owner:
            logger.error(f"No accounts configured for employee @{account_owner}")
        else:
            logger.error("No accounts configured")
        return
    if not models:
        logger.error("No models configured in database")
        return
    if not messages and not model_message_map:
        logger.error("No messages configured (general or model-specific)")
        return

    logger.info(
        f"Loaded {len(accounts)} accounts, {len(models)} models, "
        f"{len(messages)} general messages, {len(model_message_map)} model-specific sets, "
        f"{len(proxy_pool)} proxies"
    )
    if account_owner:
        logger.info(f"Account scope: employee @{account_owner}")

    proxy_assignments = _assign_session_proxies(accounts, proxy_pool)
    if proxy_pool and len(proxy_pool) < len(accounts):
        logger.warning(
            "[ProxyPool] Pool has fewer proxies than accounts; some proxies will be reused this session"
        )

    # Load DM log and build 24-hour exclusion set
    from datetime import datetime, timedelta
    dm_log = database.get_dm_logs()
    already_dmd = set()
    cutoff_time = datetime.now() - timedelta(hours=24)
    
    for user_dmd, timestamp_str in dm_log.items():
        try:
            if not timestamp_str:
                already_dmd.add(user_dmd)
                continue
            
            # support fromisoformat compatibility
            safe_ts = timestamp_str.replace("Z", "+00:00")
            dmd_time = datetime.fromisoformat(safe_ts)
            if dmd_time > cutoff_time:
                already_dmd.add(user_dmd)
        except (ValueError, TypeError):
            # Fallback for old/corrupted formats
            already_dmd.add(user_dmd)

    # Start Telegram
    telegram_bot.start_polling()
    telegram_bot.send_startup()
    telegram_bot.send_account_pool_summary(_build_account_pool_summary(accounts, models))
    telegram_bot.stats["status"] = "Running"

    total_dms_sent = 0
    completed_model_keys = set()

    try:
        for account in accounts:
            if stop_event and stop_event.is_set():
                log_and_telegram("🛑 Stop requested. Ending current session.")
                break

            username = account["username"]
            account_model_key = _normalize_account_model_label(account.get("model_label", ""))
            account_models = _models_for_account(account, models)
            account_custom_messages = _normalize_message_list(account.get("custom_messages"))
            account_label_display = str(account.get("model_label", "")).strip().lstrip("@") or ""

            log_and_telegram(f"━━━ Switching to account: @{username} ━━━")
            if account_model_key:
                log_and_telegram(f"[{username}] 🏷️ Marketing label: {account_label_display}")
            else:
                log_and_telegram(f"[{username}] 🏷️ Marketing label: Generic")

            telegram_bot.stats["current_account"] = username
            telegram_bot.stats["accounts_used"] += 1

            # Create browser
            driver = None
            pool_proxy = str(proxy_assignments.get(username) or "").strip()
            manual_proxy = str(account.get("proxy") or "").strip()
            account_proxy = pool_proxy or manual_proxy or None
            try:
                if account_proxy:
                    from core.browser import _mask_proxy_for_log
                    proxy_source = "pool" if pool_proxy else "account"
                    log_and_telegram(
                        f"[{username}] 🌐 Using {proxy_source} proxy: {_mask_proxy_for_log(account_proxy)}"
                    )
                else:
                    log_and_telegram(f"[{username}] 🌐 Using direct connection (no proxy)")
                driver = create_driver(proxy=account_proxy)
                _register_driver(driver)
            except Exception as e:
                log_and_telegram(f"❌ Failed to create browser for @{username}: {e}")
                log_and_telegram("⚠️ Check your internet connection — ChromeDriver needs to download.")
                continue

            try:
                # Login
                logged_in = _perform_login(driver, account)
                if not logged_in:
                    log_and_telegram(f"❌ Failed to login @{username}, skipping")
                    close_driver(driver)
                    continue

                # Process each model allowed for this account
                for model_username in account_models:
                    if stop_event and stop_event.is_set():
                        log_and_telegram("🛑 Stop requested, breaking model loop.")
                        break

                    if not telegram_bot._polling:
                        log_and_telegram("🛑 Stop requested, finishing up...")
                        break

                    log_and_telegram(f"🎯 Targeting model: @{model_username}")
                    telegram_bot.stats["current_model"] = model_username

                    custom_messages = model_message_map.get(_normalize_model_key(model_username), [])
                    label_messages = model_message_map.get(account_model_key, []) if account_model_key else []
                    if account_model_key and account_custom_messages:
                        messages_for_model = account_custom_messages
                        log_and_telegram(
                            f"[{username}] Using {len(messages_for_model)} account custom messages for @{model_username}"
                        )
                    elif account_model_key:
                        # Labeled accounts use messages mapped to their marketing label.
                        messages_for_model = label_messages if label_messages else messages
                        if not messages_for_model:
                            log_and_telegram(
                                f"[{username}] ⚠️ No messages configured for marketing label '{account_label_display}', skipping"
                            )
                            continue

                        if label_messages:
                            log_and_telegram(
                                f"[{username}] Using {len(messages_for_model)} label custom messages ({account_label_display}) for @{model_username}"
                            )
                        else:
                            log_and_telegram(
                                f"[{username}] No label message set for {account_label_display}; using general messages for @{model_username}"
                            )
                    else:
                        messages_for_model = custom_messages if custom_messages else messages
                        if not messages_for_model:
                            log_and_telegram(f"[{username}] ⚠️ No messages configured for @{model_username}, skipping")
                            continue

                        if custom_messages:
                            log_and_telegram(
                                f"[{username}] Using {len(messages_for_model)} custom messages for @{model_username}"
                            )
                        else:
                            log_and_telegram(
                                f"[{username}] Using general message pool for @{model_username}"
                            )

                    dms_for_model = _process_model(
                        driver, account, model_username, messages_for_model, dm_log, already_dmd, stop_event=stop_event
                    )

                    total_dms_sent += dms_for_model
                    telegram_bot.stats["dms_sent"] = total_dms_sent

                    if dms_for_model > 0:
                        model_key = _normalize_model_key(model_username) or str(model_username or "").strip().lower()
                        if model_key:
                            completed_model_keys.add(model_key)
                        telegram_bot.stats["models_processed"] = len(completed_model_keys)
                        telegram_bot.send_model_complete(model_username, dms_for_model)

                    # Check if still logged in
                    if not is_logged_in(driver):
                        log_and_telegram(f"⚠️ Lost login for @{username} during model processing")
                        break

                    # Delay before next model
                    model_delay_min = _setting_float("MODEL_SWITCH_DELAY_MIN")
                    model_delay_max = _setting_float("MODEL_SWITCH_DELAY_MAX")
                    if model_delay_max < model_delay_min:
                        model_delay_min, model_delay_max = model_delay_max, model_delay_min

                    delay = random.uniform(model_delay_min, model_delay_max)
                    log_and_telegram(f"⏳ Waiting {delay:.0f}s before next model...")
                    if _interruptible_sleep(delay, stop_event=stop_event):
                        break

                # Refresh cookies after session
                refresh_cookies(driver, username)

            except Exception as e:
                log_and_telegram(f"❌ Error with @{username}: {e}")
                telegram_bot.send_error(str(e))
            finally:
                close_driver(driver)
                _unregister_driver(driver)

            # Delay before switching accounts
            if account != accounts[-1] and not (stop_event and stop_event.is_set()):
                account_delay_min = _setting_float("ACCOUNT_SWITCH_DELAY_MIN")
                account_delay_max = _setting_float("ACCOUNT_SWITCH_DELAY_MAX")
                if account_delay_max < account_delay_min:
                    account_delay_min, account_delay_max = account_delay_max, account_delay_min

                delay = random.uniform(account_delay_min, account_delay_max)
                log_and_telegram(f"⏳ Waiting {delay:.0f}s before switching accounts...")
                if _interruptible_sleep(delay, stop_event=stop_event):
                    break

    except KeyboardInterrupt:
        log_and_telegram("🛑 Bot stopped by user (Ctrl+C)")
    except Exception as e:
        log_and_telegram(f"❌ Fatal error: {e}")
        telegram_bot.send_error(str(e))
    finally:
        telegram_bot.send_session_complete(total_dms_sent, len(completed_model_keys))
        telegram_bot.stats["status"] = "Stopped"
        telegram_bot.stop_polling()

    logger.info("=" * 60)
    logger.info(f"  SESSION COMPLETE — {total_dms_sent} DMs sent, {len(completed_model_keys)} unique models done")
    logger.info("=" * 60)


def _perform_login(driver, account: dict) -> bool:
    """
    Attempt login: cookies first, then credentials, handle challenges.
    """
    username = account["username"]

    # Try cookie login
    if login_with_cookies(driver, account):
        return True

    # Check if cookie login failed because it hit a challenge
    challenge = detect_challenge(driver)
    if challenge != ChallengeType.NONE:
        logger.warning(f"[{username}] Challenge detected after cookie injection, skipping credential login.")
    else:
        # Only try credential login if no challenge is blocking us
        if login_with_credentials(driver, account):
            return True
        
    # Final check for challenges (from either cookie or credential login)
    challenge = detect_challenge(driver)

    if challenge == ChallengeType.NONE:
        return False

    log_and_telegram(f"🔒 Challenge for @{username}: {challenge.value}")
    telegram_bot.send_challenge_alert(username, challenge.value, driver.current_url)

    challenge_timeout = _setting_int("CHALLENGE_WAIT_TIMEOUT")

    if challenge == ChallengeType.TWO_FACTOR:
        # Wait for employee to send code via Telegram
        code = telegram_bot.wait_for_code(challenge_timeout)
        if code:
            success = handle_two_factor(driver, account, code)
            if success:
                return True
            else:
                # Re-check for challenges in case 2FA led directly to a checkpoint/suspension
                post_2fa_challenge = detect_challenge(driver)
                if post_2fa_challenge in (ChallengeType.SUSPICIOUS_LOGIN, ChallengeType.CHECKPOINT, ChallengeType.LOCKED):
                    log_and_telegram(f"🔒 Post-2FA Verification detected: {post_2fa_challenge.value}")
                    telegram_bot.send_challenge_alert(username, post_2fa_challenge.value, driver.current_url)
                    approved = telegram_bot.wait_for_approval(challenge_timeout)
                    if approved:
                        driver.refresh()
                        human_delay(3, 5)
                        if is_logged_in(driver):
                            save_cookies(driver, username)
                            return True
            return False
        else:
            log_and_telegram(f"⏰ No 2FA code received for @{username}")
            return False

    elif challenge in (ChallengeType.SUSPICIOUS_LOGIN, ChallengeType.CHECKPOINT, ChallengeType.LOCKED):
        # Wait for employee to manually approve (including Suspended/Locked)
        telegram_bot.send_challenge_alert(username, challenge.value, driver.current_url)
        approved = telegram_bot.wait_for_approval(challenge_timeout)
        if approved:
            # Refresh the page and check login
            driver.refresh()
            human_delay(3, 5)
            if is_logged_in(driver):
                save_cookies(driver, username)
                return True
        return False

    return False


def _process_model(
    driver, account: dict, model_username: str,
    messages: list, dm_log: dict, already_dmd: set,
    stop_event=None,
) -> int:
    """
    Process a single model target:
    1. Get recent posts
    2. Sort by age (< 4hr first)
    3. DM post interactors (likers/commenters)
    4. If quota not met, DM followers
    
    Returns number of DMs successfully sent.
    """
    username = account["username"]
    dm_min = _setting_int("DM_MIN_PER_MODEL")
    dm_max = _setting_int("DM_MAX_PER_MODEL")
    if dm_max < dm_min:
        dm_min, dm_max = dm_max, dm_min
    dm_target = random.randint(dm_min, dm_max)
    dms_sent = 0

    log_and_telegram(f"[{username}] Target: send {dm_target} DMs for @{model_username}")

    # Step 1: Get recent posts
    posts = get_recent_posts(driver, model_username)
    if not posts:
        log_and_telegram(f"[{username}] No posts found for @{model_username}, going to followers")
        # Skip to followers
        followers = get_followers(driver, model_username, already_dmd, max_count=dm_target)
        dms_sent += _dm_list(driver, followers, messages, dm_log, already_dmd, dm_target, username, model_username)
        return dms_sent

    # Step 2: Sort posts by age priority
    sorted_posts = sort_posts_by_priority(posts, driver)
    
    # We want to dedicate at least 50% of DMs to followers, so cap post DMs
    post_dm_target = max(1, dm_target // 2)
    post_dms_sent = 0

    # Step 3: DM post interactors
    for post in sorted_posts:
        if stop_event and stop_event.is_set():
            break

        if dms_sent >= dm_target or post_dms_sent >= post_dm_target:
            break

        age_label = f"{post['age_hours']}h" if post['age_hours'] < 999 else "unknown"

        # Skip posts older than 24 hours
        post_age_limit = _setting_int("POST_AGE_PRIORITY_HOURS")
        if post['age_hours'] > post_age_limit:
            log_and_telegram(f"[{username}] ⏭️ Skipping post ({age_label} old) — too old: {post['url'][-20:]}")
            continue

        log_and_telegram(f"[{username}] Scraping post ({age_label} old): {post['url'][-20:]}")

        interactors = get_post_interactors(driver, post["url"], already_dmd, model_username)

        if interactors:
            remaining_for_posts = post_dm_target - post_dms_sent
            remaining = min(remaining_for_posts, dm_target - dms_sent)
            
            targets = interactors[:remaining]
            sent = _dm_list(driver, targets, messages, dm_log, already_dmd, remaining, username, model_username, stop_event=stop_event)
            dms_sent += sent
            post_dms_sent += sent

            # Progress update
            telegram_bot.send_progress(username, model_username, dms_sent, dm_target)

    # Step 4: If still under quota, DM followers
    if dms_sent < dm_target:
        remaining = dm_target - dms_sent
        log_and_telegram(f"[{username}] Need {remaining} more DMs, switching to followers of @{model_username}")

        followers = get_followers(driver, model_username, already_dmd, max_count=remaining)
        if followers:
            sent = _dm_list(driver, followers, messages, dm_log, already_dmd, remaining, username, model_username, stop_event=stop_event)
            dms_sent += sent

    log_and_telegram(f"[{username}] ✅ Completed @{model_username}: {dms_sent}/{dm_target} DMs sent")
    return dms_sent


def _dm_list(
    driver, usernames: list, messages: list,
    dm_log: dict, already_dmd: set,
    max_dms: int, sender: str, model: str,
    stop_event=None,
) -> int:
    """
    Send DMs to a list of usernames.
    
    Returns number of DMs successfully sent.
    """
    sent = 0

    for target_user in usernames:
        if stop_event and stop_event.is_set():
            log_and_telegram(f"[{sender}] 🛑 Stop requested during DM queue")
            break

        if sent >= max_dms:
            break

        if target_user in already_dmd:
            continue

        # Pick a random message template
        message = random.choice(messages)

        log_and_telegram(f"[{sender}] DMing @{target_user}...")
        result = send_dm(driver, target_user, message)

        if result == DMResult.SENT:
            sent += 1
            already_dmd.add(target_user)
            database.log_dm_sent(target_user)
            log_and_telegram(f"[{sender}] ✅ DM sent to @{target_user} ({sent}/{max_dms})")
        elif result == DMResult.CANT_MESSAGE:
            log_and_telegram(f"[{sender}] ⚠️ Can't message @{target_user}")
            already_dmd.add(target_user)  # Don't retry
        elif result == DMResult.USER_NOT_FOUND:
            log_and_telegram(f"[{sender}] ❌ @{target_user} not found")
            already_dmd.add(target_user)
        else:
            telegram_bot.stats["dms_failed"] += 1
            log_and_telegram(f"[{sender}] ❌ DM to @{target_user} failed: {result}")

        # Check for challenges mid-session
        challenge = detect_challenge(driver)
        if challenge != ChallengeType.NONE:
            log_and_telegram(f"[{sender}] 🔒 Challenge detected mid-session: {challenge.value}")
            telegram_bot.send_challenge_alert(sender, challenge.value)

            if challenge == ChallengeType.TWO_FACTOR:
                challenge_timeout = _setting_int("CHALLENGE_WAIT_TIMEOUT")
                code = telegram_bot.wait_for_code(challenge_timeout)
                if code:
                    handle_two_factor(driver, {"username": sender}, code)
                else:
                    break
            elif challenge == ChallengeType.LOCKED:
                telegram_bot.send_lockout_alert(sender, "Account locked during DM session")
                break
            else:
                challenge_timeout = _setting_int("CHALLENGE_WAIT_TIMEOUT")
                approved = telegram_bot.wait_for_approval(challenge_timeout)
                if not approved:
                    break
                driver.refresh()
                human_delay(3, 5)

        # Random delay between DMs
        if sent < max_dms and target_user != usernames[-1]:
            wait_between_dms(stop_event=stop_event)

    return sent
