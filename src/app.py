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
# Esta sección carga las variables de entorno desde .env y configura el sistema de logs
# para toda la aplicación. El nivel de log se puede ajustar con LOG_LEVEL en .env

load_dotenv()  # Carga variables de entorno desde archivo .env

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("roles-bot")  # Logger principal del bot


@dataclass(frozen=True)
class Config:
    """
    Configuración global del bot. Se carga una sola vez al arranque.
    
    Atributos:
        api_key: Clave API de Gupshup para enviar mensajes
        app_name: Nombre de la app (aparece en el remitente de WhatsApp)
        source: Número de WhatsApp del bot en formato E.164 SIN el '+' (ej: 917834811114)
        verify_token: Token para validar el webhook de Gupshup
        port: Puerto donde corre Flask (default: 5000)
        clubs_dir: Ruta al directorio que contiene todos los clubes (data/clubs/)
    """
    api_key: str
    app_name: str
    source: str  # E.164 SIN el '+'
    verify_token: str
    port: int
    clubs_dir: Path


def load_config() -> Config:
    """
    Carga y valida las variables de entorno necesarias para el bot.
    
    Lanza RuntimeError si falta alguna variable crítica (GUPSHUP_API_KEY, GUPSHUP_SOURCE).
    El resto de variables tienen valores por defecto razonables.
    
    Returns:
        Config: Objeto con toda la configuración del bot
    """
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


CFG = load_config()  # Configuración global (se carga al iniciar el módulo)
HEADERS_FORM = {"apikey": CFG.api_key, "Content-Type": "application/x-www-form-urlencoded"}  # Headers para Gupshup API


# ======================================================================================
# 2) Multi-club: registro y contexto por club
# ======================================================================================
# El sistema puede manejar múltiples clubes de forma aislada. Cada club tiene su propio:
# - data/clubs/<club_id>/club.json: miembros, roles, niveles
# - data/clubs/<club_id>/state.json: estado de la ronda actual (pendientes, aceptados, ciclos)
# - data/clubs/registry.json: índice de clubes con sus administradores
#
# Al arrancar la app, se carga registry.json y se inicializa un contexto (Ctx) por cada club.
# Este contexto se guarda en memoria (_CTX) para acceso rápido durante toda la ejecución.

REGISTRY_FILE = CFG.clubs_dir / "registry.json"


@dataclass
class Ctx:
    """
    Contexto de un club específico. Contiene todo lo necesario para operar ese club:
    - club_id: identificador único del club (ej: "club_1", "club_toastmasters")
    - club: instancia del modelo Club (con members y roles)
    - state_store: manejador de persistencia para state.json
    - club_file: ruta al archivo club.json
    - admins: conjunto de waids que son administradores
    - all_numbers: tupla de todos los waids de miembros
    - members_index: conjunto de waids para búsqueda rápida
    """
    club_id: str
    club: Club
    state_store: "StateStore"
    club_file: Path
    admins: Set[str]
    all_numbers: Tuple[str, ...]
    members_index: Set[str]  # waids para resolver club por miembro


def load_registry() -> dict:
    """
    Carga el archivo registry.json que contiene el índice de clubes.
    
    Formato esperado:
    {
      "clubs": {
        "club_1": {"admins": ["521XXXXXXXXXX"]},
        "club_2": {"admins": ["521YYYYYYYYYY", "521ZZZZZZZZZ"]}
      }
    }
    
    Returns:
        dict: Estructura con la lista de clubes y sus admins
    """
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    return {"clubs": {}}


