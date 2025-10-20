# app.py
# --------------------------------------------------------------------------------------
# WhatsApp roles bot (Flask + Gupshup) - MULTI-CLUB con MENÚS NUMÉRICOS
#
# - Carga todos los clubes desde data/clubs/registry.json
# - Cada club tiene su propio {club.json, state.json} en data/clubs/<club_id>/
# - Asignación de roles con priorización por dificultad y ciclo de roles.
# - Interfaz 100% por MENÚS numéricos para usuarios y administradores.
#   • Miembro: ve su menú de miembro.
#   • Admin: ve su menú de admin.
#   • Admin y miembro: menú raíz que separa ambos.
#   • Invitaciones: siempre ofrece 1 Aceptar / 2 Rechazar / 3 Responder después.
#
# .env mínimo:
#   GUPSHUP_API_KEY=...
#   GUPSHUP_APP_NAME=RolesClubBot
#   GUPSHUP_SOURCE=917834811114
#   CLUBS_DIR=data/clubs
#   VERIFY_TOKEN=rolesclub-verify
#   PORT=5000
# --------------------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import os
import random
import re
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

# Modelo POO existente
from models import Club, Member, Role

# ======================================================================================
# 1) Configuración y logging
# ======================================================================================

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
    port: int
    clubs_dir: Path


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

    app_name = os.getenv("GUPSHUP_APP_NAME", "RolesClubBot")
    verify = os.getenv("VERIFY_TOKEN", "rolesclub-verify")
    port = int(os.getenv("PORT", "5000"))
    clubs_dir = Path(os.getenv("CLUBS_DIR", "data/clubs"))
    return Config(
        api_key=api_key,
        app_name=app_name,
        source=source,
        verify_token=verify,
        port=port,
        clubs_dir=clubs_dir,
    )


CFG = load_config()
HEADERS_FORM = {"apikey": CFG.api_key, "Content-Type": "application/x-www-form-urlencoded"}


# ======================================================================================
# 2) Multi-club: registro y contexto por club
# ======================================================================================

REGISTRY_FILE = CFG.clubs_dir / "registry.json"


@dataclass
class Ctx:
    club_id: str
    club: Club
    state_store: "StateStore"
    club_file: Path
    admins: Set[str]
    all_numbers: Tuple[str, ...]
    members_index: Set[str]  # waids para resolver club por miembro


def load_registry() -> dict:
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    return {"clubs": {}}


# --- Persistencia del estado (JSON atómico con lock) ---
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
                st = {
                    "round": 0,
                    "pending": {},
                    "accepted": {},
                    "members_cycle": {},
                    "last_summary": None,
                    "canceled": False,
                }
                _dump_json_atomic(self.path, st)
                return st
            return json.loads(self.path.read_text(encoding="utf-8"))

    def save(self, st: dict) -> None:
        with self._lock:
            _dump_json_atomic(self.path, st)


# --- Carga de clubes al arranque ---
_CTX: Dict[str, Ctx] = {}


def load_club_into_registry(club_id: str, meta: dict):
    club_dir = CFG.clubs_dir / club_id
    club_file = club_dir / "club.json"
    state_file = club_dir / "state.json"
    if not club_file.exists():
        raise FileNotFoundError(f"[{club_id}] Falta {club_file}. Corre el semillador.")

    c = Club()
    c.load_from_json(str(club_file))
    st = StateStore(state_file)

    s = st.load()
    mc = s.get("members_cycle", {})
    changed = False
    for m in c.members:
        if m.waid not in mc:
            mc[m.waid] = []
            changed = True
    if changed:
        s["members_cycle"] = mc
    st.save(s)

    admins = set(meta.get("admins", []))
    ctx = Ctx(
        club_id=club_id,
        club=c,
        state_store=st,
        club_file=club_file,
        admins=admins,
        all_numbers=tuple(m.waid for m in c.members),
        members_index={m.waid for m in c.members},
    )
    _CTX[club_id] = ctx
    log.info("Cargado club %s (miembros=%d, admins=%d)", club_id, len(ctx.members_index), len(ctx.admins))


def load_all_clubs():
    reg = load_registry()
    _CTX.clear()
    for cid, meta in reg.get("clubs", {}).items():
        load_club_into_registry(cid, meta)


load_all_clubs()


