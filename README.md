# Chatbot WhatsApp — Roles/Toastmasters (PROTOTIPO)

Version: 1.1  
Author: Data & Flow Consulting <fcisnerosr@outlook.es>  
License: Proprietary

---

## ⚠️ NOTAS IMPORTANTES (LEER ANTES DE USAR)

* Este repositorio es un **PROTOTIPO** en evolución. Úsalo para pruebas; no es aún un sistema “production-ready”.
* **NUNCA** subas el archivo `.env` al repositorio ni compartas credenciales. Mantén `.env` fuera de Git (está en `.gitignore`). Provee `.env.example` como guía.
* Quien ejecute el bot debe agregarse como `ADMIN` en el `.env` para poder usar comandos administrativos (`INICIAR`/`ESTADO`/`RESET`/`CANCELAR`).
* En modo Sandbox de Gupshup, cada usuario debe escribir “hola” al número sandbox al menos una vez cada 24h para poder recibir mensajes del bot.

---

## Descripción

Bot en **Python + Flask** que administra rondas y asigna roles de una reunión (Toastmasters). Envía propuestas de rol por WhatsApp usando **Gupshup API**.  
Para pruebas locales expone el servidor con **ngrok**.

**Nueva implementación (v1.1):**  
- Se introduce **POO** con `src/models.py`:
  - `Role` (rol + dificultad 1–6)
  - `Member` (socio/invitado + nivel + historial de roles)
  - `Club` (orquestador, persistencia JSON)
- Se agrega `src/setup_club.py` para **sembrar** el estado inicial en `data/club.json`.
- **Reemplaza** el antiguo `data/members.json` como catálogo principal.  
  > A partir de ahora, **el archivo de verdad** que usa la app es `data/club.json`.

---

## Estructura principal

```text
chatbot_whastapp/
├─ src/
│  ├─ app.py          # servidor Flask (endpoints / y /webhook)
│  ├─ models.py       # POO: Role, Member, Club + persistencia JSON
│  └─ setup_club.py   # script de semilla inicial (genera data/club.json)
├─ data/
│  ├─ club.json       # (nuevo) estado del club: roles + miembros + niveles
│  └─ state.json      # estado de ronda del bot (runtime), si aplica
├─ scripts/           # utilidades
├─ notebooks/         # exploración
├─ tests/             # pruebas
├─ environment.yml    # entorno conda/mamba
├─ pyproject.toml
└─ README.md
```

**¿Qué hace cada archivo clave?**

- **`src/models.py`**  
  Define el **modelo POO** y la **persistencia**:
  - `Role`: nombre + dificultad (1–6)
  - `Member`: nombre, waid, socio/invitado, nivel (1–6), roles_done
  - `Club`: mantiene listas de `members` y `roles`, asigna roles (`assign_role`) y guarda/carga JSON (`save_to_json`/`load_from_json`).

- **`src/setup_club.py`**  
  **Semilla inicial** (Opción A): crea un `Club`, agrega los 6 roles con dificultad creciente y los miembros iniciales, y guarda todo en `data/club.json`.

- **`data/club.json`**  
  **Fuente de verdad** del catálogo (reemplaza al antiguo `members.json`). Contiene:
  {
    "members": [
      {"name": "Paco", "waid": "521...", "is_guest": false, "level": 1, "roles_done": []}
    ],
    "roles": [
      {"name": "Evaluador del tiempo", "difficulty": 1}
    ]
  }

- **`data/state.json`**  
  Estado operativo de las rondas del bot (si tu `app.py` lo usa). No se reemplaza; coexiste.

---

## Requisitos

* Python 3.12+
* Mamba/Conda (o pip)
* Cuenta de Gupshup (App WhatsApp con modo Sandbox)
* Cuenta de ngrok (gratuita) para exponer `http://localhost:5000`

---

## Instalación del entorno

1) **Crear y activar entorno con mamba/conda**
mamba env create -f environment.yml
mamba activate chatbot-whatsapp
# (Alternativa pip) pip install -r requirements.txt

2) **Crear `.env` en la raíz (NO subir a Git)**
   ```dotenv
   GUPSHUP_API_KEY=tu_api_key
   GUPSHUP_APP_NAME=RolesClubBotToastmasters
   GUPSHUP_SOURCE=917834811114      # remitente sandbox (sin +)
   VERIFY_TOKEN=rolesclub-verify
   ADMIN_NUMBERS=521XXXXXXXXXX       # tu número admin (E.164 sin +)
   PORT=5000
   ```

