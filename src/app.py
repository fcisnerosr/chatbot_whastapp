# app.py
# --------------------------------------------------------------------------------------
# WhatsApp roles bot (Flask + Gupshup)
# Versión con buenas prácticas: validación de config, logging, persistencia atómica,
# normalización de comandos y comentarios explicativos.
# --------------------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import os
import random
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Dict, List, Optional, Set, Tuple

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from requests.exceptions import RequestException

# ------------------------------------------------------------------------------
# 1) Configuración y logging
# ------------------------------------------------------------------------------

load_dotenv()  # Lee variables desde .env si existe

# Configura logging (puedes cambiar el nivel con LOG_LEVEL=DEBUG/INFO/WARNING...)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("roles-bot")


@dataclass(frozen=True)
class Config:
    """Config inmutable cargada desde variables de entorno (fail-fast)."""

    api_key: str
    app_name: str
    source: str  # E.164 SIN el '+'
    verify_token: str
    admins: Set[str]  # E.164 SIN el '+'
    port: int


def load_config() -> Config:
    """Lee y valida la configuración requerida. Revienta si falta algo crítico."""
    missing: List[str] = []
    api_key = os.getenv("GUPSHUP_API_KEY")
    if not api_key:
        missing.append("GUPSHUP_API_KEY")

    source = os.getenv("GUPSHUP_SOURCE")
    if not source:
        missing.append("GUPSHUP_SOURCE")

    if missing:
        raise RuntimeError(
            f"Variables de entorno faltantes: {', '.join(missing)}. "
            "Define un archivo .env o exporta variables antes de ejecutar."
        )

    app_name = os.getenv("GUPSHUP_APP_NAME", "RolesClubBotToastmasters")
    verify = os.getenv("VERIFY_TOKEN", "rolesclub-verify")
    admins = {n.strip() for n in os.getenv("ADMIN_NUMBERS", "").split(",") if n.strip()}
    port = int(os.getenv("PORT", "5000"))
    return Config(api_key=api_key, app_name=app_name, source=source, verify_token=verify, admins=admins, port=port)


CFG = load_config()
HEADERS_FORM = {"apikey": CFG.api_key, "Content-Type": "application/x-www-form-urlencoded"}

# ------------------------------------------------------------------------------
# 2) Rutas de archivos
# ------------------------------------------------------------------------------

# Estructura esperada del proyecto:
# <repo>/
#  ├─ data/
#  │   ├─ members.json
#  │   └─ state.json
#  └─ src/app.py  (este archivo)
BASE_DIR = Path(__file__).resolve().parent.parent
MEMBERS_FILE = BASE_DIR / "data" / "members.json"
STATE_FILE = BASE_DIR / "data" / "state.json"

if not MEMBERS_FILE.exists():
    raise FileNotFoundError(
        f"No se encontró {MEMBERS_FILE}. Crea data/members.json con estructura: "
        '{"roles": [...], "members": [{"name": "...", "waid": "E164_sin_+"}, ...]}.'
    )

# ------------------------------------------------------------------------------
# 3) Carga de catálogo (roles y miembros)
# ------------------------------------------------------------------------------

def load_members() -> Tuple[List[str], List[Dict[str, str]], Dict[str, Dict[str, str]]]:
    """Lee y valida members.json; devuelve (roles, miembros, índice por waid)."""
    with MEMBERS_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    roles = data.get("roles", [])
    members = data.get("members", [])

    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise ValueError("`roles` debe ser lista de strings en members.json")

    if not isinstance(members, list):
        raise ValueError("`members` debe ser lista en members.json")

    idx: Dict[str, Dict[str, str]] = {}
    for m in members:
        if not isinstance(m, dict) or "waid" not in m or "name" not in m:
            raise ValueError("Cada miembro debe tener claves 'name' y 'waid'")
        waid = str(m["waid"]).strip()
        if not waid.isdigit():
            log.warning("WAID no parece E.164 (solo dígitos): %s", waid)
        idx[waid] = m

    return roles, members, idx


ROLES, MEMBERS, MEMBERS_IDX = load_members()
ALL_NUMBERS: Tuple[str, ...] = tuple(m["waid"] for m in MEMBERS)

# ------------------------------------------------------------------------------
# 4) Persistencia del estado (JSON atómico con lock)
# ------------------------------------------------------------------------------