def admin_clubs(waid: str) -> List[str]:
    return [cid for cid, ctx in _CTX.items() if waid in ctx.admins]


def member_club(waid: str) -> Optional[str]:
    for cid, ctx in _CTX.items():
        if waid in ctx.members_index:
            return cid
    return None


# ======================================================================================
# 3) Utilidades (normalización, números MX y negocio)
# ======================================================================================

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


def mx_public_from_internal(waid: str) -> str:
    digits = "".join(ch for ch in waid if ch.isdigit())
    if digits.startswith("521") and len(digits) >= 13:
        return digits[-10:]
    return digits


def mx_internal_from_any(s: str) -> str:
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 10:
        return "521" + digits
    if digits.startswith("521") and len(digits) >= 13:
        return digits
    return digits


def pending_candidates(st: dict, exclude_role: Optional[str] = None) -> Set[str]:
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


def pretty_name(ctx: Ctx, waid: str) -> str:
    m = next((m for m in ctx.club.members if m.waid == waid), None)
    return m.name if m else waid


def role_min_level(ctx: Ctx, role_name: str) -> int:
    r = next((r for r in ctx.club.roles if r.name == role_name), None)
    return max(1, int(getattr(r, "difficulty", 1) or 1)) if r else 1


def choose_candidate_hier(ctx: Ctx, role: str, excluded: Set[str]) -> Optional[str]:
    st = ctx.state_store.load()
    min_lvl = role_min_level(ctx, role)

    def lvl(m: Member) -> int:
        return int(getattr(m, "level", 1) or 1)

    def pool(filter_fn, allow_repeat: bool) -> List[str]:
        res = []
        for m in ctx.club.members:
            if m.waid in excluded:
                continue
            if not filter_fn(m):
                continue
            done = set(st["members_cycle"].get(m.waid, []))
            if not allow_repeat and role in done:
                continue
            res.append(m.waid)
        return res

    p = pool(lambda m: lvl(m) >= min_lvl, allow_repeat=False)
    if p:
        return random.choice(p)

    p = pool(lambda m: lvl(m) >= min_lvl, allow_repeat=True)
    if p:
        return random.choice(p)

    for L in range(min_lvl - 1, 0, -1):
        p = pool(lambda m, L=L: lvl(m) == L, allow_repeat=False)
        if p:
            return random.choice(p)
        p = pool(lambda m, L=L: lvl(m) == L, allow_repeat=True)
        if p:
            return random.choice(p)

    return None


# --- Admin helpers --------------------------------------------------------------------

def admin_list_members(ctx: Ctx) -> str:
    if not ctx.club.members:
        return f"No hay miembros registrados aún en {ctx.club_id}."
    lines = [f"Miembros de {ctx.club_id}"]
    for m in ctx.club.members:
        pub = mx_public_from_internal(m.waid)
        lines.append(f"- {m.name} — {pub}  · nivel {getattr(m, 'level', 1)}")
    admin_labels = []
    for a in ctx.admins:
        member = next((m for m in ctx.club.members if m.waid == a), None)
        if member:
            admin_labels.append(f"{member.name} ({mx_public_from_internal(a)})")
        else:
            admin_labels.append(mx_public_from_internal(a))
    if admin_labels:
        lines.append("")
        lines.append("Administradores: " + ", ".join(admin_labels))
    return "\n".join(lines)


def _find_member_by_waid_or_name(ctx: Ctx, token: str):
    t_clean = token.strip()
    digits = "".join(ch for ch in t_clean if ch.isdigit())
    if digits:
        target = mx_internal_from_any(digits)
        m = next((m for m in ctx.club.members if m.waid == target), None)
        if m:
            return m
    t_norm = norm(t_clean)
    return next((m for m in ctx.club.members if norm(m.name) == t_norm), None)


def admin_add_member(ctx: Ctx, name: str, raw_number: str, level: int = 1, is_guest: bool = False) -> str:
    name = name.strip()
    waid = mx_internal_from_any(raw_number)
    if not name or not waid:
        return "Formato no válido. Usa: Nombre, 55XXXXXXXX"
    if any(m.waid == waid for m in ctx.club.members):
        return "Ese número ya está registrado en el club."

    new_m = Member(name=name, waid=waid, is_guest=is_guest, level=level)
    ctx.club.members.append(new_m)

    # estado
    st = ctx.state_store.load()
    st.setdefault("members_cycle", {})[waid] = []
    ctx.state_store.save(st)

    # persistencia en disco
    ctx.club.save_to_json(str(ctx.club_file))

    # >>>>>>>>>> ACTUALIZA ÍNDICES EN MEMORIA <<<<<<<<<<
    ctx.members_index.add(waid)
    ctx.all_numbers = tuple(m.waid for m in ctx.club.members)

    return f"Listo: {name} agregado a {ctx.club_id} (tel. {mx_public_from_internal(waid)}, nivel {level})."


