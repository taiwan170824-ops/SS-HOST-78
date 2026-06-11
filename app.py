import os
import json
import subprocess
import signal
import sys
import threading
import time
import logging
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from functools import wraps
import hashlib
import secrets

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

# ── Config ───────────────────────────────────────────────────────────────────
DATA_FILE = "bots_data/bots.json"
LOGS_DIR  = "bots_data/logs"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")  # Change in production!

os.makedirs("bots_data", exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# In-memory process registry  {bot_id: subprocess.Popen}
running_processes: dict[str, subprocess.Popen] = {}

# ── Helpers ───────────────────────────────────────────────────────────────────
def load_bots() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}

def save_bots(bots: dict) -> None:
    with open(DATA_FILE, "w") as f:
        json.dump(bots, f, indent=2)

def get_bot_status(bot_id: str) -> str:
    proc = running_processes.get(bot_id)
    if proc and proc.poll() is None:
        return "running"
    return "stopped"

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def tail_log(bot_id: str, lines: int = 50) -> list[str]:
    log_file = os.path.join(LOGS_DIR, f"{bot_id}.log")
    if not os.path.exists(log_file):
        return []
    with open(log_file) as f:
        all_lines = f.readlines()
    return [l.rstrip() for l in all_lines[-lines:]]

def _stream_to_log(proc: subprocess.Popen, log_path: str):
    """Background thread: write stdout+stderr to log file."""
    with open(log_path, "a") as lf:
        for line in proc.stdout:  # stdout=PIPE, stderr=STDOUT
            lf.write(line)
            lf.flush()

# ── Auth routes ───────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        pw = request.form.get("password", "")
        if hash_password(pw) == hash_password(ADMIN_PASSWORD):
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        error = "Wrong password!"
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ── Dashboard ─────────────────────────────────────────────────────────────────
@app.route("/")
@login_required
def dashboard():
    bots = load_bots()
    for bid, bot in bots.items():
        bot["status"] = get_bot_status(bid)
        bot["id"] = bid
    return render_template("dashboard.html", bots=list(bots.values()))

# ── API: Add bot ──────────────────────────────────────────────────────────────
@app.route("/api/bot/add", methods=["POST"])
@login_required
def add_bot():
    data = request.json or {}
    name  = data.get("name", "").strip()
    token = data.get("token", "").strip()
    code  = data.get("code", "").strip()

    if not name or not token or not code:
        return jsonify({"success": False, "error": "Name, token, and code are required"}), 400

    bots = load_bots()
    bot_id = f"bot_{int(time.time() * 1000)}"

    # Save bot script
    script_path = os.path.join("bots_data", f"{bot_id}.py")
    with open(script_path, "w") as f:
        f.write(code)

    bots[bot_id] = {
        "id":         bot_id,
        "name":       name,
        "token":      token,
        "script":     script_path,
        "created_at": datetime.now().isoformat(),
        "status":     "stopped",
    }
    save_bots(bots)
    logger.info("Bot added: %s (%s)", name, bot_id)
    return jsonify({"success": True, "bot_id": bot_id})

# ── API: Edit bot ─────────────────────────────────────────────────────────────
@app.route("/api/bot/<bot_id>/edit", methods=["POST"])
@login_required
def edit_bot(bot_id):
    bots = load_bots()
    if bot_id not in bots:
        return jsonify({"success": False, "error": "Bot not found"}), 404

    data  = request.json or {}
    name  = data.get("name", "").strip()
    token = data.get("token", "").strip()
    code  = data.get("code", "").strip()

    if name:  bots[bot_id]["name"]  = name
    if token: bots[bot_id]["token"] = token
    if code:
        with open(bots[bot_id]["script"], "w") as f:
            f.write(code)

    save_bots(bots)
    return jsonify({"success": True})