# --- Persistencia del estado (JSON atómico con lock) ---
def _dump_json_atomic(path: Path, obj: dict) -> None:
    """
    Escribe JSON de forma atómica para evitar corrupción en escrituras concurrentes.
    
    El proceso es:
    1. Crear archivo temporal en el mismo directorio
    2. Escribir contenido completo al temp
    3. Reemplazar el original con os.replace (operación atómica en POSIX)
    
    Esto garantiza que nunca quedará un archivo a medio escribir si el proceso
    se interrumpe durante la escritura.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=path.parent, encoding="utf-8") as tmp:
        json.dump(obj, tmp, ensure_ascii=False, indent=2)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


class StateStore:
    """
    Manejador de persistencia para el archivo state.json de un club.
    
    El state.json contiene el estado operativo de las rondas:
    - round: número de ronda actual
    - pending: roles pendientes de aceptar {role: {candidate, declined_by, accepted}}
    - accepted: roles ya confirmados {role: {waid, name}}
    - members_cycle: historial de roles de cada miembro {waid: [roles_done]}
    - last_summary: último resumen enviado (para evitar duplicados)
    - canceled: si la ronda fue cancelada
    
    Usa un Lock para evitar condiciones de carrera en escrituras concurrentes.
    """
    def __init__(self, path: Path):
        self.path = path
        self._lock = Lock()

    def load(self) -> dict:
        """
        Carga el estado desde disco. Si el archivo no existe, lo crea con valores iniciales.
        """
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
        """
        Guarda el estado a disco de forma atómica.
        """
        with self._lock:
            _dump_json_atomic(self.path, st)


# --- Carga de clubes al arranque ---
_CTX: Dict[str, Ctx] = {}  # Diccionario global con todos los contextos de clubes cargados


def load_club_into_registry(club_id: str, meta: dict):
    """
    Carga un club individual en memoria. Se ejecuta al arranque por cada club en registry.json
    
    Pasos:
    1. Localiza club.json y state.json del club
    2. Carga el modelo Club desde club.json
    3. Inicializa StateStore para state.json
    4. Sincroniza members_cycle: agrega entradas para nuevos miembros si no existen
    5. Crea contexto (Ctx) con toda la info y lo guarda en _CTX global
    
    Args:
        club_id: Identificador del club (ej: "club_1")
        meta: Metadatos del registry (contiene lista de admins)
    """
    club_dir = CFG.clubs_dir / club_id
    club_file = club_dir / "club.json"
    state_file = club_dir / "state.json"
    if not club_file.exists():
        raise FileNotFoundError(f"[{club_id}] Falta {club_file}. Corre el semillador.")

    c = Club()
    c.load_from_json(str(club_file))
    st = StateStore(state_file)

    # Sincronizar members_cycle: asegurar que cada miembro tenga una entrada
    s = st.load()
    mc = s.get("members_cycle", {})
    changed = False
    for m in c.members:
        if m.waid not in mc:
            mc[m.waid] = []  # Inicializa historial vacío
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
    """
    Carga todos los clubes definidos en registry.json al arranque de la app.
    Limpia _CTX antes de cargar para evitar estados obsoletos si se recarga.
    """
    reg = load_registry()
    _CTX.clear()
    for cid, meta in reg.get("clubs", {}).items():
        load_club_into_registry(cid, meta)


load_all_clubs()  # Se ejecuta al importar el módulo (cuando Flask arranca)


def admin_clubs(waid: str) -> List[str]:
    """
    Devuelve los club_ids donde el waid es administrador.
    Útil para determinar qué menús mostrar y qué operaciones permitir.
    """
    return [cid for cid, ctx in _CTX.items() if waid in ctx.admins]


def member_club(waid: str) -> Optional[str]:
    """
    Devuelve el club_id al que pertenece el waid como miembro.
    
    Nota: Si un miembro está en múltiples clubes (caso inusual), devuelve el primero.
    Si no está en ninguno, devuelve None.
    """
    for cid, ctx in _CTX.items():
        if waid in ctx.members_index:
            return cid
    return None


# ======================================================================================
# 3) Utilidades (normalización, números MX y negocio)
# ======================================================================================
# Funciones auxiliares para:
# - Normalizar texto (quitar acentos, minúsculas)
# - Convertir números MX entre formato interno (E.164: 521XXXXXXXXXX) y público (10 dígitos)
# - Enviar mensajes vía Gupshup
# - Seleccionar candidatos para roles considerando nivel y ciclo

def norm(s: str) -> str:
    """
    Normaliza texto: remueve acentos, convierte a minúsculas, elimina espacios extra.
    Útil para comparar comandos de usuario sin importar cómo los escriban.
    """
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s.strip().lower()


def mx_public_from_internal(waid: str) -> str:
    """
    Convierte número de formato interno (521XXXXXXXXXX) a formato público (10 dígitos).
    
    Ejemplo: "5215551234567" -> "5551234567"
    
    Se usa para mostrar números de forma legible a los usuarios mexicanos.
    """
    digits = "".join(ch for ch in waid if ch.isdigit())
    if digits.startswith("521") and len(digits) >= 13:
        return digits[-10:]
    return digits


def mx_internal_from_any(s: str) -> str:
    """
    Convierte cualquier formato de número MX a formato interno E.164 (521XXXXXXXXXX).
    
    Casos soportados:
    - 10 dígitos (5551234567) -> 5215551234567
    - Ya en formato E.164 (5215551234567) -> sin cambios
    - Otros formatos -> devuelve los dígitos tal cual
    
    Se usa al agregar miembros o buscar por número.
    """
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) == 10:
        return "521" + digits
    if digits.startswith("521") and len(digits) >= 13:
        return digits
    return digits


def pending_candidates(st: dict, exclude_role: Optional[str] = None) -> Set[str]:
    """
    Devuelve el conjunto de waids que ya tienen roles propuestos (pendientes).
    
    Se usa al iniciar ronda para evitar proponer múltiples roles al mismo miembro.
    
    Args:
        st: Estado de la ronda
        exclude_role: Si se indica, no considera candidatos de ese rol específico
    
    Returns:
        Set de waids con roles pendientes
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
    """
    Envía un mensaje de texto vía Gupshup API.
    
    Args:
        to_e164_no_plus: Número destino en formato E.164 SIN '+' (ej: 5215551234567)
        text: Contenido del mensaje
    
    Returns:
        dict con respuesta de Gupshup o {"ok": False, ...} si hubo error
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
    """
    Envía un mensaje a múltiples destinatarios.
    
    Returns:
        dict con conteo de envíos exitosos y fallidos: {"ok": N, "fail": M}
    """
    ok = fail = 0
    for n in numbers:
        res = send_text(n, text)
        if res.get("ok") is False or res.get("status_code", 200) >= 400:
            fail += 1
        else:
            ok += 1
    return {"ok": ok, "fail": fail}


def pretty_name(ctx: Ctx, waid: str) -> str:
    """
    Devuelve el nombre legible de un miembro dado su waid.
    Si no se encuentra, devuelve el waid mismo.
    """
    m = next((m for m in ctx.club.members if m.waid == waid), None)
    return m.name if m else waid


def role_min_level(ctx: Ctx, role_name: str) -> int:
    """
    Devuelve el nivel mínimo requerido para un rol (su dificultad).
    Si el rol no existe o no tiene dificultad definida, devuelve 1.
    """
    r = next((r for r in ctx.club.roles if r.name == role_name), None)
    return max(1, int(getattr(r, "difficulty", 1) or 1)) if r else 1


def choose_candidate_hier(ctx: Ctx, role: str, excluded: Set[str]) -> Optional[str]:
    """
    Selecciona un candidato para un rol usando estrategia jerárquica.
    
    Lógica de selección (en orden de prioridad):
    1. Miembros con nivel >= dificultad del rol Y que NO han hecho ese rol
    2. Miembros con nivel >= dificultad Y que SÍ lo han hecho (permiten repetir)
    3. Miembros con nivel inferior (fallback descendente), primero sin repetir, luego con repetir
    
    Args:
        ctx: Contexto del club
        role: Nombre del rol a asignar
        excluded: Conjunto de waids que NO deben considerarse (ya tienen roles, etc.)
    
    Returns:
        waid del candidato seleccionado, o None si no hay nadie disponible
    """
    st = ctx.state_store.load()
    min_lvl = role_min_level(ctx, role)

    def lvl(m: Member) -> int:
        return int(getattr(m, "level", 1) or 1)

    def pool(filter_fn, allow_repeat: bool) -> List[str]:
        """
        Construye un pool de candidatos aplicando un filtro y respetando ciclo de roles.
        
        Args:
            filter_fn: Función que filtra miembros (ej: lambda m: m.level >= 2)
            allow_repeat: Si True, incluye miembros que ya hicieron el rol
        """
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

    # Intento 1: Nivel suficiente, sin repetir
    p = pool(lambda m: lvl(m) >= min_lvl, allow_repeat=False)
    if p:
        return random.choice(p)

    # Intento 2: Nivel suficiente, permitir repetir
    p = pool(lambda m: lvl(m) >= min_lvl, allow_repeat=True)
    if p:
        return random.choice(p)

    # Intento 3 (fallback): Nivel insuficiente, descendente desde min_lvl-1 hasta 1
    for L in range(min_lvl - 1, 0, -1):
        p = pool(lambda m, L=L: lvl(m) == L, allow_repeat=False)
        if p:
            return random.choice(p)
        p = pool(lambda m, L=L: lvl(m) == L, allow_repeat=True)
        if p:
            return random.choice(p)

    return None  # No hay candidatos disponibles


# --- Admin helpers --------------------------------------------------------------------

def admin_list_members(ctx: Ctx) -> str:
    """
    Genera un texto con la lista de miembros del club y sus administradores.
    
    Formato:
    Miembros de club_1
    - Paco — 5551234567 · nivel 2
    - Roger — 5559876543 · nivel 1
    
    Administradores: Paco (5551234567)
    """
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
    """
    Busca un miembro por waid (10 dígitos o E.164) o por nombre exacto (case-insensitive).
    
    Útil para el comando "ELIMINAR" que puede recibir número o nombre.
    """
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
    """
    Agrega un nuevo miembro al club.
    
    Pasos críticos:
    1. Valida formato y duplicados
    2. Crea instancia Member
    3. Agrega a ctx.club.members
    4. Inicializa su ciclo en state.json
    5. Persiste club.json
    6. **ACTUALIZA ÍNDICES EN MEMORIA** (ctx.members_index y ctx.all_numbers)
    
    El paso 6 es esencial: sin actualizar los índices, el bot no reconocería al nuevo
    miembro hasta reiniciar el servidor.
    """
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


def admin_remove_member(ctx: Ctx, waid_or_name: str) -> str:
    """
    Elimina un miembro del club.
    
    Validaciones:
    - No se puede eliminar si tiene roles pendientes o aceptados en ronda actual
    
    Al igual que admin_add_member, actualiza los índices en memoria después de persistir.
    """
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
# Gestión completa del ciclo de vida de una ronda:
# - start_new_round: Inicia ronda, asigna candidatos, envía invitaciones
# - handle_accept: Procesa aceptación de rol
# - handle_reject: Procesa rechazo y busca nuevo candidato
# - check_and_announce_if_complete: Verifica si todos aceptaron y anuncia resumen
# - cancel_round: Cancela ronda actual
# - reset_all: Reinicia estado completo del club

def start_new_round(ctx: Ctx, by_admin: str) -> str:
    """
    Inicia una nueva ronda de asignación de roles.
    
    Flujo:
    1. Valida que no haya roles pendientes de ronda anterior
    2. Incrementa contador de ronda
    3. Ordena roles por dificultad (más difíciles primero para mejor distribución)
    4. Por cada rol, selecciona candidato excluyendo ya asignados
    5. Envía invitaciones con menú 1/2/3
    6. Notifica a admins si algún rol quedó sin candidato
    
    Args:
        ctx: Contexto del club
        by_admin: Nombre del administrador que inicia la ronda
    
    Returns:
        Mensaje de confirmación o error
    """
    st = ctx.state_store.load()
    if any(not v.get("accepted") for v in st["pending"].values()):
        return "Ya hay invitaciones pendientes. Primero cierra o cancela esa ronda."

    st["round"] += 1
    st["pending"] = {}
    st["accepted"] = {}
    st["last_summary"] = None
    st["canceled"] = False

    # Ordenar roles por dificultad descendente (asignar difíciles primero)
    roles_sorted = sorted(ctx.club.roles, key=lambda r: int(getattr(r, "difficulty", 1) or 1), reverse=True)

    for r in roles_sorted:
        role = r.name
        # Excluir miembros que ya tienen roles asignados o pendientes en esta ronda
        excluded = set(a["waid"] for a in st["accepted"].values())
        excluded.update(pending_candidates(st))
        cand = choose_candidate_hier(ctx, role, excluded)
        if not cand:
            continue  # No hay candidatos para este rol
        st["pending"][role] = {"candidate": cand, "declined_by": [], "accepted": False}

    ctx.state_store.save(st)

    # Enviar invitaciones con menú numérico
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

    # Notificar roles sin asignar
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
    """
    Procesa la aceptación de un rol por parte de un miembro.
    
    Flujo:
    1. Busca el rol pendiente para el waid
    2. Marca como aceptado en state.json
    3. Actualiza members_cycle (historial de roles del miembro)
    4. Si completa ciclo (hizo todos los roles), resetea su ciclo
    5. Actualiza nivel y roles_done del miembro en club.json
    6. Verifica si todos los roles fueron aceptados para anunciar resumen final
    
    Returns:
        Mensaje de confirmación
    """
    st = ctx.state_store.load()
    for role, info in st["pending"].items():
        if info["candidate"] == waid and not info["accepted"]:
            info["accepted"] = True
            st["accepted"][role] = {"waid": waid, "name": pretty_name(ctx, waid)}
            
            # Actualizar ciclo del miembro
            done_list = list(st["members_cycle"].get(waid, []))
            if role not in done_list:
                done_list.append(role)
            # Si completó todos los roles, resetear ciclo
            if len(done_list) >= len(ctx.club.roles):
                done_list = []
            st["members_cycle"][waid] = done_list
            
            # Persistir progreso del miembro en club.json
            member = next((m for m in ctx.club.members if m.waid == waid), None)
            role_obj = next((r for r in ctx.club.roles if r.name == role), None)
            if member and role_obj:
                member.add_role(role_obj)  # Actualiza nivel y roles_done
                ctx.club.save_to_json(str(ctx.club_file))
            
            ctx.state_store.save(st)
            check_and_announce_if_complete(ctx)
            return f"Aceptado: {role} por {pretty_name(ctx, waid)}."
    return "No hay nada pendiente para aceptar."


def handle_reject(ctx: Ctx, waid: str) -> str:
    """
    Procesa el rechazo de un rol y busca un nuevo candidato.
    
    Flujo:
    1. Registra el rechazo en declined_by
    2. Busca nuevo candidato excluyendo todos los que rechazaron
    3. Si hay nuevo candidato, envía invitación
    4. Si no hay más candidatos, elimina el rol de pending y notifica a admins
    
    Returns:
        Mensaje informativo sobre el resultado
    """
    st = ctx.state_store.load()
    for role, info in list(st["pending"].items()):
        if info.get("candidate") == waid and not info.get("accepted"):
            info["declined_by"].append(waid)

            # Buscar nuevo candidato excluyendo rechazos y asignados
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
                # No hay más candidatos disponibles
                del st["pending"][role]
                ctx.state_store.save(st)
                broadcast_text(ctx.admins, f"[{ctx.club_id}] No hay más opciones para el rol: {role}.")
                return "Sin candidatos."
    return "No hay nada pendiente para rechazar."


def make_summary(ctx: Ctx, st: dict) -> str:
    """
    Genera un resumen legible de los roles asignados en la ronda.
    
    Formato:
    Reunión #3 — Resumen de roles
    - Evaluador del tiempo: Paco
    - Evaluador de muletillas: por confirmar
    - Evaluador gramatical: Roger
    """
    lines = [f"Reunión #{st['round']} — Resumen de roles"]
    for role in [r.name for r in ctx.club.roles]:
        if role in st["accepted"]:
            w = st["accepted"][role]["waid"]
            lines.append(f"- {role}: {pretty_name(ctx, w)}")
        else:
            lines.append(f"- {role}: por confirmar")
    return "\n".join(lines)


def check_and_announce_if_complete(ctx: Ctx) -> None:
    """
    Verifica si todos los roles fueron aceptados y, de ser así, anuncia el resumen final.
    
    Se llama después de cada aceptación. Solo anuncia una vez (usa last_summary para evitar duplicados).
    """
    st = ctx.state_store.load()
    all_ok = all(role in st["accepted"] for role in [r.name for r in ctx.club.roles])
    if not all_ok or st.get("canceled"):
        return
    summary = make_summary(ctx, st)
    if st.get("last_summary") == summary:
        return  # Ya se anunció este resumen
    st["last_summary"] = summary
    ctx.state_store.save(st)
    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] {summary}")


def who_am_i(ctx: Ctx, waid: str) -> str:
    """
    Devuelve el estado actual del miembro: rol pendiente, confirmado, o ninguno.
    """
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
    """
    Genera un reporte detallado del estado de la ronda para administradores.
    
    Incluye:
    - Resumen de roles asignados
    - Lista de roles pendientes con candidato actual y número de rechazos
    - Estado de cancelación si aplica
    """
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
    """
    Cancela la ronda actual, limpiando todos los roles pendientes y aceptados.
    """
    st = ctx.state_store.load()
    st["pending"] = {}
    st["accepted"] = {}
    st["last_summary"] = None
    st["canceled"] = True
    ctx.state_store.save(st)
    broadcast_text(ctx.all_numbers, f"[{ctx.club_id}] La ronda se canceló.")
    return f"La ronda #{st['round']} fue cancelada."


def reset_all(ctx: Ctx, by_admin: str) -> str:
    """
    Reinicia completamente el estado del club.
    
    ⚠️ PELIGROSO: Borra todo el historial de rondas y ciclos de miembros.
    """
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
# Sistema de navegación por menús numéricos con gestión de sesiones en memoria.
#
# Arquitectura:
# - SESSION: Dict global que almacena estado de navegación por waid
# - SLOCK: Lock para acceso thread-safe a SESSION
# - mode: Estado actual del menú (root/member/admin/admin_pick/etc)
# - awaiting: Mecanismo para recolectar datos multi-paso
#
# Flujos principales:
# 1. Usuario envía mensaje → resolve_club_context() identifica club
# 2. Si tiene invitación pendiente, procesa 1/2/3 (prioridad máxima en webhook)
# 3. Si no, interpreta mensaje según session.mode
# 4. Menús renderizados con funciones render_*_menu()

# Memoria en proceso para navegación por menús
SESSION: Dict[str, dict] = {}
SLOCK = Lock()

def get_session(waid: str) -> dict:
    """
    Obtiene o crea la sesión de navegación del usuario.
    
    Las sesiones son transitorias (en memoria). Si el servidor reinicia,
    los usuarios regresan al menú principal.
    
    Estructura de sesión:
    {
        "mode": "root" | "member" | "admin" | "admin_pick" | "admin_add",
        "club": club_id seleccionado (para admins multi-club),
        "awaiting": tipo de dato esperado (nombre/apellidos/waid/palabra/etc),
        "buffer": diccionario temporal para datos multi-paso
    }
    """
    with SLOCK:
        s = SESSION.get(waid)
        if not s:
            s = {"mode": "root", "club": None, "awaiting": None, "buffer": None}
            SESSION[waid] = s
        return s

def set_session(waid: str, **kwargs) -> None:
    """
    Actualiza campos de la sesión del usuario de manera thread-safe.
    
    Ejemplo:
        set_session(waid, mode="admin", club="club_1")
    """
    with SLOCK:
        s = SESSION.setdefault(waid, {"mode": "root", "club": None, "awaiting": None, "buffer": None})
        s.update(kwargs)

# ----- Renderizado de menús -----
# Todos los menús usan formato numérico (1/2/3...) para facilitar
# la entrada desde dispositivos móviles

def render_root_menu(waid: str) -> str:
    """
    Genera el menú principal dinámicamente según los roles del usuario.
    
    Opciones mostradas:
    - "Menú de miembro" si waid pertenece a algún club
    - "Menú de admin" si waid es admin en uno o más clubs
      * Si es admin de 1 solo club, va directo
      * Si es admin de múltiples, muestra selector de club
    - "Mi estado de rol" siempre disponible
    
    Returns:
        String con opciones numeradas dinámicamente
    """
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
    """
    Menú de operaciones disponibles para miembros regulares.
    
    Opciones:
    1) Ver mi rol pendiente o confirmado
    2) Ver estado general de la ronda
    9) Volver al menú principal
    """
    return (
        f"[{ctx.club_id}] Menú miembro\n"
        "Elige una opción y envía solo el número:\n"
        "1) Mi rol (pendiente/confirmado)\n"
        "2) Estado de la ronda\n"
        "9) Volver"
    )

def render_admin_club_picker(aclubs: List[str]) -> str:
    """
    Selector de club para administradores que gestionan múltiples clubs.
    
    Se muestra cuando admin tiene permisos en más de un club.
    """
    lines = ["Elige club para administrar (envía solo el número):"]
    for i, cid in enumerate(aclubs, 1):
        lines.append(f"{i}) {cid}")
    lines.append("9) Volver")
    return "\n".join(lines)

def render_admin_menu(ctx: Ctx) -> str:
    """
    Menú completo de operaciones administrativas.
    
    Operaciones disponibles:
    1) Iniciar ronda: start_new_round()
    2) Ver estado: status_text() con detalles de roles pendientes
    3) Cancelar ronda: cancel_round()
    4) Resetear estado: reset_all() (⚠️ operación peligrosa)
    5) Agregar miembro: inicia flujo multi-paso (nombre→apellidos→waid)
    6) Eliminar miembro: cambia a modo admin_pick para seleccionar
    7) Ver miembros: lista completa con waids y niveles
    9) Volver al menú principal
    """
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
# Servidor Flask que procesa mensajes de WhatsApp vía Gupshup.
#
# Endpoints:
# - GET /: Health check con información de clubs
# - GET /webhook: Verificación de webhook por Gupshup
# - POST /webhook: Procesa mensajes entrantes
#
# Flujo de procesamiento en webhook_post():
# 1. Extrae waid y body del mensaje
# 2. PRIORIDAD MÁXIMA: Si tiene invitación pendiente, procesa 1/2/3
# 3. Si está en flujo awaiting (multi-paso), maneja recolección de datos
# 4. Si es número (1/2/3/etc), interpreta como navegación de menú según session.mode
# 5. Si es texto libre, procesa comandos legacy o muestra menú
#
# Resolución de club:
# - Usa session.club si está establecido
# - Busca en miembros (member_club)
# - Busca en admins (admin_clubs)
# - Busca en pending/accepted de state.json
# - Permite especificar [club_id] al final del mensaje

app = Flask(__name__)


@app.route("/", methods=["GET"])
def health():
    """
    Endpoint de salud que reporta estado de todos los clubs.
    
    Returns:
        JSON con número de miembros y roles por club
    """
    info = {}
    for cid, ctx in _CTX.items():
        info[cid] = {"members": len(ctx.members_index), "roles": [r.name for r in ctx.club.roles]}
    return {"ok": True, "app": CFG.app_name, "clubs": info}


@app.route("/webhook", methods=["GET"])
def webhook_get():
    """
    Verificación de webhook requerida por Gupshup durante configuración inicial.
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == CFG.verify_token:
        return challenge or "OK", 200
    return "ok", 200