def _dump_json_atomic(path: Path, obj: dict) -> None:
    """Escribe JSON de forma atómica (escribe a fichero temporal y luego reemplaza)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as tmp:
        json.dump(obj, tmp, ensure_ascii=False, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)  # atómico en la mayoría de SOs


class StateStore:
    """Pequeña utilidad hilo-segura para cargar/guardar state.json."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()

    def load(self) -> dict:
        with self._lock:
            if not self.path.exists():
                st = default_state()
                _dump_json_atomic(self.path, st)
                return st
            return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, st: dict) -> None:
        with self._lock:
            _dump_json_atomic(self.path, st)


def default_state() -> dict:
    """Estado inicial."""
    return {
        "round": 0,
        "pending": {},  # role -> {"candidate": waid, "declined_by": [], "accepted": false}
        "accepted": {},  # role -> {"waid": "...", "name": "..."}
        "members_cycle": {m["waid"]: [] for m in MEMBERS},  # roles completados por miembro
        "last_summary": None,
        "canceled": False,
    }


state_store = StateStore(STATE_FILE)

# ------------------------------------------------------------------------------
# 5) Utilidades de negocio (envíos, elección de candidatos, textos)
# ------------------------------------------------------------------------------

def send_text(to_e164_no_plus: str, text: str) -> dict:
    """
    Envía texto por la API de Gupshup con manejo de errores.
    Devuelve dict con resultado (útil para logs/depuración).
    """
    url = "https://api.gupshup.io/wa/api/v1/msg"
    data = {
        "channel": "whatsapp",
        "source": CFG.source,
        "destination": to_e164_no_plus,
        "message": text,
        "src.name": CFG.app_name,
    }
    try:
        r = requests.post(url, headers=HEADERS_FORM, data=data, timeout=15)
        if r.ok:
            return r.json()
        log.warning("Gupshup %s: %s", r.status_code, r.text)
        return {"ok": False, "status": r.status_code, "text": r.text}
    except RequestException as e:
        log.exception("Error al llamar Gupshup")
        return {"ok": False, "error": str(e)}


def broadcast_text(numbers: Set[str] | List[str] | Tuple[str, ...], text: str) -> Dict[str, int]:
    """Envía un texto a varios números y devuelve contadores de OK/fallo."""
    ok = fail = 0
    for n in numbers:
        res = send_text(n, text)
        if res.get("ok") is False or res.get("status_code", 200) >= 400:
            fail += 1
        else:
            ok += 1
    return {"ok": ok, "fail": fail}


def pretty_name(waid: str) -> str:
    m = MEMBERS_IDX.get(waid)
    return m["name"] if m else waid


def roles_left_for_member(waid: str) -> List[str]:
    st = state_store.load()
    done = set(st["members_cycle"].get(waid, []))
    return [r for r in ROLES if r not in done]


def choose_candidate(role: str, excluded: Set[str]) -> Optional[str]:
    """
    Elige elegibles (no hayan hecho ese rol en su ciclo) y excluye ya propuestos/aceptados.
    Si no hay elegibles, permite repetir evitando duplicar dentro de la ronda.
    """
    st = state_store.load()
    eligible: List[str] = []
    for m in MEMBERS:
        w = m["waid"]
        if w in excluded:
            continue
        done = set(st["members_cycle"].get(w, []))
        if role not in done:
            eligible.append(w)
    if not eligible:
        eligible = [m["waid"] for m in MEMBERS if m["waid"] not in excluded]
    return random.choice(eligible) if eligible else None


def make_summary(st: dict) -> str:
    lines = [f"🗓️ Reunión #{st['round']} – Roles asignados:"]
    for role in ROLES:
        if role in st["accepted"]:
            w = st["accepted"][role]["waid"]
            lines.append(f"• {role}: {pretty_name(w)}")
        else:
            lines.append(f"• {role}: (pendiente)")
    return "\n".join(lines)


# Normalizador y tablas de comandos (evita problemas de acentos/mayúsculas)
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


ADMIN_CMDS: Dict[str, str] = {
    "iniciar": "start",
    "/iniciar": "start",
    "roles": "start",
    "estado": "status",
    "/estado": "status",
    "cancelar": "cancel",
    "/cancelar": "cancel",
    "reset": "reset",
    "/reset": "reset",
}