# ── API: Delete bot ───────────────────────────────────────────────────────────
@app.route("/api/bot/<bot_id>/delete", methods=["POST"])
@login_required
def delete_bot(bot_id):
    bots = load_bots()
    if bot_id not in bots:
        return jsonify({"success": False, "error": "Bot not found"}), 404

    # Stop if running
    if get_bot_status(bot_id) == "running":
        _stop_proc(bot_id)

    # Remove files
    script = bots[bot_id].get("script", "")
    if os.path.exists(script):
        os.remove(script)
    log_file = os.path.join(LOGS_DIR, f"{bot_id}.log")
    if os.path.exists(log_file):
        os.remove(log_file)

    del bots[bot_id]
    save_bots(bots)
    logger.info("Bot deleted: %s", bot_id)
    return jsonify({"success": True})

# ── API: Start bot ────────────────────────────────────────────────────────────
@app.route("/api/bot/<bot_id>/start", methods=["POST"])
@login_required
def start_bot(bot_id):
    bots = load_bots()
    if bot_id not in bots:
        return jsonify({"success": False, "error": "Bot not found"}), 404

    if get_bot_status(bot_id) == "running":
        return jsonify({"success": False, "error": "Bot already running"})

    script = bots[bot_id]["script"]
    if not os.path.exists(script):
        return jsonify({"success": False, "error": "Script file missing"}), 404

    token    = bots[bot_id]["token"]
    env      = {**os.environ, "BOT_TOKEN": token, "TELEGRAM_TOKEN": token}
    log_path = os.path.join(LOGS_DIR, f"{bot_id}.log")

    try:
        proc = subprocess.Popen(
            [sys.executable, script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        running_processes[bot_id] = proc
        t = threading.Thread(target=_stream_to_log, args=(proc, log_path), daemon=True)
        t.start()
        logger.info("Bot started: %s (PID %s)", bot_id, proc.pid)
        return jsonify({"success": True, "pid": proc.pid})
    except Exception as e:
        logger.error("Failed to start bot %s: %s", bot_id, e)
        return jsonify({"success": False, "error": str(e)}), 500

def _stop_proc(bot_id: str):
    proc = running_processes.pop(bot_id, None)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

# ── API: Stop bot ─────────────────────────────────────────────────────────────
@app.route("/api/bot/<bot_id>/stop", methods=["POST"])
@login_required
def stop_bot(bot_id):
    if get_bot_status(bot_id) != "running":
        return jsonify({"success": False, "error": "Bot is not running"})
    _stop_proc(bot_id)
    logger.info("Bot stopped: %s", bot_id)
    return jsonify({"success": True})

# ── API: Restart bot ──────────────────────────────────────────────────────────
@app.route("/api/bot/<bot_id>/restart", methods=["POST"])
@login_required
def restart_bot(bot_id):
    if get_bot_status(bot_id) == "running":
        _stop_proc(bot_id)
        time.sleep(1)
    return start_bot(bot_id)

# ── API: Logs ─────────────────────────────────────────────────────────────────
@app.route("/api/bot/<bot_id>/logs")
@login_required
def bot_logs(bot_id):
    lines = int(request.args.get("lines", 50))
    return jsonify({"logs": tail_log(bot_id, lines), "status": get_bot_status(bot_id)})

# ── API: Bot code ─────────────────────────────────────────────────────────────
@app.route("/api/bot/<bot_id>/code")
@login_required
def bot_code(bot_id):
    bots = load_bots()
    if bot_id not in bots:
        return jsonify({"success": False, "error": "Bot not found"}), 404
    script = bots[bot_id].get("script", "")
    code = ""
    if os.path.exists(script):
        with open(script) as f:
            code = f.read()
    return jsonify({"code": code, "name": bots[bot_id]["name"], "token": bots[bot_id]["token"]})

# ── API: Status of all bots ───────────────────────────────────────────────────
@app.route("/api/bots/status")
@login_required
def all_status():
    bots = load_bots()
    result = {bid: get_bot_status(bid) for bid in bots}
    return jsonify(result)

# ── Cleanup on exit ───────────────────────────────────────────────────────────
def shutdown(*_):
    logger.info("Shutting down – stopping all bots…")
    for bid in list(running_processes.keys()):
        _stop_proc(bid)
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT,  shutdown)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