# --- reemplaza admin_remove_member ---
def admin_remove_member(ctx: Ctx, waid_or_name: str) -> str:
    target = _find_member_by_waid_or_name(ctx, waid_or_name)
    if not target:
        return "No encontré a esa persona. Ingresa 10 dígitos MX o el nombre exacto."

    st = ctx.state_store.load()
    in_pending = any(d["candidate"] == target.waid and not d.get("accepted") for d in st.get("pending", {}).values())
    in_accepted = any(v["waid"] == target.waid for v in st.get("accepted", {}).values())
    if in_pending or in_accepted:
        return "No se puede eliminar ahora: tiene un rol pendiente o aceptado en esta ronda."

    # quita del modelo y persiste
    ctx.club.members = [m for m in ctx.club.members if m.waid != target.waid]
    ctx.club.save_to_json(str(ctx.club_file))
    st["members_cycle"].pop(target.waid, None)
    ctx.state_store.save(st)

    # >>>>>>>>>> ACTUALIZA ÍNDICES EN MEMORIA <<<<<<<<<<
    if target.waid in ctx.members_index:
        ctx.members_index.remove(target.waid)
    ctx.all_numbers = tuple(m.waid for m in ctx.club.members)

    return f"Eliminado de {ctx.club_id}: {target.name} (tel. {mx_public_from_internal(target.waid)})."


# ======================================================================================
# 4) Reglas de la ronda (multi-club)
# ======================================================================================

def start_new_round(ctx: Ctx, by_admin: str) -> str:
    st = ctx.state_store.load()
    if any(not v.get("accepted") for v in st["pending"].values()):
        return "Ya hay invitaciones pendientes. Primero cierra o cancela esa ronda."

    st["round"] += 1
    st["pending"] = {}
    st["accepted"] = {}
    st["last_summary"] = None
    st["canceled"] = False

    roles_sorted = sorted(ctx.club.roles, key=lambda r: int(getattr(r, "difficulty", 1) or 1), reverse=True)

    for r in roles_sorted:
        role = r.name
        excluded = set(a["waid"] for a in st["accepted"].values())
        excluded.update(pending_candidates(st))
        cand = choose_candidate_hier(ctx, role, excluded)
        if not cand:
            continue
        st["pending"][role] = {"candidate": cand, "declined_by": [], "accepted": False}

    ctx.state_store.save(st)

    # Invitación con menú 1/2/3
    for role, info in st["pending"].items():
        cand = info["candidate"]
        send_text(
            cand,
            f"{pretty_name(ctx, cand)}, se te propone el rol {role} para la reunión #{st['round']}.\n"
            "Elige una opción y envía solo el número:\n"
            "1) Aceptar\n"
            "2) Rechazar\n"
            "3) Responder después"
        )

    assigned_roles = set(st["pending"].keys())
    not_assigned = [r.name for r in ctx.club.roles if r.name not in assigned_roles]
    if not_assigned:
        broadcast_text(
            ctx.admins,
            f"[{ctx.club_id}] Algunos roles quedaron sin candidato: {', '.join(not_assigned)}. "
            "Agrega más miembros o intenta de nuevo."
        )

    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] Iniciamos la ronda #{st['round']}.")
    return f"Ronda #{st['round']} iniciada en {ctx.club_id}."


def handle_accept(ctx: Ctx, waid: str) -> str:
    st = ctx.state_store.load()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            info["accepted"] = True
            st["accepted"][role] = {"waid": waid, "name": pretty_name(ctx, waid)}
            done_list = list(st["members_cycle"].get(waid, []))
            if role not in done_list:
                done_list.append(role)
            if len(done_list) >= len(ctx.club.roles):
                done_list = []
            st["members_cycle"][waid] = done_list
            member = next((m for m in ctx.club.members if m.waid == waid), None)
            role_obj = next((r for r in ctx.club.roles if r.name == role), None)
            if member and role_obj:
                member.add_role(role_obj)
                ctx.club.save_to_json(str(ctx.club_file))
            ctx.state_store.save(st)
            check_and_announce_if_complete(ctx)
            return f"Aceptado: {role} por {pretty_name(ctx, waid)}."
    return "No hay nada pendiente para aceptar."


