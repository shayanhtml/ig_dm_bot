"""
Flask server — runs the DM bot in a 24/7 loop with cooldown.
Provides a live dashboard to monitor status.
"""
import sys
import os
import json
import time
import random
import sqlite3
import threading
import logging
from functools import wraps
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, render_template, request, redirect, session, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from bot import run_bot, setup_logging, load_dm_log
from config.settings import ACCOUNTS_FILE, MODELS_FILE, MESSAGES_FILE, DM_LOG_FILE, SETTINGS_FILE, COOLDOWN_MIN, COOLDOWN_MAX, COOKIES_DIR

# ── Config ──
BOT_LOOP_ENABLED = True
AUTH_DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "auth.db")
AUTH_DEFAULT_USERNAME = "beyinstabot"
AUTH_DEFAULT_PASSWORD = "#beymedia!"

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
    "errors": [],
    "log_lines": [],
}
bot_thread = None
stop_event = threading.Event()


# ── Authentication ──
def get_auth_db_connection():
  conn = sqlite3.connect(AUTH_DB_FILE)
  conn.row_factory = sqlite3.Row
  return conn


def init_auth_db():
  """Create auth DB and ensure the default account exists."""
  os.makedirs(os.path.dirname(AUTH_DB_FILE), exist_ok=True)
  conn = get_auth_db_connection()
  try:
    conn.execute(
      """
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL
      )
      """
    )

    password_hash = generate_password_hash(AUTH_DEFAULT_PASSWORD)
    existing = conn.execute(
      "SELECT id FROM users WHERE username = ?",
      (AUTH_DEFAULT_USERNAME,),
    ).fetchone()

    if existing:
      conn.execute(
        "UPDATE users SET password_hash = ? WHERE username = ?",
        (password_hash, AUTH_DEFAULT_USERNAME),
      )
    else:
      conn.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)",
        (AUTH_DEFAULT_USERNAME, password_hash),
      )

    conn.commit()
  finally:
    conn.close()


def is_valid_login(username: str, password: str) -> bool:
  conn = get_auth_db_connection()
  try:
    row = conn.execute(
      "SELECT password_hash FROM users WHERE username = ?",
      (username,),
    ).fetchone()
  finally:
    conn.close()

  if not row:
    return False
  return check_password_hash(row["password_hash"], password)


def login_required(route_func):
  @wraps(route_func)
  def wrapper(*args, **kwargs):
    if session.get("authenticated"):
      return route_func(*args, **kwargs)

    if request.path.startswith("/api/"):
      return jsonify({"success": False, "error": "Unauthorized"}), 401

    return redirect(url_for("login"))

  return wrapper


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


