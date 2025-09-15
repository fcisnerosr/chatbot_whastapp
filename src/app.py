import os, json, random
from pathlib import Path
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests

load_dotenv()

# ====== CONFIG Gupshup ======
GUPSHUP_API_KEY = os.getenv("GUPSHUP_API_KEY")
GUPSHUP_APP_NAME = os.getenv("GUPSHUP_APP_NAME", "RolesClubBot")
GUPSHUP_SOURCE = os.getenv("GUPSHUP_SOURCE")  # ej: 917834811114 (sin +)
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "rolesclub-verify")

# Admin(es) (E.164 sin +, separados por coma)
ADMIN_NUMBERS = [n.strip() for n in os.getenv("ADMIN_NUMBERS", "").split(",") if n.strip()]

HEADERS_FORM = {"apikey": GUPSHUP_API_KEY, "Content-Type": "application/x-www-form-urlencoded"}

# ====== Archivos ======
BASE_DIR = Path(__file__).parent
MEMBERS_FILE = BASE_DIR / "members.json"
STATE_FILE   = BASE_DIR / "state.json"

# ====== Utilidades ======
def send_text(to_e164_no_plus: str, text: str):
    url = "https://api.gupshup.io/wa/api/v1/msg"
    data = {
        "channel": "whatsapp",
        "source": GUPSHUP_SOURCE,
        "destination": to_e164_no_plus,
        "message": text,
        "src.name": GUPSHUP_APP_NAME
    }
    r = requests.post(url, headers=HEADERS_FORM, data=data, timeout=30)
    try:
        return r.json()
    except Exception:
        return {"status_code": r.status_code, "text": r.text}

def broadcast_text(numbers, text):
    for n in numbers:
        send_text(n, text)

