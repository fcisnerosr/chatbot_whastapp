"""
--------------------------------------------------------------------------------
Archivo: setup_club.py
Rol dentro del pipeline del bot:
--------------------------------------------------------------------------------
Este script es el PUNTO DE INICIO del catálogo del club. 

Su única responsabilidad es CREAR la "semilla" inicial:
  - Define los ROLES de Toastmasters con dificultad (1 a 6).
  - Define los MIEMBROS iniciales (nombre + número de WhatsApp).
  - Construye un objeto Club (definido en models.py) y le agrega
    esos roles y miembros.
  - Finalmente, GUARDA todo en data/club.json.

⚠️ Importante:
- Este script se corre UNA SOLA VEZ al inicio (o cuando quieres reiniciar
  el catálogo).
- Genera automáticamente el archivo data/club.json.
- Ese archivo se convierte en la "fuente de verdad" de los roles y miembros
  que usará el bot en app.py.
- NO se debe editar club.json a mano: se regenera con setup_club.py
  o se modifica a través del código.

En el pipeline del bot:
setup_club.py → crea club.json → app.py carga ese JSON en cada ejecución.
--------------------------------------------------------------------------------
"""

from models import Club, Member, Role

# Crear el club
club = Club()

# Definir roles con dificultad 1–6
roles = [
    ("Evaluador del tiempo", 1),
    ("Evaluador de vacilaciones", 2),
    ("Evaluador gramatical", 3),
    ("Director de Table topics", 4),
    ("Toastmasters de la noche", 5),
    ("Evaluador general", 6),
]

for name, diff in roles:
    club.add_role(Role(name, diff))

# Definir miembros iniciales
members = [
    ("Daniel", "5219212671618"),
    ("Paco", "5212293655442"),
    ("Marcos", "5212721073312"),
    ("Sheila", "5219211787763"),
    ("Roger", "5215634948177"),
]

for name, waid in members:
    club.add_member(Member(name, waid))

# Guardar en club.json
club.save_to_json("data/club.json")

print("✅ Semilla creada: data/club.json")