# ── Bot Loop ──
def bot_loop():
    """Run the bot in a continuous loop with cooldown between sessions."""
    global bot_state

    setup_logging()

    # Attach dashboard log handler
    dash_handler = DashboardLogHandler()
    dash_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger("model_dm_bot").addHandler(dash_handler)

    session_num = 0

    while not stop_event.is_set():
        session_num += 1
        bot_state["status"] = "running"
        bot_state["current_session"] = session_num
        bot_state["last_run_start"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info(f"🔄 SESSION #{session_num} STARTING")

        try:
            run_bot()
        except Exception as e:
            logger.error(f"Session #{session_num} crashed: {e}")
            bot_state["errors"].append(f"Session {session_num}: {e}")

        bot_state["total_sessions"] = session_num
        bot_state["last_run_end"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Count total DMs from log file
        try:
            dm_log = load_dm_log()
            bot_state["total_dms_all_time"] = sum(
                1 for v in dm_log.values()
                if isinstance(v, dict) and v.get("timestamp")
            )
        except Exception:
            pass

        if stop_event.is_set():
            break

        # Cooldown — pick random duration
        bot_state["status"] = "cooldown"
        cooldown_minutes = random.randint(COOLDOWN_MIN, COOLDOWN_MAX)
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

    if is_valid_login(username, password):
      session.clear()
      session.permanent = True
      session["authenticated"] = True
      session["username"] = username
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
    dm_log_count = 0
    try:
        if os.path.exists(DM_LOG_FILE):
            with open(DM_LOG_FILE, "r") as f:
                dm_log_count = len(json.load(f))
    except Exception:
        pass

    return render_template("index.html", state=bot_state, cooldown_range=f"{COOLDOWN_MIN}-{COOLDOWN_MAX} min", dm_log_count=dm_log_count)

# ── API ──
@app.route("/api/config", methods=["GET"])
@login_required
def api_get_config():
    """Retrieve all configuration JSON chunks to populate the UI."""
    data = {"accounts": [], "models": [], "messages": [], "settings": {}}
    try:
        with open(ACCOUNTS_FILE, "r") as f: data["accounts"] = json.load(f)
        with open(MODELS_FILE, "r") as f: data["models"] = json.load(f)
        with open(MESSAGES_FILE, "r") as f: data["messages"] = json.load(f)
        with open(SETTINGS_FILE, "r") as f: data["settings"] = json.load(f)
    except Exception as e:
        logger.error(f"Error reading config API: {e}")
    return jsonify(data)

@app.route("/api/config/<target>", methods=["POST"])
@login_required
def api_save_config(target):
    """Save configuration changes back to disk."""
    file_map = {
        "accounts": ACCOUNTS_FILE,
        "models": MODELS_FILE,
        "messages": MESSAGES_FILE,
        "settings": SETTINGS_FILE
    }
    target_file = file_map.get(target)
    if not target_file:
        return jsonify({"success": False, "error": "Invalid target"}), 400
        
    try:
        payload = request.get_json()
        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            
        # If settings were updated, force update the in-memory variables safely
        if target == "settings":
            import config.settings as s
            for k, v in payload.items():
                if hasattr(s, k):
                    setattr(s, k, v)
                    
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving {target}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/cookies/<username>", methods=["GET"])
@login_required
def api_get_cookies(username):
    """Retrieve raw cookies JSON for an account if it exists."""
    cookie_path = os.path.join(COOKIES_DIR, f"{username}.json")
    if os.path.exists(cookie_path):
        try:
            with open(cookie_path, "r", encoding="utf-8") as f:
                return jsonify({"success": True, "cookies": f.read()})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "cookies": ""})


@app.route("/api/cookies/<username>", methods=["POST"])
@login_required
def api_save_cookies(username):
    """Save raw cookies JSON directly to the user's cookies file."""
    cookie_path = os.path.join(COOKIES_DIR, f"{username}.json")
    try:
        payload = request.get_json()
        cookies_str = payload.get("cookies", "")
        if not cookies_str.strip():
            # If empty, delete the file so it forces fresh login
            if os.path.exists(cookie_path):
                os.remove(cookie_path)
        else:
            # Validate it's json array
            json.loads(cookies_str)
            with open(cookie_path, "w", encoding="utf-8") as f:
                f.write(cookies_str)
        return jsonify({"success": True})
    except json.JSONDecodeError:
        return jsonify({"success": False, "error": "Invalid JSON format"}), 400
    except Exception as e:
        logger.error(f"Error saving cookies for {username}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
@app.route("/api/status")
@login_required
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
    if bot_state["status"] in ("idle", "stopped"):
        stop_event.clear()
        bot_thread = threading.Thread(target=bot_loop, daemon=True)
        bot_thread.start()
        bot_state["status"] = "running"
    return "<script>window.location='/'</script>"


@app.route("/stop")
@login_required
def stop_bot():
    stop_event.set()
    bot_state["status"] = "stopped"
    return "<script>window.location='/'</script>"


# ── Main ──
if __name__ == "__main__":
    init_auth_db()

    print("=" * 60)
    print("  INSTAGRAM DM BOT — FLASK SERVER")
    print("=" * 60)
    print(f"  Dashboard: http://localhost:5000")
    print(f"  Cooldown:  {COOLDOWN_MIN}-{COOLDOWN_MAX} minutes between sessions")
    print("=" * 60)
    print()

    # Bot will remain idle until started via the web UI dashboard.
    bot_state["status"] = "idle"

    app.run(host="0.0.0.0", port=5000, debug=False)
