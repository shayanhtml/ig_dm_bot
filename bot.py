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

from config.settings import (
    ACCOUNTS_FILE, MODELS_FILE, MESSAGES_FILE, DM_LOG_FILE,
    DM_MIN_PER_MODEL, DM_MAX_PER_MODEL,
    ACCOUNT_SWITCH_DELAY_MIN, ACCOUNT_SWITCH_DELAY_MAX,
    MODEL_SWITCH_DELAY_MIN, MODEL_SWITCH_DELAY_MAX,
    CHALLENGE_WAIT_TIMEOUT,
    LOGS_DIR,
)
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


def load_json(filepath: str) -> list | dict:
    """Load a JSON config file."""
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def load_dm_log() -> dict:
    """Load the DM log (tracks who was already messaged)."""
    if os.path.exists(DM_LOG_FILE):
        try:
            with open(DM_LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_dm_log(dm_log: dict):
    """Save the DM log back to disk."""
    with open(DM_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(dm_log, f, indent=2)


def log_and_telegram(msg: str):
    """Log a message and add it to Telegram's log buffer."""
    logger.info(msg)
    telegram_bot.add_log(msg)


def run_bot():
    """Main bot orchestration loop."""
    setup_logging()

    logger.info("=" * 60)
    logger.info("  INSTAGRAM MODEL DM BOT — STARTING")
    logger.info("=" * 60)

    # Load config
    try:
        accounts = load_json(ACCOUNTS_FILE)
        models = load_json(MODELS_FILE)
        messages = load_json(MESSAGES_FILE)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return

    if not accounts:
        logger.error("No accounts configured in accounts.json")
        return
    if not models:
        logger.error("No models configured in models.json")
        return
    if not messages:
        logger.error("No messages configured in messages.json")
        return

    logger.info(f"Loaded {len(accounts)} accounts, {len(models)} models, {len(messages)} message templates")

    # Load DM log and build 24-hour exclusion set
    from datetime import datetime, timedelta
    dm_log = load_dm_log()
    already_dmd = set()
    cutoff_time = datetime.now() - timedelta(hours=24)
    
    for user_dmd, data in dm_log.items():
        try:
            timestamp_str = data.get("timestamp", "")
            if not timestamp_str:
                already_dmd.add(user_dmd)
                continue
            
            # support fromisoformat compatibility
            dmd_time = datetime.fromisoformat(timestamp_str)
            if dmd_time > cutoff_time:
                already_dmd.add(user_dmd)
        except (ValueError, TypeError):
            # Fallback for old/corrupted formats
            already_dmd.add(user_dmd)

    # Start Telegram
    telegram_bot.start_polling()
    telegram_bot.send_startup()
    telegram_bot.stats["status"] = "Running"

    total_dms_sent = 0
    total_models_done = 0

    try:
        for account in accounts:
            username = account["username"]
            log_and_telegram(f"━━━ Switching to account: @{username} ━━━")
            telegram_bot.stats["current_account"] = username
            telegram_bot.stats["accounts_used"] += 1

            # Create browser
            driver = None
            try:
                driver = create_driver()
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

                # Process each model
                for model_username in models:
                    if not telegram_bot._polling:
                        log_and_telegram("🛑 Stop requested, finishing up...")
                        break

                    log_and_telegram(f"🎯 Targeting model: @{model_username}")
                    telegram_bot.stats["current_model"] = model_username

                    dms_for_model = _process_model(
                        driver, account, model_username, messages, dm_log, already_dmd
                    )

                    total_dms_sent += dms_for_model
                    telegram_bot.stats["dms_sent"] = total_dms_sent

                    if dms_for_model > 0:
                        total_models_done += 1
                        telegram_bot.stats["models_processed"] = total_models_done
                        telegram_bot.send_model_complete(model_username, dms_for_model)

                    # Save DM log after each model
                    save_dm_log(dm_log)

                    # Check if still logged in
                    if not is_logged_in(driver):
                        log_and_telegram(f"⚠️ Lost login for @{username} during model processing")
                        break

                    # Delay before next model
                    delay = random.uniform(MODEL_SWITCH_DELAY_MIN, MODEL_SWITCH_DELAY_MAX)
                    log_and_telegram(f"⏳ Waiting {delay:.0f}s before next model...")
                    time.sleep(delay)

                # Refresh cookies after session
                refresh_cookies(driver, username)

            except Exception as e:
                log_and_telegram(f"❌ Error with @{username}: {e}")
                telegram_bot.send_error(str(e))
            finally:
                close_driver(driver)

            # Delay before switching accounts
            if account != accounts[-1]:
                delay = random.uniform(ACCOUNT_SWITCH_DELAY_MIN, ACCOUNT_SWITCH_DELAY_MAX)
                log_and_telegram(f"⏳ Waiting {delay:.0f}s before switching accounts...")
                time.sleep(delay)

    except KeyboardInterrupt:
        log_and_telegram("🛑 Bot stopped by user (Ctrl+C)")
    except Exception as e:
        log_and_telegram(f"❌ Fatal error: {e}")
        telegram_bot.send_error(str(e))
    finally:
        save_dm_log(dm_log)
        telegram_bot.send_session_complete(total_dms_sent, total_models_done)
        telegram_bot.stats["status"] = "Stopped"
        telegram_bot.stop_polling()

    logger.info("=" * 60)
    logger.info(f"  SESSION COMPLETE — {total_dms_sent} DMs sent, {total_models_done} models done")
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

    if challenge == ChallengeType.TWO_FACTOR:
        # Wait for employee to send code via Telegram
        code = telegram_bot.wait_for_code(CHALLENGE_WAIT_TIMEOUT)
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
                    approved = telegram_bot.wait_for_approval(CHALLENGE_WAIT_TIMEOUT)
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
        approved = telegram_bot.wait_for_approval(CHALLENGE_WAIT_TIMEOUT)
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
    messages: list, dm_log: dict, already_dmd: set
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
    dm_target = random.randint(DM_MIN_PER_MODEL, DM_MAX_PER_MODEL)
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
        if dms_sent >= dm_target or post_dms_sent >= post_dm_target:
            break

        age_label = f"{post['age_hours']}h" if post['age_hours'] < 999 else "unknown"

        # Skip posts older than 24 hours
        if post['age_hours'] > 24:
            log_and_telegram(f"[{username}] ⏭️ Skipping post ({age_label} old) — too old: {post['url'][-20:]}")
            continue

        log_and_telegram(f"[{username}] Scraping post ({age_label} old): {post['url'][-20:]}")

        interactors = get_post_interactors(driver, post["url"], already_dmd)

        if interactors:
            remaining_for_posts = post_dm_target - post_dms_sent
            remaining = min(remaining_for_posts, dm_target - dms_sent)
            
            targets = interactors[:remaining]
            sent = _dm_list(driver, targets, messages, dm_log, already_dmd, remaining, username, model_username)
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
            sent = _dm_list(driver, followers, messages, dm_log, already_dmd, remaining, username, model_username)
            dms_sent += sent

    log_and_telegram(f"[{username}] ✅ Completed @{model_username}: {dms_sent}/{dm_target} DMs sent")
    return dms_sent


def _dm_list(
    driver, usernames: list, messages: list,
    dm_log: dict, already_dmd: set,
    max_dms: int, sender: str, model: str
) -> int:
    """
    Send DMs to a list of usernames.
    
    Returns number of DMs successfully sent.
    """
    sent = 0

    for target_user in usernames:
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
            dm_log[target_user] = {
                "sent_by": sender,
                "model": model,
                "message": message[:50],
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            log_and_telegram(f"[{sender}] ✅ DM sent to @{target_user} ({sent}/{max_dms})")
        elif result == DMResult.CANT_MESSAGE:
            log_and_telegram(f"[{sender}] ⚠️ Can't message @{target_user}")
            already_dmd.add(target_user)  # Don't retry
            dm_log[target_user] = {"status": "cant_message", "sent_by": sender}
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
                code = telegram_bot.wait_for_code(CHALLENGE_WAIT_TIMEOUT)
                if code:
                    handle_two_factor(driver, {"username": sender}, code)
                else:
                    break
            elif challenge == ChallengeType.LOCKED:
                telegram_bot.send_lockout_alert(sender, "Account locked during DM session")
                break
            else:
                approved = telegram_bot.wait_for_approval(CHALLENGE_WAIT_TIMEOUT)
                if not approved:
                    break
                driver.refresh()
                human_delay(3, 5)

        # Random delay between DMs
        if sent < max_dms and target_user != usernames[-1]:
            wait_between_dms()

    return sent
