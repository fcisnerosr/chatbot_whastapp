# app.py
# --------------------------------------------------------------------------------------
# WhatsApp roles bot (Flask + Gupshup)
# Versión integrada con models.py y club.json (POO + persistencia).
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

# Importar las clases del modelo POO
from models import Club, Member, Role

# ------------------------------------------------------------------------------
# 1) Configuración y logging
# ------------------------------------------------------------------------------

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("roles-bot")


@dataclass(frozen=True)
class Config:
    api_key: str
    app_name: str
    source: str  # E.164 SIN el '+'
    verify_token: str
    admins: Set[str]
    port: int


def load_config() -> Config:
    missing: List[str] = []
    api_key = os.getenv("GUPSHUP_API_KEY")
    if not api_key:
        missing.append("GUPSHUP_API_KEY")
    source = os.getenv("GUPSHUP_SOURCE")
    if not source:
        missing.append("GUPSHUP_SOURCE")

    if missing:
        raise RuntimeError(f"Faltan variables: {', '.join(missing)}")

    app_name = os.getenv("GUPSHUP_APP_NAME", "RolesClubBotToastmasters")
    verify = os.getenv("VERIFY_TOKEN", "rolesclub-verify")
    admins = {n.strip() for n in os.getenv("ADMIN_NUMBERS", "").split(",") if n.strip()}
    port = int(os.getenv("PORT", "5000"))
    return Config(api_key=api_key, app_name=app_name, source=source, verify_token=verify, admins=admins, port=port)


CFG = load_config()
HEADERS_FORM = {"apikey": CFG.api_key, "Content-Type": "application/x-www-form-urlencoded"}

# ------------------------------------------------------------------------------
# 2) Rutas de archivos y carga del club
# ------------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
CLUB_FILE = BASE_DIR / "data" / "club.json"
STATE_FILE = BASE_DIR / "data" / "state.json"

if not CLUB_FILE.exists():
    raise FileNotFoundError(
        f"No se encontró {CLUB_FILE}. Ejecuta primero: python src/setup_club.py"
    )

# Cargar catálogo del club (roles + miembros + niveles)
club = Club()
club.load_from_json(str(CLUB_FILE))

ALL_NUMBERS: Tuple[str, ...] = tuple(m.waid for m in club.members)

# ------------------------------------------------------------------------------
# 3) Persistencia del estado (JSON atómico con lock)
# ------------------------------------------------------------------------------

