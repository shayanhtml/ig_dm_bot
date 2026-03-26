"""
Telegram bot for alerting employees and receiving 2FA codes / approvals.
Runs a polling loop in a background thread.
"""
import time
import threading
import queue
import logging
import requests
from collections import deque
from datetime import datetime

from config.settings import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("model_dm_bot")


class TelegramBot:
    """
    Telegram bot that:
    - Sends alerts to employees (challenges, status, errors)
    - Polls for employee responses (2FA codes, approvals)
    - Provides /status command support
    """

    def __init__(self, token: str = None, chat_id: str = None):
        self.token = token or TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or TELEGRAM_CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.token}"

        self.last_update_id = 0
        self.code_queue = queue.Queue()        # Queue for 2FA codes
        self.approval_queue = queue.Queue()    # Queue for manual approvals
        self.logs = deque(maxlen=20)           # Recent log lines
        self.start_time = time.time()
        self.stats = {
            "accounts_used": 0,
            "models_processed": 0,
            "dms_sent": 0,
            "dms_failed": 0,
            "current_account": "—",
            "current_model": "—",
            "status": "Initializing",
        }

        self._polling = False
        self._poll_thread = None

    # ──────────────────────────────────────────
    # Sending Messages
    # ──────────────────────────────────────────

    def send(self, message: str):
        """Send a message to the configured chat."""
        if not self.token or not self.chat_id:
            return
        try:
            requests.post(
                f"{self.base_url}/sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "Markdown",
                },
                timeout=10,
            )
        except Exception as e:
            logger.error(f"[Telegram] Failed to send: {e}")

    def send_startup(self):
        """Send bot startup notification."""
        self.send(
            "🚀 *MODEL DM BOT STARTED*\n\n"
            f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}\n"
            "Use `/status` for current state\n"
            "Use `/stop` to request stop"
        )

    def send_challenge_alert(self, account: str, challenge_type: str):
        """Alert employees that an account needs human intervention."""
        self.send(
            f"⚠️ *CHALLENGE DETECTED*\n\n"
            f"👤 Account: `{account}`\n"
            f"🔒 Type: `{challenge_type}`\n\n"
            f"*Actions:*\n"
            f"• Reply `/code 123456` to submit a 2FA code\n"
            f"• Reply `/approve` after manual resolution\n"
            f"• Reply `/skip` to skip this account"
        )

    def send_lockout_alert(self, account: str, description: str):
        """Alert employees that an account is locked out."""
        self.send(
            f"🚨 *ACCOUNT LOCKED*\n\n"
            f"👤 Account: `{account}`\n"
            f"📝 Details: {description}\n\n"
            f"Bot will skip this account and continue.\n"
            f"Please unlock manually and reply `/approve` when ready."
        )

    def send_progress(self, account: str, model: str, dms_sent: int, total_target: int):
        """Send a progress update."""
        self.send(
            f"📊 *PROGRESS*\n\n"
            f"👤 Account: `{account}`\n"
            f"🎯 Model: `@{model}`\n"
            f"✉️ DMs: {dms_sent}/{total_target}\n"
            f"⏰ Time: {datetime.now().strftime('%H:%M:%S')}"
        )

    def send_model_complete(self, model: str, dms_sent: int):
        """Notify that a model target is complete."""
        self.send(
            f"✅ *MODEL COMPLETE*\n\n"
            f"🎯 Model: `@{model}`\n"
            f"✉️ DMs sent: {dms_sent}"
        )

    def send_session_complete(self, total_dms: int, models_done: int):
        """Notify that the entire bot session is done."""
        self.send(
            f"🏁 *SESSION COMPLETE*\n\n"
            f"✉️ Total DMs: {total_dms}\n"
            f"🎯 Models: {models_done}\n"
            f"⏰ Duration: {self._uptime()}"
        )

    def send_error(self, error: str):
        """Send an error alert."""
        self.send(f"❌ *ERROR*\n\n```\n{error[:500]}\n```")

    # ──────────────────────────────────────────
    # Receiving Responses
    # ──────────────────────────────────────────

    def wait_for_code(self, timeout: int = 300) -> str:
        """
        Wait for an employee to send a 2FA code via Telegram.
        
        Returns:
            The code string, or empty string if timeout
        """
        logger.info(f"[Telegram] Waiting up to {timeout}s for 2FA code...")
        try:
            code = self.code_queue.get(timeout=timeout)
            return code
        except queue.Empty:
            logger.warning("[Telegram] Timed out waiting for 2FA code")
            return ""

    def wait_for_approval(self, timeout: int = 300) -> bool:
        """
        Wait for an employee to approve a manual action.
        
        Returns:
            True if approved, False if timed out or skipped
        """
        logger.info(f"[Telegram] Waiting up to {timeout}s for approval...")
        try:
            result = self.approval_queue.get(timeout=timeout)
            return result == "approve"
        except queue.Empty:
            logger.warning("[Telegram] Timed out waiting for approval")
            return False

    # ──────────────────────────────────────────
    # Polling Thread
    # ──────────────────────────────────────────

    def start_polling(self):
        """Start the background polling thread."""
        if self._polling:
            return
        self._polling = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        logger.info("[Telegram] Polling thread started")

    def stop_polling(self):
        """Stop the background polling thread."""
        self._polling = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        logger.info("[Telegram] Polling thread stopped")

    def _poll_loop(self):
        """Background loop that polls Telegram for new messages."""
        while self._polling:
            try:
                url = f"{self.base_url}/getUpdates"
                params = {
                    "offset": self.last_update_id + 1,
                    "timeout": 5,
                }
                r = requests.get(url, params=params, timeout=15)
                data = r.json()

                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        self.last_update_id = update["update_id"]
                        self._handle_update(update)

            except Exception as e:
                logger.debug(f"[Telegram] Poll error: {e}")

            time.sleep(2)

    def _handle_update(self, update: dict):
        """Process a single Telegram update."""
        message = update.get("message", {})
        text = message.get("text", "").strip()
        sender_id = str(message.get("chat", {}).get("id", ""))

        if sender_id != self.chat_id:
            return

        if not text:
            return

        text_lower = text.lower()

        # /code 123456
        if text_lower.startswith("/code "):
            code = text[6:].strip()
            if code and code.isdigit() and len(code) == 6:
                self.code_queue.put(code)
                self.send(f"✅ Code `{code}` received. Processing...")
                logger.info(f"[Telegram] Received 2FA code: {code}")
            else:
                self.send("❌ Invalid code. Send `/code` followed by a 6-digit number.")

        # /approve
        elif text_lower == "/approve":
            self.approval_queue.put("approve")
            self.send("✅ Approval received. Resuming bot...")
            logger.info("[Telegram] Received approval")

        # /skip
        elif text_lower == "/skip":
            self.approval_queue.put("skip")
            self.code_queue.put("")  # Unblock code wait too
            self.send("⏭️ Skipping current account...")
            logger.info("[Telegram] Received skip command")

        # /status
        elif text_lower == "/status":
            self.send(self._get_status_text())

        # /stop
        elif text_lower == "/stop":
            self.send("🛑 Stop requested. Bot will finish current DM and stop.")
            self._polling = False

    def _get_status_text(self) -> str:
        """Generate a status message."""
        return (
            f"🤖 *MODEL DM BOT STATUS*\n\n"
            f"🟢 Status: `{self.stats['status']}`\n"
            f"⏱️ Uptime: `{self._uptime()}`\n\n"
            f"👤 Account: `{self.stats['current_account']}`\n"
            f"🎯 Model: `{self.stats['current_model']}`\n\n"
            f"✉️ DMs Sent: `{self.stats['dms_sent']}`\n"
            f"❌ DMs Failed: `{self.stats['dms_failed']}`\n"
            f"🎯 Models Done: `{self.stats['models_processed']}`\n\n"
            f"📋 *Recent Logs:*\n"
            f"```\n" + "\n".join(list(self.logs)[-5:]) + "\n```"
        )

    def _uptime(self) -> str:
        """Get formatted uptime string."""
        elapsed = time.time() - self.start_time
        h, r = divmod(elapsed, 3600)
        m, s = divmod(r, 60)
        return f"{int(h)}h {int(m)}m {int(s)}s"

    def add_log(self, message: str):
        """Add a log line to the recent logs buffer."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {message}")


# Global instance
telegram_bot = TelegramBot()
