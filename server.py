"""
Flask server — runs the DM bot in a 24/7 loop with cooldown.
Provides a live dashboard to monitor status.
"""
import sys
import os
import json
import time
import random
import threading
import logging
from functools import wraps
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, render_template, request, redirect, session, url_for
from bot import run_bot, setup_logging, force_stop_active_sessions
from config import database
from config.database import get_setting
from config.settings import COOLDOWN_MIN, COOLDOWN_MAX

# ── Config ──
BOT_LOOP_ENABLED = True

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "beyinstabot-local-secret")
app.jinja_env.auto_reload = True
logger = logging.getLogger("model_dm_bot")

# ── Shared State ──
bot_state = {
    "status": "idle",           # idle | running | cooldown | stopped
    "current_session": 0,
    "total_sessions": 0,
    "last_run_start": None,
    "last_run_end": None,
    "next_run": None,
    "total_dms_all_time": 0,
    "started_by": "",
    "started_by_role": "employee",
    "errors": [],
    "log_lines": [],
}
bot_thread = None
bot_thread_lock = threading.Lock()
stop_event = threading.Event()


# ── Authentication ──
# (Auth DB is now handled natively by config.database)


def login_required(route_func):
  @wraps(route_func)
  def wrapper(*args, **kwargs):
    if session.get("authenticated"):
      return route_func(*args, **kwargs)

    if request.path.startswith("/api/"):
      return jsonify({"success": False, "error": "Unauthorized"}), 401

    return redirect(url_for("login"))

  return wrapper


def _current_user_context():
  return {
    "username": session.get("username", ""),
    "role": session.get("role", "employee"),
  }


def _is_master():
  return session.get("role") == "master"


def master_required(route_func):
  @wraps(route_func)
  def wrapper(*args, **kwargs):
    if _is_master():
      return route_func(*args, **kwargs)

    if request.path.startswith("/api/"):
      return jsonify({"success": False, "error": "Master access required"}), 403

    return redirect(url_for("dashboard"))

  return wrapper


def _log_actor_action(action, target_type="", target_value="", details=None, employees_only=True):
  """Append an actor action to audit log. By default only logs employee actions."""
  user_ctx = _current_user_context()
  actor_username = user_ctx.get("username", "")
  actor_role = user_ctx.get("role", "employee")

  if not actor_username:
    return
  if employees_only and actor_role != "employee":
    return

  try:
    database.log_activity(
      actor_username,
      actor_role,
      action,
      target_type=target_type,
      target_value=target_value,
      details=details,
    )
  except Exception as e:
    logger.debug(f"Activity log failed for {actor_username}: {e}")