def handle_reject(ctx: Ctx, waid: str) -> str:
    st = ctx.state_store.load()
    for role, info in list(st["pending"].items()):
        if info.get("candidate") == waid and not info.get("accepted"):
            info["declined_by"].append(waid)

            excluded = set(info["declined_by"])
            excluded.update(a["waid"] for a in st.get("accepted", {}).values())
            excluded.update(pending_candidates(st, exclude_role=role))

            cand = choose_candidate_hier(ctx, role, excluded)
            if cand:
                info["candidate"] = cand
                ctx.state_store.save(st)
                send_text(
                    cand,
                    f"Se te propone el rol {role} en la reunión #{st['round']}.\n"
                    "Elige una opción y envía solo el número:\n"
                    "1) Aceptar\n"
                    "2) Rechazar\n"
                    "3) Responder después"
                )
                return f"Rechazado por {pretty_name(ctx, waid)}. Nuevo candidato: {pretty_name(ctx, cand)}."
            else:
                del st["pending"][role]
                ctx.state_store.save(st)
                broadcast_text(ctx.admins, f"[{ctx.club_id}] No hay más opciones para el rol: {role}.")
                return "Sin candidatos."
    return "No hay nada pendiente para rechazar."


def make_summary(ctx: Ctx, st: dict) -> str:
    lines = [f"Reunión #{st['round']} — Resumen de roles"]
    for role in [r.name for r in ctx.club.roles]:
        if role in st["accepted"]:
            w = st["accepted"][role]["waid"]
            lines.append(f"- {role}: {pretty_name(ctx, w)}")
        else:
            lines.append(f"- {role}: por confirmar")
    return "\n".join(lines)


def check_and_announce_if_complete(ctx: Ctx) -> None:
    st = ctx.state_store.load()
    all_ok = all(role in st["accepted"] for role in [r.name for r in ctx.club.roles])
    if not all_ok or st.get("canceled"):
        return
    summary = make_summary(ctx, st)
    if st.get("last_summary") == summary:
        return
    st["last_summary"] = summary
    ctx.state_store.save(st)
    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] {summary}")


def who_am_i(ctx: Ctx, waid: str) -> str:
    st = ctx.state_store.load()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            return (
                f"Tienes una invitación pendiente: {role} en la ronda #{st['round']} ({ctx.club_id}).\n"
                "Elige una opción y envía solo el número:\n"
                "1) Aceptar\n"
                "2) Rechazar\n"
                "3) Responder después"
            )
    for role, acc in st["accepted"].items():
        if acc["waid"] == waid:
            return f"Confirmaste el rol {role} en la ronda #{st['round']} ({ctx.club_id})."
    return "No tienes roles asignados ni pendientes."


def status_text(ctx: Ctx) -> str:
    st = ctx.state_store.load()
    lines = [make_summary(ctx, st), "", "Pendientes por confirmar:"]
    any_pending = False
    for role, info in st["pending"].items():
        if not info["accepted"]:
            any_pending = True
            cand = info["candidate"]
            lines.append(f"- {role}: propuesto a {pretty_name(ctx, cand)} (rechazos: {len(info['declined_by'])})")
    if not any_pending:
        lines.append("- Ninguno")
    if st.get("canceled"):
        lines.append("\nEstado: Ronda cancelada.")
    return "\n".join(lines)


def cancel_round(ctx: Ctx, by_admin: str) -> str:
    st = ctx.state_store.load()
    st["pending"] = {}
    st["accepted"] = {}
    st["last_summary"] = None
    st["canceled"] = True
    ctx.state_store.save(st)
    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] La ronda se canceló.")
    return f"La ronda #{st['round']} fue cancelada."


def reset_all(ctx: Ctx, by_admin: str) -> str:
    st = {
        "round": 0,
        "pending": {},
        "accepted": {},
        "members_cycle": {m.waid: [] for m in ctx.club.members},
        "last_summary": None,
        "canceled": False,
    }
    ctx.state_store.save(st)
    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] Se reinició el estado del club.")
    return "Estado del club reiniciado."