def load_members():
    with open(MEMBERS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    idx = {m["waid"]: m for m in data["members"]}
    return data["roles"], data["members"], idx

ROLES, MEMBERS, MEMBERS_IDX = load_members()
ALL_NUMBERS = [m["waid"] for m in MEMBERS]

def default_state():
    return {
        "round": 0,
        "pending": {},       # role -> {"candidate": waid, "declined_by": [], "accepted": false}
        "accepted": {},      # role -> {"waid": "...", "name": "..."}
        "members_cycle": {m["waid"]: [] for m in MEMBERS},  # roles completados (ciclo personal)
        "last_summary": None,
        "canceled": False
    }

def load_state():
    if not STATE_FILE.exists():
        st = default_state()
        save_state(st)
        return st
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

def roles_left_for_member(waid):
    st = load_state()
    done = set(st["members_cycle"].get(waid, []))
    return [r for r in ROLES if r not in done]

def choose_candidate(role, excluded):
    """Elige elegibles (no hayan hecho ese rol en su ciclo) y excluye ya propuestos/aceptados."""
    st = load_state()
    eligible = []
    for m in MEMBERS:
        w = m["waid"]
        if w in excluded:
            continue
        done = set(st["members_cycle"].get(w, []))
        if role not in done:
            eligible.append(w)
    # fallback: si nadie elegible, permite repetir (evita duplicar dentro de la ronda)
    if not eligible:
        eligible = [m["waid"] for m in MEMBERS if m["waid"] not in excluded]
    return random.choice(eligible) if eligible else None

def pretty_name(waid):
    m = MEMBERS_IDX.get(waid)
    return m["name"] if m else waid

def make_summary(st):
    lines = [f"🗓️ Reunión #{st['round']} – Roles asignados:"]
    for role in ROLES:
        if role in st["accepted"]:
            w = st["accepted"][role]["waid"]
            lines.append(f"• {role}: {pretty_name(w)}")
        else:
            lines.append(f"• {role}: (pendiente)")
    return "\n".join(lines)

# ====== Flujo de ronda ======
def start_new_round(by_admin):
    st = load_state()
    if any(not v.get("accepted") for v in st["pending"].values()):
        return "Ya hay una ronda con roles pendientes."
    st["round"] += 1
    st["pending"] = {}
    st["accepted"] = {}
    st["last_summary"] = None
    st["canceled"] = False
    save_state(st)

    # Asignación inicial (primer candidato por rol)
    for role in ROLES:
        excluded = set([a["waid"] for a in st["accepted"].values()])
        cand = choose_candidate(role, excluded)
        if not cand:
            continue
        st["pending"][role] = {"candidate": cand, "declined_by": [], "accepted": False}
        save_state(st)
        send_text(
            cand,
            f"Hola {pretty_name(cand)} 👋\n"
            f"Para la reunión #{st['round']} te propongo el rol *{role}*.\n\n"
            f"Responde:\n• *ACEPTO* para confirmar\n• *RECHAZO* si no puedes\n\n"
            f"(Si rechazas, se propondrá a otro miembro.)"
        )

    broadcast_text(ADMIN_NUMBERS, f"✅ Ronda #{st['round']} iniciada por {by_admin}. Escribe ESTADO para ver pendientes.")
    return f"Ronda #{st['round']} iniciada."

def handle_accept(waid):
    st = load_state()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            info["accepted"] = True
            st["accepted"][role] = {"waid": waid, "name": pretty_name(waid)}
            save_state(st)

            # Actualiza ciclo personal
            done_list = st["members_cycle"].get(waid, [])
            if role not in done_list:
                done_list.append(role)
            if len(done_list) >= len(ROLES):
                done_list = []  # reinicia ciclo al completar todos
            st["members_cycle"][waid] = done_list
            save_state(st)

            send_text(waid, f"🎉 ¡Gracias {pretty_name(waid)}! Quedaste como *{role}* en la reunión #{st['round']}.")
            check_and_announce_if_complete()
            return f"{pretty_name(waid)} aceptó {role}."
    send_text(waid, "No tienes una propuesta de rol pendiente ahora mismo. Escribe *MI ROL* para verificar.")
    return "Nada que aceptar."

def handle_reject(waid):
    st = load_state()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            info["declined_by"].append(waid)
            excluded = set(info["declined_by"])
            excluded.update([a["waid"] for a in st["accepted"].values()])
            cand = choose_candidate(role, excluded)
            if cand:
                info["candidate"] = cand
                save_state(st)
                send_text(waid, f"Gracias por avisar, buscaremos otra opción para *{role}* 👍")
                send_text(
                    cand,
                    f"Hola {pretty_name(cand)} 👋\n"
                    f"¿Podrías tomar el rol *{role}* para la reunión #{st['round']}?\n"
                    f"Responde *ACEPTO* o *RECHAZO*."
                )
                return f"{pretty_name(waid)} rechazó {role}. Nuevo candidato: {pretty_name(cand)}"
            else:
                del st["pending"][role]
                save_state(st)
                broadcast_text(ADMIN_NUMBERS, f"⚠️ No hay candidato disponible para {role}. Resolver manualmente.")
                return "Sin candidatos."
    send_text(waid, "No tienes propuesta de rol pendiente ahora. Escribe *MI ROL* para verificar.")
    return "Nada que rechazar."

def check_and_announce_if_complete():
    st = load_state()
    all_ok = all(role in st["accepted"] for role in ROLES)
    if not all_ok or st.get("canceled"):
        return
    summary = make_summary(st)
    if st.get("last_summary") == summary:
        return
    st["last_summary"] = summary
    save_state(st)
    broadcast_text(ALL_NUMBERS, f"✅ {summary}\n\n¡Nos vemos en la próxima reunión!")
    broadcast_text(ADMIN_NUMBERS, "📣 Se anunció a todos los miembros.")

def who_am_i(waid):
    st = load_state()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            return f"Tienes pendiente el rol *{role}* en la ronda #{st['round']}.\nResponde *ACEPTO* o *RECHAZO*."
    for role, acc in st["accepted"].items():
        if acc["waid"] == waid:
            return f"Ya aceptaste el rol *{role}* en la ronda #{st['round']}."
    return "No tienes asignaciones pendientes. Si esperas una propuesta, consulta al admin."

def status_text():
    st = load_state()
    lines = [make_summary(st), "", "Pendientes:"]
    any_pending = False
    for role, info in st["pending"].items():
        if not info["accepted"]:
            any_pending = True
            cand = info["candidate"]
            lines.append(f"• {role}: propuesto a {pretty_name(cand)} (declinaron: {len(info['declined_by'])})")
    if not any_pending:
        lines.append("• (ninguno)")
    if st.get("canceled"):
        lines.append("\nEstado: ❌ Ronda cancelada.")
    return "\n".join(lines)

# ====== Admin extras: CANCELAR y RESET ======
def cancel_round(by_admin):
    st = load_state()
    st["pending"] = {}
    st["accepted"] = {}
    st["last_summary"] = None
    st["canceled"] = True
    save_state(st)
    broadcast_text(ALL_NUMBERS, "⚠️ La ronda de roles fue *cancelada* por el administrador.")
    broadcast_text(ADMIN_NUMBERS, f"❌ Ronda #{st['round']} cancelada por {by_admin}.")
    return f"Ronda #{st['round']} cancelada."

def reset_all(by_admin):
    st = default_state()
    save_state(st)
    broadcast_text(ADMIN_NUMBERS, f"🔄 Estado completamente reiniciado por {by_admin}. (round=0)")
    return "Estado reiniciado a fábrica."

# ====== Flask app ======
app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return {"ok": True, "app": GUPSHUP_APP_NAME, "roles": ROLES, "members": len(MEMBERS)}

# Verificación tipo FB/Gupshup (opcional)
@app.route("/webhook", methods=["GET"])
def webhook_get():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge or "OK", 200
    return "ok", 200

@app.route("/webhook", methods=["POST"])
def webhook_post():
    data = request.get_json(force=True, silent=True) or {}
    try:
        changes = data["entry"][0]["changes"][0]["value"]
        # Mensajes entrantes
        if "messages" in changes:
            for msg in changes["messages"]:
                if msg.get("type") == "text":
                    waid = msg["from"]
                    body = msg["text"]["body"].strip().lower()

                    # === Comandos admin ===
                    if waid in ADMIN_NUMBERS:
                        if body in ("iniciar", "roles", "/iniciar"):
                            out = start_new_round(by_admin=pretty_name(waid))
                            send_text(waid, out)
                            return jsonify({"status":"ok"})
                        if body in ("estado", "/estado"):
                            send_text(waid, status_text())
                            return jsonify({"status":"ok"})
                        if body in ("cancelar", "/cancelar"):
                            out = cancel_round(pretty_name(waid))
                            send_text(waid, out)
                            return jsonify({"status":"ok"})
                        if body in ("reset", "/reset"):
                            out = reset_all(pretty_name(waid))
                            send_text(waid, out)
                            return jsonify({"status":"ok"})

                    # === Usuario común ===
                    if body in ("acepto", "aceptar", "sí acepto", "si acepto"):
                        handle_accept(waid)
                    elif body in ("rechazo", "no acepto", "no puedo", "rechazar"):
                        handle_reject(waid)
                    elif body in ("mi rol", "mirol", "miasignacion", "miasignación"):
                        send_text(waid, who_am_i(waid))
                    elif body in ("hola", "hi", "hello"):
                        send_text(waid, "¡Hola! Soy RolesClubBot 🤖. ¿En qué te ayudo?")
                    else:
                        send_text(waid, f"Recibí: {body}. Escribe *MI ROL*, *ACEPTO* o *RECHAZO*.")
        # Status de mensajes
        elif "statuses" in changes:
            pass
    except Exception as e:
        print("Webhook parsing error:", e, "payload:", data)

    return jsonify({"status": "ok"})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)