def _dump_json_atomic(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as tmp:
        json.dump(obj, tmp, ensure_ascii=False, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


class StateStore:
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
    return {
        "round": 0,
        "pending": {},
        "accepted": {},
        "members_cycle": {m.waid: [] for m in club.members},
        "last_summary": None,
        "canceled": False,
    }


state_store = StateStore(STATE_FILE)

# ------------------------------------------------------------------------------
# 4) Utilidades de negocio
# ------------------------------------------------------------------------------

def pending_candidates(st: dict, exclude_role: Optional[str] = None) -> Set[str]:
    """
    Devuelve el conjunto de waid actualmente propuestos en 'pending'.
    Si exclude_role se indica, no considera el candidato de ese rol.
    """
    cands: Set[str] = set()
    for r, info in st.get("pending", {}).items():
        if exclude_role is not None and r == exclude_role:
            continue
        cand = info.get("candidate")
        if cand:
            cands.add(cand)
    return cands


def send_text(to_e164_no_plus: str, text: str) -> dict:
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
    ok = fail = 0
    for n in numbers:
        res = send_text(n, text)
        if res.get("ok") is False or res.get("status_code", 200) >= 400:
            fail += 1
        else:
            ok += 1
    return {"ok": ok, "fail": fail}


def pretty_name(waid: str) -> str:
    member = next((m for m in club.members if m.waid == waid), None)
    return member.name if member else waid


def roles_left_for_member(waid: str) -> List[str]:
    st = state_store.load()
    done = set(st["members_cycle"].get(waid, []))
    return [r.name for r in club.roles if r.name not in done]


def choose_candidate(role: str, excluded: Set[str]) -> Optional[str]:
    st = state_store.load()
    # Buscar el rol para obtener su dificultad
    role_obj = next((r for r in club.roles if r.name == role), None)
    if not role_obj:
        return None
    
    eligible: List[str] = []
    for m in club.members:
        if m.waid in excluded:
            continue
        # Solo considerar miembros cuyo nivel >= dificultad del rol
        if m.level < role_obj.difficulty:
            continue
        done = set(st["members_cycle"].get(m.waid, []))
        if role not in done:
            eligible.append(m.waid)
    
    if not eligible:
        # Fallback: cualquier miembro con nivel suficiente (ignora si ya hizo el rol)
        eligible = [m.waid for m in club.members 
                   if m.waid not in excluded and m.level >= role_obj.difficulty]
    
    return random.choice(eligible) if eligible else None


def make_summary(st: dict) -> str:
    lines = [f"🗓️ Reunión #{st['round']} – Roles asignados:"]
    for role in [r.name for r in club.roles]:
        if role in st["accepted"]:
            w = st["accepted"][role]["waid"]
            lines.append(f"• {role}: {pretty_name(w)}")
        else:
            lines.append(f"• {role}: (pendiente)")
    return "\n".join(lines)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()

def admin_list_members() -> str:
    """Devuelve un texto con la lista de miembros y su nivel."""
    if not club.members:
        return "No hay miembros en el club."
    lines = ["👥 Miembros del club:"]
    for m in club.members:
        lines.append(f"• {m.name} — {m.waid} (nivel {getattr(m, 'level', 1)})")
    return "\n".join(lines)

def _find_member_by_waid_or_name(token: str):
    token = token.strip().lower()
    # buscar por waid exacto
    m = next((m for m in club.members if m.waid == token), None)
    if m:
        return m
    # buscar por nombre (case-insensitive)
    return next((m for m in club.members if m.name.strip().lower() == token), None)

def admin_add_member(name: str, waid: str, level: int = 1, is_guest: bool = False) -> str:
    from models import Member  # evitar import circular arriba si lo mueves
    name = name.strip()
    waid = "".join(ch for ch in waid if ch.isdigit())  # sanitiza E.164 sin '+'
    if not name or not waid:
        return "Formato inválido. Usa: AGREGAR Nombre, 521XXXXXXXXXX"
    # ya existe ese número
    if any(m.waid == waid for m in club.members):
        return f"Ya existe un miembro con ese número: {waid}"
    new_m = Member(name=name, waid=waid, is_guest=is_guest, level=level)
    club.members.append(new_m)
    # agregar entrada a members_cycle en el estado
    st = state_store.load()
    st["members_cycle"][waid] = []
    state_store.save(st)
    club.save_to_json(str(CLUB_FILE))
    return f"✅ Agregado: {name} — {waid} (nivel {level})"

def admin_remove_member(waid_or_name: str) -> str:
    target = _find_member_by_waid_or_name(waid_or_name)
    if not target:
        return "No encontré al miembro. Usa: ELIMINAR 521XXXXXXXXXX (o nombre exacto)."
    # no permitir borrar si está en roles pendientes/aceptados de la ronda
    st = state_store.load()
    in_pending = any(d["candidate"] == target.waid and not d.get("accepted") for d in st.get("pending", {}).values())
    in_accepted = any(v["waid"] == target.waid for v in st.get("accepted", {}).values())
    if in_pending or in_accepted:
        return "No se puede eliminar: el miembro tiene roles pendientes o aceptados en la ronda actual."

    # eliminar del catálogo
    club.members = [m for m in club.members if m.waid != target.waid]
    club.save_to_json(str(CLUB_FILE))

    # limpiar del estado (ciclo)
    st["members_cycle"].pop(target.waid, None)
    state_store.save(st)
    return f"🗑️ Eliminado: {target.name} — {target.waid}"


ADMIN_CMDS: Dict[str, str] = {
    "iniciar": "start",
    "estado": "status",
    "cancelar": "cancel",
    "reset": "reset",
}

USER_CMDS: Dict[str, str] = {
    "acepto": "accept",
    "rechazo": "reject",
    "mi rol": "whoami",
    "hola": "hello",
}

# ------------------------------------------------------------------------------
# 5) Reglas de la ronda
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


    for role in [r.name for r in club.roles]:
        # 1er intento: evitar duplicar candidatos dentro de la misma ronda
        excluded = set(a["waid"] for a in st["accepted"].values())
        excluded.update(pending_candidates(st))  # evita que una misma persona reciba 2 roles

        cand = choose_candidate(role, excluded)

        # Fallback: si no hay nadie (pocos miembros vs muchos roles), relajamos la exclusión
        if not cand:
            excluded = set(a["waid"] for a in st["accepted"].values())
            cand = choose_candidate(role, excluded)

        if not cand:
            # si aún así no hay candidato, pasa al siguiente rol
            continue

        st["pending"][role] = {"candidate": cand, "declined_by": [], "accepted": False}
    state_store.save(st)

    for role, info in st["pending"].items():
        cand = info["candidate"]
        send_text(
            cand,
            f"Hola {pretty_name(cand)} 👋\n"
            f"Para la reunión #{st['round']} te propongo el rol *{role}*.\n\n"
            f"Responde:\n• *ACEPTO* para confirmar\n• *RECHAZO* si no puedes",
        )

    broadcast_text(CFG.admins, f"✅ Ronda #{st['round']} iniciada por {by_admin}.")
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
            if len(done_list) >= len(club.roles):
                done_list = []
            st["members_cycle"][waid] = done_list

            # 🔑 Actualiza progreso también en club.json (sube nivel y guarda)
            member = next((m for m in club.members if m.waid == waid), None)
            role_obj = next((r for r in club.roles if r.name == role), None)
            if member and role_obj:
                member.add_role(role_obj)
                club.save_to_json(str(CLUB_FILE))

            state_store.save(st)
            send_text(waid, f"🎉 ¡Gracias {pretty_name(waid)}! Quedaste como *{role}* en la reunión #{st['round']}.")
            check_and_announce_if_complete()
            return f"{pretty_name(waid)} aceptó {role}."
    return "Nada que aceptar."


def handle_reject(waid: str) -> str:
    st = state_store.load()
    for role, info in list(st["pending"].items()):
        if info["candidate"] == waid and not info["accepted"]:
            info["declined_by"].append(waid)
            excluded = set(info["declined_by"])
            excluded.update(a["waid"] for a in st["accepted"].values())
            # Evita proponer a alguien ya pendiente para otro rol
            excluded.update(pending_candidates(st, exclude_role=role))

            cand = choose_candidate(role, excluded)

            # Fallback: si no alcanzan los miembros, relajamos la exclusión de 'pending'
            if not cand:
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
                    f"¿Podrías tomar el rol *{role}* en la reunión #{st['round']}?\n"
                    f"Responde *ACEPTO* o *RECHAZO*.",
                )
                return f"{pretty_name(waid)} rechazó {role}. Nuevo candidato: {pretty_name(cand)}"
            else:
                del st["pending"][role]
                state_store.save(st)
                broadcast_text(CFG.admins, f"⚠️ No hay candidato disponible para {role}.")
                return "Sin candidatos."
    return "Nada que rechazar."


def check_and_announce_if_complete() -> None:
    st = state_store.load()
    all_ok = all(role in st["accepted"] for role in [r.name for r in club.roles])
    if not all_ok or st.get("canceled"):
        return
    summary = make_summary(st)
    if st.get("last_summary") == summary:
        return
    st["last_summary"] = summary
    state_store.save(st)
    broadcast_text(ALL_NUMBERS, f"✅ {summary}\n\n¡Nos vemos en la próxima reunión!")


def who_am_i(waid: str) -> str:
    st = state_store.load()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            return f"Tienes pendiente el rol *{role}* en la ronda #{st['round']}."
    for role, acc in st["accepted"].items():
        if acc["waid"] == waid:
            return f"Ya aceptaste el rol *{role}* en la ronda #{st['round']}."
    return "No tienes asignaciones pendientes."


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
    broadcast_text(ALL_NUMBERS, "⚠️ La ronda fue cancelada por el administrador.")
    return f"Ronda #{st['round']} cancelada."


def reset_all(by_admin: str) -> str:
    st = default_state()
    state_store.save(st)
    broadcast_text(CFG.admins, f"🔄 Estado reiniciado por {by_admin}.")
    return "Estado reiniciado."

# ------------------------------------------------------------------------------
# 6) Flask app
# ------------------------------------------------------------------------------

app = Flask(__name__)

@app.route("/", methods=["GET"])
def health():
    return {"ok": True, "app": CFG.app_name, "roles": [r.name for r in club.roles], "members": len(club.members)}


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
    data = request.get_json(force=True, silent=True) or {}
    try:
        value = (
            (data.get("entry") or [{}])[0]
            .get("changes", [{}])[0]
            .get("value", {})
        )
        for msg in value.get("messages", []):
            if msg.get("type") != "text":
                continue
            waid = msg.get("from", "")
            body_raw = msg.get("text", {}).get("body", "")
            body = norm(body_raw)
            log.info("Mensaje de %s: %s", waid, body)

            # Comandos admin (exactos + con argumentos)
            if waid in CFG.admins:
                # comandos exactos
                if body in ADMIN_CMDS:
                    cmd = ADMIN_CMDS[body]
                    if cmd == "start":
                        out = start_new_round(pretty_name(waid))
                        send_text(waid, out)
                    elif cmd == "status":
                        send_text(waid, status_text())
                    elif cmd == "cancel":
                        send_text(waid, cancel_round(pretty_name(waid)))
                    elif cmd == "reset":
                        send_text(waid, reset_all(pretty_name(waid)))
                    continue

                # prefijos con argumentos
                # MIEMBROS → lista
                if body == "miembros":
                    send_text(waid, admin_list_members())
                    continue

                # AGREGAR Nombre, 521XXXXXXXXXX
                if body.startswith("agregar "):
                    # usa el body_raw para conservar mayúsculas/acentos del nombre
                    tail = body_raw.strip()[len("agregar "):]
                    # formato "Nombre, 521XXXXXXXXXX"
                    if "," in tail:
                        name, num = tail.split(",", 1)
                    else:
                        # o "Nombre 521XXXXXXXXXX" separado por espacio
                        parts = tail.rsplit(" ", 1)
                        if len(parts) != 2:
                            send_text(waid, "Formato inválido. Usa: AGREGAR Nombre, 521XXXXXXXXXX")
                            continue
                        name, num = parts[0], parts[1]
                    out = admin_add_member(name.strip(), num.strip())
                    send_text(waid, out)
                    continue

                # ELIMINAR 521XXXXXXXXXX  (o nombre exacto)
                if body.startswith("eliminar "):
                    tail = body_raw.strip()[len("eliminar "):]
                    out = admin_remove_member(tail.strip())
                    send_text(waid, out)
                    continue

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
                    send_text(waid, "¡Hola! Soy RolesClubBot 🤖.")
            else:
                if waid in CFG.admins:
                    send_text(waid,
                    "Comandos admin: INICIAR | ESTADO | CANCELAR | RESET\n"
                    "• MIEMBROS — lista miembros\n"
                    "• AGREGAR Nombre, 521XXXXXXXXXX\n"
                    "• ELIMINAR 521XXXXXXXXXX (o nombre exacto)\n\n"
                    "Usuarios: MI ROL | ACEPTO | RECHAZO | HOLA"
                    )
                else:
                    send_text(waid, f"Recibí: {body_raw}. Escribe *MI ROL*, *ACEPTO* o *RECHAZO*.")

    except Exception:
        log.exception("Error procesando webhook; payload=%s", data)

    return jsonify({"status": "ok"})

# ------------------------------------------------------------------------------
# 7) Main
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CFG.port, debug=False)