def extract_trailing_club_id(text: str) -> Optional[str]:
    """
    Extrae club_id si el usuario lo especifica al final del mensaje.
    
    Formatos soportados:
    - "mensaje [club_1]"
    - "mensaje club_1"
    
    Returns:
        club_id si es válido, None si no se encuentra o no existe
    """
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
    """
    Remueve el club_id del final del mensaje para procesar el resto.
    """
    t = text.strip()
    t = re.sub(rf"\s*\[\s*{re.escape(cid)}\s*\]\s*$", "", t, flags=re.IGNORECASE)
    t = re.sub(rf"\s+{re.escape(cid)}\s*$", "", t, flags=re.IGNORECASE)
    return t.strip()


def infer_user_club(waid: str, explicit_cid: Optional[str] = None) -> Optional[str]:
    """
    Determina el club del usuario por múltiples vías.
    
    Prioridad:
    1. Club especificado explícitamente en mensaje
    2. Club donde es miembro (member_club)
    3. Club donde tiene invitación pendiente
    4. Club donde tiene rol aceptado
    
    Returns:
        club_id si se puede determinar unívocamente, None si es ambiguo o no existe
    """
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
    """
    Verifica si el usuario tiene una invitación de rol pendiente en este club.
    
    Returns:
        Nombre del rol si hay invitación pendiente, None si no hay
    """
    st = ctx.state_store.load()
    for role, info in st.get("pending", {}).items():
        if info.get("candidate") == waid and not info.get("accepted"):
            return role
    return None