# ── Dashboard HTML ──
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Instagram DM Bot — Dashboard</title>
<meta http-equiv="refresh" content="10">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: #0a0a0f;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 2rem;
  }
  .container { max-width: 900px; margin: 0 auto; }
  h1 {
    text-align: center;
    font-size: 2rem;
    background: linear-gradient(135deg, #f09433, #e6683c, #dc2743, #cc2366, #bc1888);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    margin-bottom: 2rem;
  }
  .status-bar {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 1rem;
    margin-bottom: 2rem;
  }
  .status-badge {
    padding: 0.5rem 1.5rem;
    border-radius: 50px;
    font-weight: 700;
    font-size: 0.9rem;
    text-transform: uppercase;
    letter-spacing: 1px;
  }
  .status-idle { background: #1a1a2e; color: #888; border: 1px solid #333; }
  .status-running { background: #0d3320; color: #4ade80; border: 1px solid #166534; animation: pulse 2s infinite; }
  .status-cooldown { background: #1e1b3a; color: #a78bfa; border: 1px solid #4c1d95; }
  .status-stopped { background: #2d1215; color: #f87171; border: 1px solid #7f1d1d; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.7; } }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin-bottom: 2rem;
  }
  .card {
    background: #12121a;
    border: 1px solid #1e1e2e;
    border-radius: 12px;
    padding: 1.5rem;
    text-align: center;
  }
  .card .value {
    font-size: 2rem;
    font-weight: 800;
    color: #fff;
    margin-bottom: 0.3rem;
  }
  .card .label { font-size: 0.8rem; color: #888; text-transform: uppercase; letter-spacing: 1px; }
  .logs {
    background: #0d0d14;
    border: 1px solid #1e1e2e;
    border-radius: 12px;
    padding: 1.5rem;
    max-height: 400px;
    overflow-y: auto;
    font-family: 'Cascadia Code', 'Fira Code', monospace;
    font-size: 0.78rem;
    line-height: 1.6;
  }
  .logs .line { color: #6b7280; }
  .logs .line.info { color: #9ca3af; }
  .logs .line.success { color: #4ade80; }
  .logs .line.error { color: #f87171; }
  .logs .line.warn { color: #fbbf24; }
  .time-info {
    text-align: center;
    color: #555;
    font-size: 0.85rem;
    margin-bottom: 1.5rem;
  }
  .controls {
    display: flex;
    justify-content: center;
    gap: 1rem;
    margin-bottom: 2rem;
  }
  .btn {
    padding: 0.6rem 2rem;
    border: none;
    border-radius: 8px;
    font-weight: 600;
    cursor: pointer;
    font-size: 0.9rem;
    transition: all 0.2s;
  }
  .btn-stop { background: #7f1d1d; color: #fca5a5; }
  .btn-stop:hover { background: #991b1b; }
  .btn-start { background: #14532d; color: #86efac; }
  .btn-start:hover { background: #166534; }
</style>
</head>
<body>
<div class="container">
  <h1>🤖 Instagram DM Bot</h1>

  <div class="status-bar">
    <span class="status-badge status-{{ state.status }}">{{ state.status }}</span>
  </div>

  <div class="time-info">
    {% if state.next_run and state.status == 'cooldown' %}
      ⏳ Next run in: <strong>{{ state.next_run }}</strong>
    {% elif state.last_run_end %}
      Last completed: {{ state.last_run_end }}
    {% else %}
      Waiting to start...
    {% endif %}
  </div>

  <div class="controls">
    {% if state.status == 'stopped' %}
      <a href="/start"><button class="btn btn-start">▶ Start Loop</button></a>
    {% else %}
      <a href="/stop"><button class="btn btn-stop">■ Stop</button></a>
    {% endif %}
  </div>

  <div class="grid">
    <div class="card">
      <div class="value">{{ state.total_sessions }}</div>
      <div class="label">Sessions Run</div>
    </div>
    <div class="card">
      <div class="value">{{ state.total_dms_all_time }}</div>
      <div class="label">Total DMs Sent</div>
    </div>
    <div class="card">
      <div class="value">{{ cooldown_range }}</div>
      <div class="label">Cooldown</div>
    </div>
    <div class="card">
      <div class="value">{{ dm_log_count }}</div>
      <div class="label">Users Reached</div>
    </div>
  </div>

  <h2 style="margin-bottom:1rem;font-size:1rem;color:#888;">📜 Recent Logs</h2>
  <div class="logs">
    {% for line in state.log_lines[-50:]|reverse %}
      <div class="line {% if '✅' in line or 'successful' in line %}success{% elif '❌' in line or 'ERROR' in line %}error{% elif '⚠️' in line or 'WARNING' in line %}warn{% else %}info{% endif %}">{{ line }}</div>
    {% endfor %}
    {% if not state.log_lines %}
      <div class="line">No logs yet. Start the bot to see activity.</div>
    {% endif %}
  </div>
</div>
</body>
</html>
"""


# ── Log Capture Handler ──
class DashboardLogHandler(logging.Handler):
    """Captures log lines into bot_state for the dashboard."""
    def emit(self, record):
        msg = self.format(record)
        bot_state["log_lines"].append(msg)
        # Keep only last 200 lines
        if len(bot_state["log_lines"]) > 200:
            bot_state["log_lines"] = bot_state["log_lines"][-200:]


def _ensure_dashboard_log_handler():
    """Attach a single dashboard log handler instance to avoid duplicate log lines."""
    model_logger = logging.getLogger("model_dm_bot")
    for handler in model_logger.handlers:
        if isinstance(handler, DashboardLogHandler):
            return

    dash_handler = DashboardLogHandler()
    dash_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    model_logger.addHandler(dash_handler)


# ── Bot Loop ──
def bot_loop():
    """Run the bot in a continuous loop with cooldown between sessions."""
    global bot_state

    setup_logging()
    _ensure_dashboard_log_handler()

    session_num = 0

    while not stop_event.is_set():
        session_num += 1
        bot_state["status"] = "running"
        bot_state["current_session"] = session_num
        bot_state["last_run_start"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"🔄 SESSION #{session_num} STARTING")

        started_by = bot_state.get("started_by", "")
        started_by_role = bot_state.get("started_by_role", "employee")
        account_owner = None if started_by_role == "master" else started_by
        if started_by:
          logger.info(
            f"Session operator: @{started_by} ({started_by_role})"
          )

        try:
          run_bot(stop_event=stop_event, account_owner=account_owner)
        except Exception as e:
            logger.error(f"Session #{session_num} crashed: {e}")
            bot_state["errors"].append(f"Session {session_num}: {e}")

        bot_state["total_sessions"] = session_num
        bot_state["last_run_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Count total DMs from database log table
        try:
            dm_log = database.get_dm_logs()
            bot_state["total_dms_all_time"] = sum(
                1 for ts in dm_log.values()
                if ts
            )
        except Exception:
            pass

        if stop_event.is_set():
            break

        # Cooldown — pick random duration
        bot_state["status"] = "cooldown"
        cooldown_min = int(get_setting("COOLDOWN_MIN", COOLDOWN_MIN))
        cooldown_max = int(get_setting("COOLDOWN_MAX", COOLDOWN_MAX))
        if cooldown_max < cooldown_min:
            cooldown_min, cooldown_max = cooldown_max, cooldown_min

        cooldown_minutes = random.randint(cooldown_min, cooldown_max)
        cooldown_end = datetime.now() + timedelta(minutes=cooldown_minutes)
        logger.info(f"💤 Cooldown: {cooldown_minutes} minutes. Next run at {cooldown_end.strftime('%H:%M:%S')}")

        while datetime.now() < cooldown_end and not stop_event.is_set():
            remaining = cooldown_end - datetime.now()
            mins = int(remaining.total_seconds() // 60)
            secs = int(remaining.total_seconds() % 60)
            bot_state["next_run"] = f"{mins}m {secs}s"
            time.sleep(5)

    bot_state["status"] = "stopped"
    logger.info("🛑 Bot loop stopped.")


# ── Flask Routes ──
@app.route("/login", methods=["GET", "POST"])
def login():
  if session.get("authenticated"):
    return redirect(url_for("dashboard"))

  error = None
  if request.method == "POST":
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    auth_user = database.authenticate_user(username, password)
    if auth_user:
      session.clear()
      session.permanent = True
      session["authenticated"] = True
      session["username"] = auth_user["username"]
      session["role"] = auth_user["role"]

      _log_actor_action(
        "login",
        target_type="auth",
        target_value="dashboard",
        details={"ip": request.remote_addr or ""},
        employees_only=True,
      )
      return redirect(url_for("dashboard"))

    error = "Invalid username or password"

  return render_template("login.html", error=error)


@app.route("/logout")
def logout():
  session.clear()
  return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    dm_log = database.get_dm_logs()
    dm_log_count = len(dm_log)
    user_ctx = _current_user_context()

    settings_cache = database.get_all_settings()
    cooldown_min = settings_cache.get("COOLDOWN_MIN", 25)
    cooldown_max = settings_cache.get("COOLDOWN_MAX", 40)
    return render_template(
        "index.html",
        state=bot_state,
        cooldown_range=f"{cooldown_min}-{cooldown_max} min",
        dm_log_count=dm_log_count,
        current_user=user_ctx,
    )

# ── API ──
@app.route("/api/config", methods=["GET"])
@login_required
def api_get_config():
    """Retrieve all configuration chunks to populate the UI."""
    user_ctx = _current_user_context()
    data = {
        "accounts": [],
      "accounts_queue": [],
        "models": [],
        "messages": [],
        "settings": {},
        "users": [],
        "current_user": user_ctx,
    }
    try:
        if user_ctx["role"] == "master":
            data["accounts"] = database.get_accounts(include_all=True)
            data["users"] = database.get_users()
        else:
            data["accounts"] = database.get_accounts(owner_username=user_ctx["username"])

        queue_rows = database.get_accounts(include_all=True)
        data["accounts_queue"] = [
          {
            "username": str(acc.get("username", "")).strip(),
            "owner_username": str(acc.get("owner_username", "")).strip() or "master",
          }
          for acc in queue_rows
          if str(acc.get("username", "")).strip()
        ]

        data["models"] = database.get_models()
        data["messages"] = database.get_messages()
        data["settings"] = database.get_all_settings()
    except Exception as e:
        logger.error(f"Error reading config API: {e}")
    return jsonify(data)


@app.route("/api/accounts/queue", methods=["GET"])
@login_required
def api_accounts_queue():
    """Return sanitized cross-employee IG account queue."""
    try:
        queue_rows = database.get_accounts(include_all=True)
        accounts = [
            {
                "username": str(acc.get("username", "")).strip(),
                "owner_username": str(acc.get("owner_username", "")).strip() or "master",
            }
            for acc in queue_rows
            if str(acc.get("username", "")).strip()
        ]
        return jsonify({"success": True, "accounts": accounts})
    except Exception as e:
        logger.error(f"Error reading accounts queue: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/config/<target>", methods=["POST"])
@login_required
def api_save_config(target):
    """Save configuration changes back to database."""
    try:
        payload = request.get_json(silent=True)
        if payload is None:
            payload = {} if target in ("settings", "model_message_map") else []
        user_ctx = _current_user_context()
        is_master = user_ctx["role"] == "master"

        if target == "settings":
            if not is_master:
                return jsonify({"success": False, "error": "Only master can update settings"}), 403
            database.save_settings(payload)
        elif target == "accounts":
            if not isinstance(payload, list):
                return jsonify({"success": False, "error": "Accounts payload must be a list"}), 400

            clean_accounts = []
            for idx, raw_acc in enumerate(payload):
                if not isinstance(raw_acc, dict):
                    return jsonify({"success": False, "error": f"Invalid account entry at index {idx}"}), 400

                username = str(raw_acc.get("username", "")).strip()
                if not username:
                    continue

                password = str(raw_acc.get("password", "")).strip()
                if not password:
                    return jsonify({
                        "success": False,
                        "error": f"Password is required for account '{username}'",
                    }), 400

                account_entry = {
                    "username": username,
                    "password": password,
                }
                if is_master:
                    account_entry["owner_username"] = str(raw_acc.get("owner_username", "")).strip().lower() or "master"

                clean_accounts.append(account_entry)

            if is_master:
                database.save_accounts(clean_accounts, include_all=True)
            else:
                database.save_accounts(clean_accounts, owner_username=user_ctx["username"], include_all=False)
            _log_actor_action(
              "update_accounts",
              target_type="config",
              target_value="accounts",
              details={
                "account_count": len(clean_accounts),
              },
              employees_only=True,
            )
        elif target == "models":
            if not isinstance(payload, list):
                return jsonify({"success": False, "error": "Models payload must be a list"}), 400
            clean_models = []
            seen_models = set()
            for model in payload:
                name = str(model or "").strip().lstrip("@")
                key = name.lower()
                if not name or key in seen_models:
                    continue
                seen_models.add(key)
                clean_models.append(name)
            database.save_models(payload)
            _log_actor_action(
              "update_models",
              target_type="config",
              target_value="models",
              details={
                "model_count": len(clean_models),
                "model_names": ", ".join(clean_models[:10]) + (" ..." if len(clean_models) > 10 else ""),
              },
              employees_only=True,
            )
        elif target == "messages":
            if not isinstance(payload, list):
                return jsonify({"success": False, "error": "Messages payload must be a list"}), 400
            clean_messages = [str(msg or "").strip() for msg in payload if str(msg or "").strip()]
            sample_messages = "; ".join(clean_messages[:3])
            if len(sample_messages) > 120:
                sample_messages = sample_messages[:117] + "..."
            database.save_messages(payload)
            _log_actor_action(
              "update_messages",
              target_type="config",
              target_value="messages",
              details={
                "message_count": len(clean_messages),
                "message_sample": sample_messages,
              },
              employees_only=True,
            )
        elif target == "model_message_map":
            if not isinstance(payload, dict):
                return jsonify({"success": False, "error": "MODEL_MESSAGE_MAP payload must be an object"}), 400
            actor_username = str(user_ctx.get("username") or "unknown")
            now_iso = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

            existing_settings = database.get_all_settings()
            raw_existing_map = existing_settings.get("MODEL_MESSAGE_MAP", {})
            existing_map = raw_existing_map if isinstance(raw_existing_map, dict) else {}
            raw_existing_meta = existing_settings.get("MODEL_MESSAGE_META", {})
            existing_meta = raw_existing_meta if isinstance(raw_existing_meta, dict) else {}

            normalized_map = {}
            for raw_model, raw_messages in payload.items():
                model_key = str(raw_model or "").strip().lstrip("@").lower()
                if not model_key or not isinstance(raw_messages, list):
                    continue

                clean_messages = [str(msg or "").strip() for msg in raw_messages if str(msg or "").strip()]
                if not clean_messages:
                    continue

                normalized_map[model_key] = clean_messages

            model_meta = {}
            total_messages = 0
            for model_key, clean_messages in normalized_map.items():
                total_messages += len(clean_messages)

                prev_meta = existing_meta.get(model_key, {})
                if not isinstance(prev_meta, dict):
                    prev_meta = {}

                prev_messages_raw = existing_map.get(model_key, [])
                prev_messages = (
                    [str(msg or "").strip() for msg in prev_messages_raw if str(msg or "").strip()]
                    if isinstance(prev_messages_raw, list)
                    else []
                )
                is_changed = prev_messages != clean_messages

                created_by = str(prev_meta.get("created_by") or actor_username)
                created_at = str(prev_meta.get("created_at") or now_iso)

                if is_changed:
                    updated_by = actor_username
                    updated_at = now_iso
                else:
                    updated_by = str(prev_meta.get("updated_by") or created_by)
                    updated_at = str(prev_meta.get("updated_at") or created_at)

                model_meta[model_key] = {
                    "created_by": created_by,
                    "created_at": created_at,
                    "updated_by": updated_by,
                    "updated_at": updated_at,
                }

            model_names = list(normalized_map.keys())
            database.save_settings({
                "MODEL_MESSAGE_MAP": normalized_map,
                "MODEL_MESSAGE_META": model_meta,
            })
            _log_actor_action(
                "update_model_message_map",
                target_type="config",
                target_value="MODEL_MESSAGE_MAP",
                details={
                    "model_entry_count": len(model_names),
                    "message_count": total_messages,
                    "model_names": ", ".join(model_names[:10]) + (" ..." if len(model_names) > 10 else ""),
                },
                    employees_only=False,
            )
            return jsonify({
                "success": True,
                "model_message_map": normalized_map,
                "model_message_meta": model_meta,
            })
        else:
            return jsonify({"success": False, "error": "Invalid target"}), 400

        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving {target}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/users", methods=["POST"])
@login_required
@master_required
def api_create_user():
    """Create a new dashboard user (master-only)."""
    try:
        payload = request.get_json() or {}
        user = database.create_user(
            payload.get("username", ""),
            payload.get("password", ""),
            payload.get("role", "employee"),
        )
        return jsonify({"success": True, "user": user})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/users/<username>/password", methods=["POST"])
@login_required
@master_required
def api_update_user_password(username):
    """Reset password for a dashboard user (master-only)."""
    try:
        payload = request.get_json() or {}
        ok = database.update_user_password(username, payload.get("password", ""))
        if not ok:
            return jsonify({"success": False, "error": "User not found"}), 404
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error updating user password: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/users/<username>", methods=["PUT", "POST"])
@app.route("/api/users/<username>/update", methods=["POST"])
@login_required
@master_required
def api_update_user_credentials(username):
    """Update dashboard username and/or password (master-only)."""
    try:
        payload = request.get_json() or {}
        result = database.update_user_credentials(
            username,
            new_username=payload.get("username"),
            new_password=payload.get("password"),
        )
        if not result:
            return jsonify({"success": False, "error": "User not found"}), 404

        # Keep current session consistent if the active master renamed this account.
        if session.get("username", "").strip().lower() == result["old_username"]:
            session["username"] = result["username"]

        return jsonify({"success": True, "user": result})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error updating user credentials: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/users/<username>", methods=["DELETE"])
@login_required
@master_required
def api_delete_user(username):
    """Delete a dashboard user (master-only)."""
    try:
        deleted = database.delete_user(username)
        if not deleted:
            return jsonify({"success": False, "error": "User not found"}), 404
        return jsonify({"success": True})
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception as e:
        logger.error(f"Error deleting user: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/cookies/<username>", methods=["GET"])
@login_required
def api_get_cookies(username):
    """Retrieve raw cookies JSON for an account if it exists."""
    try:
        user_ctx = _current_user_context()
        if not database.user_can_access_account(username, user_ctx["username"], user_ctx["role"]):
            return jsonify({"success": False, "error": "Forbidden"}), 403

        cookies_list = database.get_cookies(username)
        if cookies_list:
            return jsonify({"success": True, "cookies": json.dumps(cookies_list, indent=2)})
        return jsonify({"success": True, "cookies": ""})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/cookies/<username>", methods=["POST"])
@login_required
def api_save_cookies(username):
    """Save raw cookies JSON directly to the database."""
    try:
      user_ctx = _current_user_context()
      if not database.user_can_access_account(username, user_ctx["username"], user_ctx["role"]):
        return jsonify({"success": False, "error": "Forbidden"}), 403

      payload = request.get_json()
      cookies_str = payload.get("cookies", "")
      cookie_count = 0

      if not cookies_str.strip():
        database.save_cookies(username, [])
      else:
        cookies_list = json.loads(cookies_str)
        database.save_cookies(username, cookies_list)
        cookie_count = len(cookies_list) if isinstance(cookies_list, list) else 1

      _log_actor_action(
        "save_cookies",
        target_type="account",
        target_value=username,
        details={
          "cookie_count": cookie_count,
          "cleared": cookie_count == 0,
        },
        employees_only=True,
      )

      return jsonify({"success": True})
    except json.JSONDecodeError:
        return jsonify({"success": False, "error": "Invalid JSON format"}), 400
    except Exception as e:
        logger.error(f"Error saving cookies for {username}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/activity/employee", methods=["GET"])
@login_required
@master_required
def api_employee_activity():
    """Get recent employee actions for the master dashboard."""
    try:
        limit_raw = request.args.get("limit", "150")
        try:
            limit = int(limit_raw)
        except Exception:
            limit = 150

        logs = database.get_activity_logs(limit=limit, employees_only=True)
        return jsonify({"success": True, "logs": logs})
    except Exception as e:
        logger.error(f"Error loading employee activity: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/activity/all", methods=["GET"])
@login_required
@master_required
def api_all_activity():
    """Get all dashboard activity for the last N hours (default: 24)."""
    try:
        limit_raw = request.args.get("limit", "500")
        hours_raw = request.args.get("hours", "24")

        try:
            limit = int(limit_raw)
        except Exception:
            limit = 500

        try:
            hours = int(hours_raw)
        except Exception:
            hours = 24

        logs = database.get_activity_logs_recent_hours(hours=hours, limit=limit, employees_only=False)

        day_counts = {}
        for row in logs:
            day_key = str(row.get("created_at") or "").strip()[:10] or "-"
            day_counts[day_key] = day_counts.get(day_key, 0) + 1

        by_day = [
            {"day": day, "count": day_counts[day]}
            for day in sorted(day_counts.keys(), reverse=True)
        ]

        return jsonify({
            "success": True,
            "logs": logs,
            "by_day": by_day,
            "hours": hours,
        })
    except Exception as e:
        logger.error(f"Error loading all activity: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/status")
@login_required
@master_required
def api_status():
    return jsonify(bot_state)


@app.after_request
def add_no_cache_headers(response):
    """Prevent stale dashboard/template content in browser cache."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.route("/start")
@login_required
def start_bot():
    global bot_thread
    user_ctx = _current_user_context()
    with bot_thread_lock:
        if bot_thread and bot_thread.is_alive():
            logger.info("Start requested but bot loop is already running.")
            return "<script>window.location='/'</script>"

        stop_event.clear()
        bot_state["started_by"] = user_ctx["username"]
        bot_state["started_by_role"] = user_ctx["role"]
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()
        bot_state["status"] = "running"
        _log_actor_action(
          "start_bot",
          target_type="runtime",
          target_value="bot",
          details={"status": "running"},
          employees_only=True,
        )
    return "<script>window.location='/'</script>"


@app.route("/stop")
@login_required
def stop_bot():
    global bot_thread
    stop_event.set()
    force_stop_active_sessions()
    with bot_thread_lock:
        if bot_thread and bot_thread.is_alive():
            bot_thread.join(timeout=10)
    bot_state["status"] = "stopped"
    bot_state["started_by"] = ""
    bot_state["started_by_role"] = "employee"
    _log_actor_action(
      "stop_bot",
      target_type="runtime",
      target_value="bot",
      details={"status": "stopped"},
      employees_only=True,
    )
    return "<script>window.location='/'</script>"


# Main
if __name__ == "__main__":
    database.init_db()

    print("=" * 60)
    print("  INSTAGRAM DM BOT - FLASK SERVER")
    print("=" * 60)
    print("  Dashboard: http://localhost:5000")
    print(f"  System Booting...")
    print("=" * 60)
    print()

    # Bot will remain idle until started via the web UI dashboard.
    bot_state["status"] = "idle"

    app.run(host="0.0.0.0", port=5000, debug=False)