**Importante**
- Si no estás en `ADMIN_NUMBERS`, no podrás usar comandos admin.
- Provee `.env.example` al equipo (sin secretos).

---

## 🧩 Inicialización del catálogo (Semilla)

> **Esta sección es nueva en v1.1 y es obligatoria la primera vez.**

Genera `data/club.json` con roles y miembros iniciales.  
Esto debe hacerse **antes de la primera vez que corras `app.py`**:

python src/setup_club.py

Verás: ✅ Semilla creada en data/club.json

Puedes editar `setup_club.py` para cambiar:
- Miembros (añadir/quitar, marcar invitados con `is_guest=True`)
- Roles (nombres y dificultad)

---

## Ejecución local + ngrok + Webhook

1) **Levantar Flask**
python src/app.py

2) **Autenticar ngrok** (una vez por máquina)
ngrok config add-authtoken <TU_AUTHTOKEN_PRIVADO>

3) **Exponer puerto**
ngrok http 5000

4) **Configurar Webhook en Gupshup (App > Webhooks > Add/Edit)**
- Callback URL: https://xxxxx.ngrok-free.app/webhook
- Payload: Meta format (v3)
- Eventos: Message, Sent, Delivered, Read, Failed

5) **Regla Sandbox (Gupshup)**
- Cada miembro debe enviar “hola” al sandbox para habilitar recepción 24h.
- Alternativa: compartir el **Click URL** de `Opt-ins > Onboarding mechanism` en gupshup.ai.

6) **Prueba rápida (desde el número admin)**
- Enviar “hola” al sandbox.
- INICIAR → comienza una ronda.
- ESTADO → ver pendientes.
- Los miembros responden ACEPTO / RECHAZO.

> **Tip**: inspecciona tráfico en http://127.0.0.1:4040.

---

## Cómo usar el catálogo en `app.py`

En tu `src/app.py`, **carga el club** al iniciar la app:

from models import Club

club = Club()
club.load_from_json("data/club.json")

# Ejemplo de uso dentro de tus handlers:
# club.assign_role("Paco", "Evaluador del tiempo")
# roles_sugeridos = club.available_roles_for_level(level_actual)

> **Importante**:  
> - `setup_club.py` se ejecuta **una sola vez al inicio** para crear `club.json`.  
> - Después, cada vez que corras `python src/app.py`, el bot ya carga automáticamente ese catálogo.  
> - `data/club.json` es el **catálogo persistente** (miembros + roles + niveles).  
> - `data/state.json` es el **estado de la ronda en curso** y se maneja solo desde `app.py`.

---

## Migración (opcional) desde `data/members.json`

Si aún tienes el archivo antiguo:
{
  "roles": ["Moderador", "Secretario"],
  "members": [
    {"name": "Daniel", "waid": "521..."}
  ]
}
*Recomendación:* **Deja de usarlo** y migra al nuevo `data/club.json` con `setup_club.py`.  
Si necesitas conservar “roles antiguos”, añádelos manualmente a `setup_club.py` (o edita `data/club.json` después de la semilla).

---

## Buenas prácticas (secrets y configuración)

* `.env`: nunca en Git. Usa `.env.example` como plantilla.
* **Producción**: define variables de entorno en el servicio (Render/Railway/Heroku, Docker/Compose, systemd) o usa un Secret Manager.
* Si alguna clave se sube por error: **rota** la clave y limpia el historial.

---

## Despliegue (alto nivel)

1) Sube el repo a GitHub.  
2) Usa un PaaS (Render/Railway/Heroku) o VPS con Nginx+Gunicorn.  
3) Variables de entorno = valores de `.env`.  
4) Comando de inicio simple: python src/app.py (para prototipo).  
5) Configura el Webhook de Gupshup a:  
   https://<tu-app>.onrender.com/webhook

---

## Solución de problemas comunes

* **No recibo mensajes**  
  - Enviaste “hola” (ventana 24h)?  
  - Webhook apunta a `/webhook` con `Meta v3`?  
  - `ADMIN_NUMBERS` está en E.164 sin “+”?
* **ngrok sin tráfico** → abre http://127.0.0.1:4040.  
* **Cambió la URL de ngrok** → actualiza Webhook en Gupshup y confirma “Edit Webhook”.
* **Catálogo no carga** → corre primero `python src/setup_club.py` y verifica que exista `data/club.json`.

---

## Contribución

* Crea rama: git checkout -b feat/poo-club-model  
* Commits: feat|fix|chore|docs|refactor|test: ...  
* Pull Request con descripción clara.

---

## Licencia

Proprietary. Consulta LICENSE.