@app.route("/webhook", methods=["POST"])
def webhook_post():
    """
    Procesa mensajes entrantes de WhatsApp.
    
    Flujo de prioridades:
    1. MÁXIMA: Invitación pendiente (1/2/3 para aceptar/rechazar/posponer)
    2. ALTA: Flujo awaiting (recolección multi-paso de datos)
    3. MEDIA: Navegación por menús numéricos (1/2/3/etc según mode)
    4. BAJA: Comandos de texto libre (legacy)
    5. FALLBACK: Muestra menú principal
    
    Resolución de club:
    - Usa session.club si está establecido
    - Infiere de miembros/admins/pending/accepted
    - Permite especificar [club_id] explícitamente
    
    Respuestas:
    - Todos los mensajes responden con send_text() inmediatamente
    - Después de operaciones, muestra menú correspondiente
    - Mantiene contexto de sesión para navegación multi-paso
    """
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

            # ---------------------- 0) Identificación inicial ----------------------
            # Detectar si el mensaje es un número (para navegación de menú)
            is_number = re.fullmatch(r"\d{1,3}", body) is not None

            # Resolver club por defecto si no hay uno en la sesión
            if not s.get("club"):
                mc = member_club(waid)
                acls = admin_clubs(waid)
                if mc:
                    set_session(waid, club=mc)
                elif len(acls) == 1:
                    set_session(waid, club=acls[0])

            # Determinar club actual (puede incluir [club_id] explícito)
            current_cid = s.get("club") or infer_user_club(waid, extract_trailing_club_id(body_raw))
            if current_cid and current_cid in _CTX:
                ctx = _CTX[current_cid]
                role_pending = has_pending_invite(ctx, waid)
            else:
                ctx = None
                role_pending = None

            # ---------------------- PRIORIDAD 1: Invitación pendiente ----------------
            # Si tiene rol pendiente y envía 1/2/3, procesar inmediatamente
            if is_number and role_pending and body in ("1", "2", "3"):
                if body == "1":
                    # ⭐ INTERCEPTAR: Si es Evaluador gramatical, iniciar flujo de palabra del día
                    if role_pending == "Evaluador gramatical":
                        st = ctx.state_store.load()
                        set_session(waid, awaiting="word_step1_palabra", 
                                   buffer={"role": role_pending, "waid": waid, "club": ctx.club_id, "round": st["round"]})
                        send_text(waid, 
                            "¡Excelente! Como Evaluador gramatical, compartes la *Palabra del Día*.\n\n"
                            "📖 Envía la palabra:"
                        )
                    else:
                        # Para otros roles, aceptación normal
                        send_text(waid, handle_accept(ctx, waid))
                        set_session(waid, mode="root", awaiting=None, buffer=None)
                        send_text(waid, render_root_menu(waid))
                elif body == "2":
                    send_text(waid, handle_reject(ctx, waid))
                    set_session(waid, mode="root", awaiting=None, buffer=None)
                    send_text(waid, render_root_menu(waid))
                else:
                    # "3" = Responder después: confirmar y mantener pendiente
                    st = ctx.state_store.load()
                    send_text(waid, f"Queda pendiente tu respuesta para {role_pending} en la ronda #{st['round']} ({ctx.club_id}).")
                    set_session(waid, mode="root", awaiting=None, buffer=None)
                    send_text(waid, render_root_menu(waid))
                continue

            # ---------------------- PRIORIDAD 2: Router por estado de sesión --------
            awaiting = s.get("awaiting")
            if is_number:
                # ===== Menú raíz =====
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
                        # "Mi estado de rol": muestra estado sin cambiar mode
                        cid = infer_user_club(waid)
                        if cid and cid in _CTX:
                            send_text(waid, who_am_i(_CTX[cid], waid))
                        else:
                            send_text(waid, "No se pudo determinar tu club. Pide a un admin que te agregue.")
                        send_text(waid, render_root_menu(waid)); continue

                # ===== Picker de club para admin multi-club =====
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

                # ===== Menú de miembro =====
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

                # ===== Menú de admin =====
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
                        # Iniciar flujo de agregar miembro
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

            # ---------------------- PRIORIDAD 3: Flujos awaiting (texto libre) ------
            # Estos flujos esperan texto libre del usuario (no números de menú)
            
            # Flujo: Agregar miembro (esperando "Nombre, 55XXXXXXXX")
            if awaiting == "admin_add_member" and s.get("mode") == "admin" and ctx:
                tail = body_raw.strip()
                if "," in tail:
                    name, num = tail.split(",", 1)
                else:
                    # Formato alternativo: "Nombre 55XXXXXXXX"
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

            # Flujo: Eliminar miembro (esperando waid o nombre)
            if awaiting == "admin_remove_member" and s.get("mode") == "admin" and ctx:
                tail = body_raw.strip()
                out = admin_remove_member(ctx, tail)
                send_text(waid, out)
                set_session(waid, awaiting=None, buffer=None)
                send_text(waid, render_admin_menu(ctx))
                continue

            # ==================== FLUJO: Palabra del Día (Evaluador gramatical) ====================
            
            # Paso 1: Recibir palabra
            if awaiting == "word_step1_palabra":
                buffer = s.get("buffer", {})
                buffer["palabra"] = body_raw.strip()
                set_session(waid, awaiting="word_step2_significado", buffer=buffer)
                send_text(waid, "✍️ Ahora envía el significado de la palabra:")
                continue

            # Paso 2: Recibir significado
            if awaiting == "word_step2_significado":
                buffer = s.get("buffer", {})
                buffer["significado"] = body_raw.strip()
                set_session(waid, awaiting="word_step3_ejemplo", buffer=buffer)
                send_text(waid, "💡 Finalmente, envía un ejemplo de uso de la palabra:")
                continue

            # Paso 3: Recibir ejemplo y mostrar resumen para confirmación
            if awaiting == "word_step3_ejemplo":
                buffer = s.get("buffer", {})
                buffer["ejemplo"] = body_raw.strip()
                set_session(waid, awaiting="word_confirm", buffer=buffer)
                
                # Mostrar resumen con opciones de confirmación
                resumen = (
                    f"📋 *Resumen de Palabra del Día*\n\n"
                    f"📖 *Palabra:* {buffer['palabra']}\n\n"
                    f"✍️ *Significado:* {buffer['significado']}\n\n"
                    f"💡 *Ejemplo:* {buffer['ejemplo']}\n\n"
                    f"¿Es correcta esta información?\n"
                    f"1) ✅ Sí, confirmar y aceptar rol\n"
                    f"2) ✏️ Editar palabra\n"
                    f"3) ✏️ Editar significado\n"
                    f"4) ✏️ Editar ejemplo\n"
                    f"5) ❌ Cancelar"
                )
                send_text(waid, resumen)
                continue

            # Confirmación: Usuario decide si confirmar o editar
            if awaiting == "word_confirm" and is_number:
                buffer = s.get("buffer", {})
                
                if body == "1":
                    # ✅ CONFIRMAR: Guardar palabra del día y completar aceptación
                    club_ctx = _CTX[buffer["club"]]
                    st = club_ctx.state_store.load()
                    
                    # Guardar palabra del día en state.json
                    st["word_of_the_day"] = {
                        "palabra": buffer["palabra"],
                        "significado": buffer["significado"],
                        "ejemplo": buffer["ejemplo"],
                        "waid": buffer["waid"],
                        "nombre": pretty_name(club_ctx, buffer["waid"]),
                        "round": buffer["round"]
                    }
                    club_ctx.state_store.save(st)
                    
                    # AHORA SÍ confirmar el rol de Evaluador gramatical
                    result = handle_accept(club_ctx, buffer["waid"])
                    send_text(waid, f"✅ {result}\n📖 Palabra del día guardada: '{buffer['palabra']}'")
                    
                    # Limpiar sesión y volver al menú principal
                    set_session(waid, awaiting=None, buffer=None, mode="root")
                    send_text(waid, render_root_menu(waid))
                    continue
                
                elif body == "2":
                    # ✏️ Editar palabra
                    set_session(waid, awaiting="word_edit_palabra", buffer=buffer)
                    send_text(waid, f"📖 Palabra actual: {buffer['palabra']}\n\nEnvía la nueva palabra:")
                    continue
                
                elif body == "3":
                    # ✏️ Editar significado
                    set_session(waid, awaiting="word_edit_significado", buffer=buffer)
                    send_text(waid, f"✍️ Significado actual: {buffer['significado']}\n\nEnvía el nuevo significado:")
                    continue
                
                elif body == "4":
                    # ✏️ Editar ejemplo
                    set_session(waid, awaiting="word_edit_ejemplo", buffer=buffer)
                    send_text(waid, f"💡 Ejemplo actual: {buffer['ejemplo']}\n\nEnvía el nuevo ejemplo:")
                    continue
                
                elif body == "5":
                    # ❌ CANCELAR: Limpiar todo y volver al menú
                    send_text(waid, "❌ Palabra del día cancelada. La invitación de rol sigue pendiente.")
                    set_session(waid, awaiting=None, buffer=None, mode="root")
                    send_text(waid, render_root_menu(waid))
                    continue
                
                else:
                    # Opción inválida, volver a mostrar menú de confirmación
                    send_text(waid, "Opción inválida. Envía 1, 2, 3, 4 o 5.")
                    continue

            # Edición: Re-capturar palabra
            if awaiting == "word_edit_palabra":
                buffer = s.get("buffer", {})
                buffer["palabra"] = body_raw.strip()
                set_session(waid, awaiting="word_confirm", buffer=buffer)
                
                # Volver a mostrar resumen
                resumen = (
                    f"📋 *Resumen de Palabra del Día*\n\n"
                    f"📖 *Palabra:* {buffer['palabra']}\n\n"
                    f"✍️ *Significado:* {buffer['significado']}\n\n"
                    f"💡 *Ejemplo:* {buffer['ejemplo']}\n\n"
                    f"¿Es correcta esta información?\n"
                    f"1) ✅ Sí, confirmar y aceptar rol\n"
                    f"2) ✏️ Editar palabra\n"
                    f"3) ✏️ Editar significado\n"
                    f"4) ✏️ Editar ejemplo\n"
                    f"5) ❌ Cancelar"
                )
                send_text(waid, resumen)
                continue

            # Edición: Re-capturar significado
            if awaiting == "word_edit_significado":
                buffer = s.get("buffer", {})
                buffer["significado"] = body_raw.strip()
                set_session(waid, awaiting="word_confirm", buffer=buffer)
                
                # Volver a mostrar resumen
                resumen = (
                    f"📋 *Resumen de Palabra del Día*\n\n"
                    f"📖 *Palabra:* {buffer['palabra']}\n\n"
                    f"✍️ *Significado:* {buffer['significado']}\n\n"
                    f"💡 *Ejemplo:* {buffer['ejemplo']}\n\n"
                    f"¿Es correcta esta información?\n"
                    f"1) ✅ Sí, confirmar y aceptar rol\n"
                    f"2) ✏️ Editar palabra\n"
                    f"3) ✏️ Editar significado\n"
                    f"4) ✏️ Editar ejemplo\n"
                    f"5) ❌ Cancelar"
                )
                send_text(waid, resumen)
                continue

            # Edición: Re-capturar ejemplo
            if awaiting == "word_edit_ejemplo":
                buffer = s.get("buffer", {})
                buffer["ejemplo"] = body_raw.strip()
                set_session(waid, awaiting="word_confirm", buffer=buffer)
                
                # Volver a mostrar resumen
                resumen = (
                    f"📋 *Resumen de Palabra del Día*\n\n"
                    f"📖 *Palabra:* {buffer['palabra']}\n\n"
                    f"✍️ *Significado:* {buffer['significado']}\n\n"
                    f"💡 *Ejemplo:* {buffer['ejemplo']}\n\n"
                    f"¿Es correcta esta información?\n"
                    f"1) ✅ Sí, confirmar y aceptar rol\n"
                    f"2) ✏️ Editar palabra\n"
                    f"3) ✏️ Editar significado\n"
                    f"4) ✏️ Editar ejemplo\n"
                    f"5) ❌ Cancelar"
                )
                send_text(waid, resumen)
                continue

            # ==================== FIN FLUJO: Palabra del Día ====================

            # ---------------------- PRIORIDAD 4: Comandos legacy (texto libre) ------
            # Compatibilidad con comandos de texto para usuarios que escriben en lugar de números
            
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

            # ---------------------- FALLBACK: Mostrar menú principal -----------------
            # Si el mensaje no coincide con ningún flujo, mostrar menú raíz
            send_text(waid, render_root_menu(waid))

    except Exception:
        log.exception("Error procesando webhook; payload=%s", data)

    return jsonify({"status": "ok"})


# ======================================================================================
# 7) Main
# ======================================================================================
# Punto de entrada del servidor Flask.
# En producción, usar gunicorn u otro servidor WSGI.

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=CFG.port, debug=False)