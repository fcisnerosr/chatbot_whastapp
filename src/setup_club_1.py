# src/seed_club.py
# =============================================================================
# Semillador multi-club (intuitivo y robusto)
#
# ¿Qué hace?
#   - Crea/actualiza la carpeta del club: data/clubs/<CLUB_ID>/
#   - Genera/actualiza:
#         club.json  (catálogo de miembros y roles)
#         state.json (estado inicial del bot para ese club)
#   - Actualiza el registro global: data/clubs/registry.json
#       => ahí se guardan los administradores asociados a cada club
#
# ¿Cómo lo uso?
#   1) Edita la sección "CONFIGURACIÓN" de abajo.
#   2) Ejecuta:  python src/nombre del archivo
#      ejm: python src/seed_club.py
#   3) Repite para otro club cambiando CLUB_ID, ADMINS, MEMBERS, ROLES, etc.
#
# Idempotente:
#   - Puedes correrlo varias veces para el mismo club: reescribe club.json y state.json,
#     y actualiza los admins en registry.json.
#   - NO borra estados históricos de otras rondas si ya existe state.json (opción configurable).
#
# Requisitos:
#   - models.py debe existir con las clases Club, Member, Role.
# =============================================================================

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

# Importa tu modelo POO existente
from models import Club, Member, Role

# -----------------------------------------------------------------------------
# CONFIGURACIÓN (EDITA AQUÍ)
# -----------------------------------------------------------------------------
# Identificador único del club (carpeta)
CLUB_ID: str = "club_1"

# Admins del club (formato E.164 SIN '+'). Puedes listar 1 o varios.
ADMINS: List[str] = [
    "5215634948177",  # Roger
    # "5219212671618",  # Otro admin (descomenta si aplica)
]

# Miembros iniciales del club: lista de tuplas (Nombre, NumeroE164_sinMas)
MEMBERS: List[Tuple[str, str]] = [
    ("Daniel", "5219212671618"),
    ("Paco",   "5212293655442"),
    ("Marcos", "5212721073312"),
    ("Sheila", "5219211787763"),
    ("Roger",  "5215634948177"),
]

# Roles del club con dificultad (1=fácil ... 6=difícil)
ROLES: List[Tuple[str, int]] = [
    ("Evaluador del tiempo", 1),
    ("Evaluador de muletillas", 2),
    ("Evaluador gramatical", 3),
    ("Director de Table Topics", 4),
    ("Toastmaster de la sesión", 5),
    ("Evaluador general", 6),
]

# Si el state.json ya existe, ¿lo respetamos (True) o lo regeneramos (False)?
PRESERVAR_STATE_EXISTENTE: bool = True

# Directorio base de todos los clubes
CLUBS_DIR: Path = Path("data/clubs")

# -----------------------------------------------------------------------------
# UTILIDADES (no necesitas tocarlas)
# -----------------------------------------------------------------------------

REGISTRY_FILE: Path = CLUBS_DIR / "registry.json"


@dataclass
class SeedResult:
    club_file: Path
    state_file: Path
    registry_file: Path
    created_club_json: bool
    created_state_json: bool
    updated_registry: bool


def _digits_only(s: str) -> str:
    """Quita cualquier cosa que no sea dígito (para números E.164 sin '+')."""
    return "".join(ch for ch in s if ch.isdigit())


def _validate_config():
    if not CLUB_ID.strip():
        raise ValueError("CLUB_ID no puede estar vacío.")
    if not ADMINS:
        raise ValueError("Debes especificar al menos un admin en ADMINS.")
    for i, a in enumerate(ADMINS):
        ADMINS[i] = _digits_only(a)
        if not ADMINS[i]:
            raise ValueError(f"Admin inválido en posición {i}.")
    if not MEMBERS:
        raise ValueError("Debes especificar al menos un miembro en MEMBERS.")
    for i, (name, waid) in enumerate(MEMBERS):
        if not name.strip():
            raise ValueError(f"Nombre de miembro vacío en posición {i}.")
        MEMBERS[i] = (name.strip(), _digits_only(waid))
        if not MEMBERS[i][1]:
            raise ValueError(f"Número de miembro inválido en posición {i}.")
    if not ROLES:
        raise ValueError("Debes especificar al menos un rol en ROLES.")
    for i, (rname, diff) in enumerate(ROLES):
        if not rname.strip():
            raise ValueError(f"Nombre de rol vacío en posición {i}.")
        if not isinstance(diff, int) or diff < 1 or diff > 6:
            raise ValueError(f"Dificultad de rol inválida en posición {i}: debe ser 1..6.")


