from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from instagrapi import Client
from instagrapi.exceptions import LoginRequired, ChallengeRequired, BadPassword
import json
import os
import logging
from datetime import datetime
import atexit

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-secreta-123")

DATA_FILE = "data/tracker_data.json"
SESSION_FILE = "data/ig_session.json"
os.makedirs("data", exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

cl = Client()
scheduler = BackgroundScheduler()

# ── Persistência ──────────────────────────────────────────────

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "username": None,
        "logged_in": False,
        "followers_history": [],
        "unfollowers": [],
        "new_followers": [],
        "last_check": None,
        "total_checks": 0
    }

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ── Login / Sessão ────────────────────────────────────────────

def try_load_session():
    if os.path.exists(SESSION_FILE):
        try:
            cl.load_settings(SESSION_FILE)
            cl.get_timeline_feed()
            logger.info("Sessão carregada com sucesso.")
            return True
        except Exception:
            logger.warning("Sessão expirada, requer novo login.")
    return False

# ── Coleta de seguidores ──────────────────────────────────────

def fetch_followers():
    data = load_data()
    if not data.get("logged_in"):
        return

    try:
        user_id = cl.user_id_from_username(data["username"])
        followers = cl.user_followers(user_id, amount=0)  # 0 = todos
        current_ids = set(str(uid) for uid in followers.keys())
        current_info = {str(uid): u.username for uid, u in followers.items()}

        now = datetime.now().isoformat()

        if data["followers_history"]:
            previous = set(data["followers_history"][-1]["ids"])
            unfollowed_ids = previous - current_ids
            new_ids = current_ids - previous

            unfollowers_list = []
            for uid in unfollowed_ids:
                username = None
                for snap in reversed(data["followers_history"]):
                    username = snap.get("usernames", {}).get(uid)
                    if username:
                        break
                unfollowers_list.append({
                    "id": uid,
                    "username": username or f"user_{uid}",
                    "detected_at": now
                })

            new_followers_list = []
            for uid in new_ids:
                new_followers_list.append({
                    "id": uid,
                    "username": current_info.get(uid, f"user_{uid}"),
                    "detected_at": now
                })

            data["unfollowers"] = (unfollowers_list + data.get("unfollowers", []))[:500]
            data["new_followers"] = (new_followers_list + data.get("new_followers", []))[:500]

        data["followers_history"].append({
            "timestamp": now,
            "count": len(current_ids),
            "ids": list(current_ids),
            "usernames": current_info
        })
        # Manter últimos 30 snapshots
        data["followers_history"] = data["followers_history"][-30:]
        data["last_check"] = now
        data["total_checks"] = data.get("total_checks", 0) + 1
        save_data(data)
        logger.info(f"Verificação concluída: {len(current_ids)} seguidores.")

    except LoginRequired:
        logger.error("Login expirado.")
        data["logged_in"] = False
        save_data(data)
    except Exception as e:
        logger.error(f"Erro ao buscar seguidores: {e}")

# ── Rotas ─────────────────────────────────────────────────────

@app.route("/")
def index():
    data = load_data()
    return render_template("index.html", data=data)

@app.route("/login", methods=["POST"])
def login():
    body = request.get_json()
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    code_2fa = body.get("code_2fa", "").strip()

    if not username or not password:
        return jsonify({"ok": False, "error": "Preencha usuário e senha."})

    try:
        if try_load_session():
            data = load_data()
            data["logged_in"] = True
            data["username"] = cl.username
            save_data(data)
            return jsonify({"ok": True, "message": "Sessão restaurada!"})

        if code_2fa:
            cl.login(username, password, verification_code=code_2fa)
        else:
            cl.login(username, password)

        cl.dump_settings(SESSION_FILE)

        data = load_data()
        data["logged_in"] = True
        data["username"] = username
        save_data(data)

        # Primeira coleta imediata
        fetch_followers()

        return jsonify({"ok": True, "message": "Login realizado com sucesso!"})

    except BadPassword:
        return jsonify({"ok": False, "error": "Senha incorreta."})
    except ChallengeRequired:
        return jsonify({"ok": False, "error": "Instagram pediu verificação. Tente pelo app primeiro.", "challenge": True})
    except Exception as e:
        err = str(e)
        if "two_factor" in err.lower() or "2fa" in err.lower():
            return jsonify({"ok": False, "error": "2FA ativado. Informe o código.", "need_2fa": True})
        return jsonify({"ok": False, "error": f"Erro: {err}"})

@app.route("/logout", methods=["POST"])
def logout():
    try:
        cl.logout()
    except Exception:
        pass
    if os.path.exists(SESSION_FILE):
        os.remove(SESSION_FILE)
    data = load_data()
    data["logged_in"] = False
    save_data(data)
    return jsonify({"ok": True})

@app.route("/check", methods=["POST"])
def manual_check():
    data = load_data()
    if not data.get("logged_in"):
        return jsonify({"ok": False, "error": "Não está logado."})
    fetch_followers()
    return jsonify({"ok": True, "message": "Verificação concluída!"})

@app.route("/api/stats")
def api_stats():
    data = load_data()
    history = data.get("followers_history", [])
    current_count = history[-1]["count"] if history else 0
    chart_labels = [h["timestamp"][:10] for h in history[-14:]]
    chart_values = [h["count"] for h in history[-14:]]
    return jsonify({
        "logged_in": data.get("logged_in", False),
        "username": data.get("username"),
        "current_followers": current_count,
        "total_unfollowers": len(data.get("unfollowers", [])),
        "total_new": len(data.get("new_followers", [])),
        "last_check": data.get("last_check"),
        "total_checks": data.get("total_checks", 0),
        "unfollowers": data.get("unfollowers", [])[:20],
        "new_followers": data.get("new_followers", [])[:20],
        "chart_labels": chart_labels,
        "chart_values": chart_values,
    })

# ── Scheduler ─────────────────────────────────────────────────

def start_scheduler():
    if not scheduler.running:
        scheduler.add_job(fetch_followers, "interval", hours=6, id="auto_check")
        scheduler.start()
        atexit.register(lambda: scheduler.shutdown())
        logger.info("Scheduler iniciado — verificação a cada 6 horas.")

if __name__ == "__main__":
    try_load_session()
    start_scheduler()
    app.run(host="0.0.0.0", port=5000, debug=False)