USER_CMDS: Dict[str, str] = {
    "acepto": "accept",
    "aceptar": "accept",
    "si acepto": "accept",
    "sí acepto": "accept",
    "rechazo": "reject",
    "no acepto": "reject",
    "no puedo": "reject",
    "rechazar": "reject",
    "mi rol": "whoami",
    "mirol": "whoami",
    "miasignacion": "whoami",
    "miasignacion": "whoami",
    "hola": "hello",
    "hi": "hello",
    "hello": "hello",
}

# ------------------------------------------------------------------------------
# 6) Reglas de la ronda (mutan el state y envían mensajes)
# ------------------------------------------------------------------------------

def start_new_round(by_admin: str) -> str:
    st = state_store.load()
    if any(not v.get("accepted") for v in st["pending"].values()):
        return "Ya hay una ronda con roles pendientes."

    st["round"] += 1
    st["pending"] = {}
    st["accepted"] = {}
    st["last_summary"] = None
    st["canceled"] = False

    # Crea primer candidato por rol y avisa
    for role in ROLES:
        excluded = set(a["waid"] for a in st["accepted"].values())
        cand = choose_candidate(role, excluded)
        if not cand:
            continue
        st["pending"][role] = {"candidate": cand, "declined_by": [], "accepted": False}
    state_store.save(st)

    for role, info in st["pending"].items():
        cand = info["candidate"]
        send_text(
            cand,
            f"Hola {pretty_name(cand)} 👋\n"
            f"Para la reunión #{st['round']} te propongo el rol *{role}*.\n\n"
            f"Responde:\n• *ACEPTO* para confirmar\n• *RECHAZO* si no puedes\n\n"
            f"(Si rechazas, se propondrá a otro miembro.)",
        )

    broadcast_text(CFG.admins, f"✅ Ronda #{st['round']} iniciada por {by_admin}. Escribe ESTADO para ver pendientes.")
    return f"Ronda #{st['round']} iniciada."


def handle_accept(waid: str) -> str:
    st = state_store.load()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            info["accepted"] = True
            st["accepted"][role] = {"waid": waid, "name": pretty_name(waid)}

            # Actualiza ciclo personal
            done_list = list(st["members_cycle"].get(waid, []))
            if role not in done_list:
                done_list.append(role)
            if len(done_list) >= len(ROLES):
                done_list = []  # reinicia ciclo al completar todos
            st["members_cycle"][waid] = done_list

            state_store.save(st)
            send_text(waid, f"🎉 ¡Gracias {pretty_name(waid)}! Quedaste como *{role}* en la reunión #{st['round']}.")
            check_and_announce_if_complete()
            return f"{pretty_name(waid)} aceptó {role}."
    send_text(waid, "No tienes una propuesta de rol pendiente ahora mismo. Escribe *MI ROL* para verificar.")
    return "Nada que aceptar."


def handle_reject(waid: str) -> str:
    st = state_store.load()
    for role, info in list(st["pending"].items()):
        if info["candidate"] == waid and not info["accepted"]:
            info["declined_by"].append(waid)
            excluded = set(info["declined_by"])
            excluded.update(a["waid"] for a in st["accepted"].values())
            cand = choose_candidate(role, excluded)
            if cand:
                info["candidate"] = cand
                state_store.save(st)
                send_text(waid, f"Gracias por avisar, buscaremos otra opción para *{role}* 👍")
                send_text(
                    cand,
                    f"Hola {pretty_name(cand)} 👋\n"
                    f"¿Podrías tomar el rol *{role}* para la reunión #{st['round']}?\n"
                    f"Responde *ACEPTO* o *RECHAZO*.",
                )
                return f"{pretty_name(waid)} rechazó {role}. Nuevo candidato: {pretty_name(cand)}"
            else:
                del st["pending"][role]
                state_store.save(st)
                broadcast_text(CFG.admins, f"⚠️ No hay candidato disponible para {role}. Resolver manualmente.")
                return "Sin candidatos."
    send_text(waid, "No tienes propuesta de rol pendiente ahora. Escribe *MI ROL* para verificar.")
    return "Nada que rechazar."


