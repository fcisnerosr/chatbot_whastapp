"""
--------------------------------------------------------------------------------
Archivo: models.py
Rol dentro del pipeline del bot:
--------------------------------------------------------------------------------
Este archivo define el MODELO ORIENTADO A OBJETOS (POO) del club. 

Contiene tres clases principales:
  - Role: representa un rol (nombre + dificultad).
  - Member: representa a un socio/invitado (nombre, WhatsApp, nivel,
            roles completados).
  - Club: el contenedor/orquestador que maneja listas de roles y miembros,
          asignaciones de roles y persistencia en JSON.

Responsabilidades:
- Define la lógica de negocio (cómo se sube de nivel, cómo se guarda/carga).
- Permite serializar/guardar el estado del club a un JSON (save_to_json).
- Permite cargar ese estado de vuelta desde JSON (load_from_json).
- Es usado tanto por setup_club.py (para generar club.json) como por app.py
  (para leer, asignar y actualizar miembros y roles).

En el pipeline del bot:
models.py → provee las clases Role, Member y Club
setup_club.py → usa Club para crear y guardar club.json
app.py → usa Club para leer/actualizar club.json en tiempo de ejecución
--------------------------------------------------------------------------------
"""

import json
from pathlib import Path

class Role:
    def __init__(self, name: str, difficulty: int):
        """
        Representa un rol de Toastmasters.
        :param name: nombre del rol
        :param difficulty: dificultad (1=fácil, 6=difícil)
        """
        self.name = name
        self.difficulty = difficulty

    def to_dict(self) -> dict:
        return {"name": self.name, "difficulty": self.difficulty}

    @classmethod
    def from_dict(cls, data: dict):
        return cls(data["name"], data["difficulty"])


class Member:
    def __init__(self, name: str, waid: str, is_guest: bool = False, level: int = 1):
        """
        Representa a un socio o invitado.
        :param name: nombre del miembro
        :param waid: número de WhatsApp en formato E.164 (sin '+')
        :param is_guest: True si es invitado, False si es socio
        :param level: nivel de experiencia (1–6)
        """
        self.name = name
        self.waid = waid
        self.is_guest = is_guest
        self.level = level
        self.roles_done = []

    def add_role(self, role: Role):
        """Asigna un rol y sube de nivel."""
        self.roles_done.append(role.name)
        self.increase_level()

    def increase_level(self):
        if self.level < 6:
            self.level += 1

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "waid": self.waid,
            "is_guest": self.is_guest,
            "level": self.level,
            "roles_done": self.roles_done,
        }

    @classmethod
    def from_dict(cls, data: dict):
        m = cls(
            data["name"],
            data["waid"],
            data.get("is_guest", False),
            data.get("level", 1),
        )
        m.roles_done = data.get("roles_done", [])
        return m


class Club:
    def __init__(self):
        """Contenedor de miembros y roles, maneja la lógica del club."""
        self.members: list[Member] = []
        self.roles: list[Role] = []

    def add_member(self, member: Member):
        self.members.append(member)

    def add_role(self, role: Role):
        self.roles.append(role)

    def assign_role(self, member_name: str, role_name: str):
        """Busca miembro + rol, los vincula y sube nivel del miembro."""
        member = next((m for m in self.members if m.name == member_name), None)
        role = next((r for r in self.roles if r.name == role_name), None)
        if not member or not role:
            raise ValueError("Miembro o rol no encontrado")
        member.add_role(role)

    def save_to_json(self, filepath="data/club.json"):
        """Guarda todo el club en JSON."""
        data = {
            "members": [m.to_dict() for m in self.members],
            "roles": [r.to_dict() for r in self.roles],
        }
        Path(filepath).write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def load_from_json(self, filepath="data/club.json"):
        """Carga el club desde JSON existente."""
        data = json.loads(Path(filepath).read_text(encoding="utf-8"))
        self.members = [Member.from_dict(m) for m in data.get("members", [])]
        self.roles = [Role.from_dict(r) for r in data.get("roles", [])]