def _load_registry() -> dict:
    if REGISTRY_FILE.exists():
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    return {"clubs": {}}  # estructura base


def _save_json_atomic(path: Path, obj: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _make_default_state() -> dict:
    """Estado inicial compatible con tu app (members_cycle por waid)."""
    return {
        "round": 0,
        "pending": {},
        "accepted": {},
        "members_cycle": {waid: [] for _, waid in MEMBERS},
        "last_summary": None,
        "canceled": False,
    }


def seed_club() -> SeedResult:
    _validate_config()

    club_dir = CLUBS_DIR / CLUB_ID
    club_dir.mkdir(parents=True, exist_ok=True)

    club_file = club_dir / "club.json"
    state_file = club_dir / "state.json"

    # Construye el catálogo en memoria
    club = Club()
    for name, diff in ROLES:
        club.add_role(Role(name.strip(), diff))
    for name, waid in MEMBERS:
        club.add_member(Member(name.strip(), waid))

    # Guardar club.json (siempre lo reescribimos para ser fuente de verdad)
    club_payload = {
        "members": [m.to_dict() for m in club.members],
        "roles": [r.to_dict() for r in club.roles],
    }
    prev_club_exists = club_file.exists()
    _save_json_atomic(club_file, club_payload)

    # Guardar state.json (según política)
    created_state = False
    if state_file.exists() and PRESERVAR_STATE_EXISTENTE:
        # Mantener el estado actual, pero aseguramos que todos los miembros existan en members_cycle
        existing = json.loads(state_file.read_text(encoding="utf-8"))
        mc = existing.get("members_cycle", {})
        changed = False
        for _name, waid in MEMBERS:
            if waid not in mc:
                mc[waid] = []
                changed = True
        if changed:
            existing["members_cycle"] = mc
            _save_json_atomic(state_file, existing)
    else:
        # Regenerar desde cero
        _save_json_atomic(state_file, _make_default_state())
        created_state = True

    # Actualizar registry.json
    registry = _load_registry()
    registry["clubs"].setdefault(CLUB_ID, {})
    # Sobrescribimos admins del club con los que están en esta semilla (más claro que hacer merge)
    registry["clubs"][CLUB_ID]["admins"] = sorted(set(ADMINS))
    _save_json_atomic(REGISTRY_FILE, registry)

    return SeedResult(
        club_file=club_file,
        state_file=state_file,
        registry_file=REGISTRY_FILE,
        created_club_json=(not prev_club_exists),
        created_state_json=created_state,
        updated_registry=True,
    )


def _pretty_ok(msg: str):
    print(f"✅ {msg}")


def _pretty_info(msg: str):
    print(f"ℹ️  {msg}")


def _pretty_warn(msg: str):
    print(f"⚠️  {msg}")


if __name__ == "__main__":
    res = seed_club()
    _pretty_ok(f"Club: {CLUB_ID}")
    _pretty_ok(f"Archivo catálogo: {res.club_file}")
    if res.created_club_json:
        _pretty_info("club.json creado.")
    else:
        _pretty_info("club.json actualizado.")

    _pretty_ok(f"Archivo estado: {res.state_file}")
    if res.created_state_json:
        _pretty_info("state.json creado.")
    else:
        if PRESERVAR_STATE_EXISTENTE:
            _pretty_info("state.json preservado y sincronizado con nuevos miembros (si faltaban).")
        else:
            _pretty_info("state.json regenerado por configuración.")

    _pretty_ok(f"Registro global: {res.registry_file}")
    _pretty_info(f"Admins para {CLUB_ID}: {', '.join(ADMINS)}")

    # Consejos de uso siguientes
    print("\nSiguiente paso:")
    print("  - Arranca tu app multi-club (lee data/clubs/registry.json).")
    print("  - Agrega más clubes repitiendo este script con CLUB_ID y datos distintos.")