# ======================================================================================
# 5) Sesiones y menús
# ======================================================================================

# Memoria en proceso para navegación por menús.
SESSION: Dict[str, dict] = {}
SLOCK = Lock()

def get_session(waid: str) -> dict:
    with SLOCK:
        s = SESSION.get(waid)
        if not s:
            s = {"mode": "root", "club": None, "awaiting": None, "buffer": None}
            SESSION[waid] = s
        return s

def set_session(waid: str, **kwargs) -> None:
    with SLOCK:
        s = SESSION.setdefault(waid, {"mode": "root", "club": None, "awaiting": None, "buffer": None})
        s.update(kwargs)

# ----- Menús -----

def render_root_menu(waid: str) -> str:
    mclub = member_club(waid)
    aclubs = admin_clubs(waid)
    opts = []
    idx = 1
    if mclub:
        opts.append(f"{idx}) Menú de miembro ({mclub})"); idx += 1
    if aclubs:
        if len(aclubs) == 1:
            opts.append(f"{idx}) Menú de admin ({aclubs[0]})"); idx += 1
        else:
            opts.append(f"{idx}) Menú de admin (elegir club)"); idx += 1
    opts.append(f"{idx}) Mi estado de rol") ; idx += 1
    return "Elige una opción y envía solo el número:\n" + "\n".join(opts)

def render_member_menu(ctx: Ctx) -> str:
    return (
        f"[{ctx.club_id}] Menú miembro\n"
        "Elige una opción y envía solo el número:\n"
        "1) Mi rol (pendiente/confirmado)\n"
        "2) Estado de la ronda\n"
        "9) Volver"
    )

def render_admin_club_picker(aclubs: List[str]) -> str:
    lines = ["Elige club para administrar (envía solo el número):"]
    for i, cid in enumerate(aclubs, 1):
        lines.append(f"{i}) {cid}")
    lines.append("9) Volver")
    return "\n".join(lines)

def render_admin_menu(ctx: Ctx) -> str:
    return (
        f"[{ctx.club_id}] Menú admin\n"
        "Elige una opción y envía solo el número:\n"
        "1) Iniciar ronda\n"
        "2) Ver estado\n"
        "3) Cancelar ronda\n"
        "4) Resetear estado\n"
        "5) Ver miembros\n"
        "6) Agregar miembro\n"
        "7) Eliminar miembro\n"
        "8) Cambiar de club\n"
        "9) Volver"
    )

def send_invite_menu(ctx: Ctx, waid: str, role: str, round_no: int) -> None:
    send_text(
        waid,
        f"Invitación pendiente: {role} en la reunión #{round_no} ({ctx.club_id}).\n"
        "Elige una opción y envía solo el número:\n"
        "1) Aceptar\n"
        "2) Rechazar\n"
        "3) Responder después"
    )

# ======================================================================================
# 6) Flask app (endpoints y webhook)
# ======================================================================================

app = Flask(__name__)


@app.route("/", methods=["GET"])
def health():
    info = {}
    for cid, ctx in _CTX.items():
        info[cid] = {"members": len(ctx.members_index), "roles": [r.name for r in ctx.club.roles]}
    return {"ok": True, "app": CFG.app_name, "clubs": info}


@app.route("/webhook", methods=["GET"])
def webhook_get():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == CFG.verify_token:
        return challenge or "OK", 200
    return "ok", 200


def extract_trailing_club_id(text: str) -> Optional[str]:
    t = text.strip()
    m = re.search(r"\[([^\]]+)\]\s*$", t)
    if m:
        last = m.group(1).strip()
        return last if last in _CTX else None
    parts = t.split()
    if len(parts) >= 2:
        last = parts[-1].strip()
        if last in _CTX:
            return last
    return None