def check_and_announce_if_complete() -> None:
    st = state_store.load()
    all_ok = all(role in st["accepted"] for role in ROLES)
    if not all_ok or st.get("canceled"):
        return
    summary = make_summary(st)
    if st.get("last_summary") == summary:
        return
    st["last_summary"] = summary
    state_store.save(st)
    broadcast_text(ALL_NUMBERS, f"✅ {summary}\n\n¡Nos vemos en la próxima reunión!")
    broadcast_text(CFG.admins, "📣 Se anunció a todos los miembros.")


def who_am_i(waid: str) -> str:
    st = state_store.load()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            return f"Tienes pendiente el rol *{role}* en la ronda #{st['round']}.\nResponde *ACEPTO* o *RECHAZO*."
    for role, acc in st["accepted"].items():
        if acc["waid"] == waid:
            return f"Ya aceptaste el rol *{role}* en la ronda #{st['round']}."
    return "No tienes asignaciones pendientes. Si esperas una propuesta, consulta al admin."


def status_text() -> str:
    st = state_store.load()
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


def cancel_round(by_admin: str) -> str:
    st = state_store.load()
    st["pending"] = {}
    st["accepted"] = {}
    st["last_summary"] = None
    st["canceled"] = True
    state_store.save(st)
    broadcast_text(ALL_NUMBERS, "⚠️ La ronda de roles fue *cancelada* por el administrador.")
    broadcast_text(CFG.admins, f"❌ Ronda #{st['round']} cancelada por {by_admin}.")
    return f"Ronda #{st['round']} cancelada."


def reset_all(by_admin: str) -> str:
    st = default_state()
    state_store.save(st)
    broadcast_text(CFG.admins, f"🔄 Estado completamente reiniciado por {by_admin}. (round=0)")
    return "Estado reiniciado a fábrica."

# ------------------------------------------------------------------------------
# 7) Flask app (endpoints)
# ------------------------------------------------------------------------------

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    """Endpoint de salud (útil para pruebas o monitoreo)."""
    return {"ok": True, "app": CFG.app_name, "roles": ROLES, "members": len(MEMBERS)}


# Verificación tipo FB/Gupshup (GET). Útil si activas el modo de verificación.
@app.route("/webhook", methods=["GET"])
def webhook_get():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == CFG.verify_token:
        return challenge or "OK", 200
    return "ok", 200


@app.route("/webhook", methods=["POST"])
def webhook_post():
    """Recibe eventos de Gupshup (formato Meta v3). Responde 200 siempre para evitar reintentos."""
    data = request.get_json(force=True, silent=True) or {}
    try:
        value = (
            (data.get("entry") or [{}])[0]
            .get("changes", [{}])[0]
            .get("value", {})
        )

        # Mensajes entrantes
        for msg in value.get("messages", []):
            if msg.get("type") != "text":
                continue
            waid = msg.get("from", "")
            body_raw = msg.get("text", {}).get("body", "")
            body = norm(body_raw)
            log.info("Mensaje de %s: %s", waid, body)

            # Comandos admin
            if waid in CFG.admins and body in ADMIN_CMDS:
                cmd = ADMIN_CMDS[body]
                if cmd == "start":
                    out = start_new_round(by_admin=pretty_name(waid))
                    send_text(waid, out)
                elif cmd == "status":
                    send_text(waid, status_text())
                elif cmd == "cancel":
                    out = cancel_round(pretty_name(waid))
                    send_text(waid, out)
                elif cmd == "reset":
                    out = reset_all(pretty_name(waid))
                    send_text(waid, out)
                continue  # siguiente mensaje

            # Comandos usuario
            if body in USER_CMDS:
                action = USER_CMDS[body]
                if action == "accept":
                    handle_accept(waid)
                elif action == "reject":
                    handle_reject(waid)
                elif action == "whoami":
                    send_text(waid, who_am_i(waid))
                elif action == "hello":
                    send_text(waid, "¡Hola! Soy RolesClubBot 🤖. ¿En qué te ayudo?")
            else:
                send_text(waid, f"Recibí: {body_raw}. Escribe *MI ROL*, *ACEPTO* o *RECHAZO*.")

        # También podrías procesar value.get("statuses") si te interesa
        if "statuses" in value:
            log.debug("Statuses: %s", value["statuses"])

    except Exception:
        log.exception("Error procesando webhook; payload=%s", data)

    return jsonify({"status": "ok"})

# ------------------------------------------------------------------------------
# 8) Main
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CFG.port, debug=False)