def strip_trailing_club(text: str, cid: str) -> str:
    t = text.strip()
    t = re.sub(rf"\s*\[\s*{re.escape(cid)}\s*\]\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(rf"\s+{re.escape(cid)}\s*$", "", t, flags=re.IGNORECASE)
    return t.strip()


def infer_user_club(waid: str, explicit_cid: Optional[str] = None) -> Optional[str]:
    if explicit_cid and explicit_cid in _CTX:
        return explicit_cid
    cid = member_club(waid)
    if cid:
        return cid
    candidates = []
    for cid, ctx in _CTX.items():
        st = ctx.state_store.load()
        for info in st.get("pending", {}).values():
            if info.get("candidate") == waid and not info.get("accepted"):
                candidates.append(cid); break
        for info in st.get("accepted", {}).values():
            if info.get("waid") == waid:
                candidates.append(cid); break
    if len(candidates) == 1:
        return candidates[0]
    return None


def has_pending_invite(ctx: Ctx, waid: str) -> Optional[str]:
    st = ctx.state_store.load()
    for role, info in st.get("pending", {}).items():
        if info.get("candidate") == waid and not info.get("accepted"):
            return role
    return None


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
            s = get_session(waid)

            # ---------------------- 0) Si viene un número puro ----------------------
            is_number = re.fullmatch(r"\d{1,3}", body) is not None

            # Resolver club por defecto
            if not s.get("club"):
                mc = member_club(waid)
                acls = admin_clubs(waid)
                if mc:
                    set_session(waid, club=mc)
                elif len(acls) == 1:
                    set_session(waid, club=acls[0])

            # Prioridad: si hay invitación pendiente en el club actual, 1/2/3 operan eso
            current_cid = s.get("club") or infer_user_club(waid, extract_trailing_club_id(body_raw))
            if current_cid and current_cid in _CTX:
                ctx = _CTX[current_cid]
                role_pending = has_pending_invite(ctx, waid)
            else:
                ctx = None
                role_pending = None

            if is_number and role_pending and body in ("1", "2", "3"):
                if body == "1":
                    send_text(waid, handle_accept(ctx, waid))
                elif body == "2":
                    send_text(waid, handle_reject(ctx, waid))
                else:
                    # Responder después: solo confirmar recepción y mantener pendiente
                    st = ctx.state_store.load()
                    send_text(waid, f"Queda pendiente tu respuesta para {role_pending} en la ronda #{st['round']} ({ctx.club_id}).")
                # Después de gestionar, mostrar menú raíz
                set_session(waid, mode="root", awaiting=None, buffer=None)
                send_text(waid, render_root_menu(waid))
                continue

            # ---------------------- 1) Router por estado de sesión -------------------
            awaiting = s.get("awaiting")
            if is_number:
                # Menú raíz
                if s.get("mode") == "root":
                    idx = 1
                    mclub = member_club(waid)
                    aclubs = admin_clubs(waid)
                    if mclub:
                        if body == str(idx):
                            set_session(waid, mode="member", club=mclub, awaiting=None)
                            send_text(waid, render_member_menu(_CTX[mclub])); continue
                        idx += 1
                    if aclubs:
                        if len(aclubs) == 1:
                            if body == str(idx):
                                set_session(waid, mode="admin", club=aclubs[0], awaiting=None)
                                send_text(waid, render_admin_menu(_CTX[aclubs[0]])); continue
                            idx += 1
                        else:
                            if body == str(idx):
                                set_session(waid, mode="admin_pick", awaiting="pick_admin_club")
                                send_text(waid, render_admin_club_picker(aclubs)); continue
                            idx += 1
                    if body == str(idx):
                        # Mi estado de rol
                        cid = infer_user_club(waid)
                        if cid and cid in _CTX:
                            send_text(waid, who_am_i(_CTX[cid], waid))
                        else:
                            send_text(waid, "No se pudo determinar tu club. Pide a un admin que te agregue.")
                        send_text(waid, render_root_menu(waid)); continue

                # Picker de club admin
                if s.get("mode") == "admin_pick" and awaiting == "pick_admin_club":
                    aclubs = admin_clubs(waid)
                    if body == "9":
                        set_session(waid, mode="root", awaiting=None, buffer=None)
                        send_text(waid, render_root_menu(waid)); continue
                    try:
                        idx = int(body) - 1
                        cid = aclubs[idx]
                        set_session(waid, mode="admin", club=cid, awaiting=None)
                        send_text(waid, render_admin_menu(_CTX[cid])); continue
                    except Exception:
                        send_text(waid, render_admin_club_picker(aclubs)); continue

                # Menú miembro
                if s.get("mode") == "member" and current_cid and current_cid in _CTX:
                    ctx = _CTX[current_cid]
                    if body == "1":
                        send_text(waid, who_am_i(ctx, waid))
                        send_text(waid, render_member_menu(ctx)); continue
                    if body == "2":
                        send_text(waid, status_text(ctx))
                        send_text(waid, render_member_menu(ctx)); continue
                    if body == "9":
                        set_session(waid, mode="root", awaiting=None, buffer=None)
                        send_text(waid, render_root_menu(waid)); continue

                # Menú admin
                if s.get("mode") == "admin" and current_cid and current_cid in _CTX:
                    ctx = _CTX[current_cid]
                    if body == "1":
                        send_text(waid, start_new_round(ctx, pretty_name(ctx, waid)))
                        send_text(waid, render_admin_menu(ctx)); continue
                    if body == "2":
                        send_text(waid, status_text(ctx))
                        send_text(waid, render_admin_menu(ctx)); continue
                    if body == "3":
                        send_text(waid, cancel_round(ctx, pretty_name(ctx, waid)))
                        send_text(waid, render_admin_menu(ctx)); continue
                    if body == "4":
                        send_text(waid, reset_all(ctx, pretty_name(ctx, waid)))
                        send_text(waid, render_admin_menu(ctx)); continue
                    if body == "5":
                        send_text(waid, admin_list_members(ctx))
                        send_text(waid, render_admin_menu(ctx)); continue
                    if body == "6":
                        set_session(waid, awaiting="admin_add_member", buffer=None)
                        send_text(waid, "Envía: Nombre, 55XXXXXXXX")
                        continue
                    if body == "7":
                        set_session(waid, awaiting="admin_remove_member", buffer=None)
                        send_text(waid, "Envía el número de 10 dígitos o el nombre exacto a eliminar")
                        continue
                    if body == "8":
                        aclubs = admin_clubs(waid)
                        if len(aclubs) > 1:
                            set_session(waid, mode="admin_pick", awaiting="pick_admin_club")
                            send_text(waid, render_admin_club_picker(aclubs)); continue
                        send_text(waid, render_admin_menu(ctx)); continue
                    if body == "9":
                        set_session(waid, mode="root", awaiting=None, buffer=None)
                        send_text(waid, render_root_menu(waid)); continue

            # Flujos que requieren texto libre (agregar/eliminar)
            if awaiting == "admin_add_member" and s.get("mode") == "admin" and ctx:
                tail = body_raw.strip()
                if "," in tail:
                    name, num = tail.split(",", 1)
                else:
                    parts = tail.rsplit(" ", 1)
                    if len(parts) != 2:
                        send_text(waid, "Formato no válido. Usa: Nombre, 55XXXXXXXX")
                        continue
                    name, num = parts[0], parts[1]
                out = admin_add_member(ctx, name.strip(), num.strip())
                send_text(waid, out)
                set_session(waid, awaiting=None, buffer=None)
                send_text(waid, render_admin_menu(ctx))
                continue

            if awaiting == "admin_remove_member" and s.get("mode") == "admin" and ctx:
                tail = body_raw.strip()
                out = admin_remove_member(ctx, tail)
                send_text(waid, out)
                set_session(waid, awaiting=None, buffer=None)
                send_text(waid, render_admin_menu(ctx))
                continue

            # ---------------------- 2) Fallbacks y compatibilidad --------------------
            # Compatibilidad con comandos texto (por si alguien escribe MI ROL, etc.)
            if body in ("mi rol", "mi rol?", "whoami"):
                cid = infer_user_club(waid, extract_trailing_club_id(body_raw))
                if cid and cid in _CTX:
                    send_text(waid, who_am_i(_CTX[cid], waid))
                else:
                    send_text(waid, "No se pudo determinar tu club. Pide a un admin que te agregue.")
                send_text(waid, render_root_menu(waid))
                continue

            if body in ("acepto", "accept") and ctx:
                send_text(waid, handle_accept(ctx, waid))
                send_text(waid, render_root_menu(waid))
                continue

            if body in ("rechazo", "reject") and ctx:
                send_text(waid, handle_reject(ctx, waid))
                send_text(waid, render_root_menu(waid))
                continue

            # Mensaje inicial por defecto
            send_text(waid, render_root_menu(waid))

    except Exception:
        log.exception("Error procesando webhook; payload=%s", data)

    return jsonify({"status": "ok"})


# ======================================================================================
# 7) Main
# ======================================================================================

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CFG.port, debug=False)