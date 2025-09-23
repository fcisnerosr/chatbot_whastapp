# Chatbot WhatsApp — Roles/Toastmasters (PROTOTIPO)

Version: 1.0
Author: Data & Flow Consulting <fcisnerosr@outlook.es>
License: Proprietary

---
## ⚠️ NOTAS IMPORTANTES (LEER ANTES DE USAR)

* Este repositorio es un **PROTOTIPO** en evolución. Úsalo para pruebas; no es aún un sistema “production-ready”.
* **NUNCA** subas el archivo `.env` al repositorio ni compartas ahí credenciales. Mantén `.env` fuera de Git (está en `.gitignore`). Provee `.env.example` como guía.
* Quien ejecute el bot debe agregarse como `ADMIN` en el `.env` para poder usar comandos administrativos (`INICIAR`/`ESTADO`/`RESET`/`CANCELAR`).
* En modo Sandbox de Gupshup, cada usuario debe escribir “hola” al número sandbox al menos una vez cada 24h para poder recibir mensajes del bot.

---
## Descripción

Bot en Python + Flask que administra rondas y asigna roles de una reunión (Toastmasters). Envía propuestas de rol por WhatsApp usando Gupshup API. Para pruebas locales expone el servidor con ngrok.

Estructura principal:
```
chatbot_whastapp/
├─ src/             # código fuente
│  └─ app.py        # servidor Flask (endpoints / y /webhook)
├─ data/             # datos locales (no públicos)
│  ├─ members.json   # catálogo de miembros y roles disponibles
│  └─ state.json     # estado persistente (se autogenera si no existe)
├─ scripts/          # utilidades
├─ notebooks/        # exploración
├─ tests/            # pruebas
├─ environment.yml   # entorno conda/mamba
├─ pyproject.toml
└─ README.md
```

Roles y comandos (resumen):
* **Admin** (debes estar en `ADMIN_NUMBERS` del `.env`): `INICIAR`, `ESTADO`, `CANCELAR`, `RESET`
* **Usuario**: `ACEPTO`, `RECHAZO`, `MI ROL`, `HOLA`

---
## Requisitos

* Python 3.12+
* Mamba/Conda (o pip)
* Cuenta de Gupshup (App WhatsApp con modo Sandbox)
* Cuenta de ngrok (gratuita) para exponer `http://localhost:5000`

---
## Instalación del entorno

1.  **Crear y activar entorno con mamba/conda:**
    ```
    mamba env create -f environment.yml
    mamba activate chatbot-whatsapp
    ```
    (Alternativa pip: `pip install -r requirements.txt`)

2.  **Crear `.env` en la raíz del proyecto (NO subir a Git). Ejemplo:**
    ```
    GUPSHUP_API_KEY=tu_api_key
    GUPSHUP_APP_NAME=RolesClubBotToastmasters
    GUPSHUP_SOURCE=917834811114           # remitente sandbox (sin +)
    VERIFY_TOKEN=rolesclub-verify
    ADMIN_NUMBERS=521XXXXXXXXXX          # tu número admin (E.164 sin +)
    PORT=5000
    ```
    **Importante:**
    * Si no estás en `ADMIN_NUMBERS`, no podrás iniciar ronda ni usar comandos admin.
    * Provee un archivo `.env.example` (sin secretos) para el equipo.

---
## Ejecución local + ngrok + Webhook

1.  **Levantar Flask (en una terminal):**
    ```
    python src/app.py
    ```
    (verás “Running on `http://127.0.0.1:5000`”)

2.  **Autenticar ngrok (una sola vez por equipo/PC):**
    ```
    ngrok config add-authtoken <TU_AUTHTOKEN_PRIVADO>
    ```

3.  **Exponer el puerto (en otra terminal):**
    ```
    ngrok http 5000
    ```
    Copia la URL pública que aparece (`https://xxxxx.ngrok-free.app`)

4.  **Configurar Webhook en Gupshup (App > Webhooks > Add/Edit):**
    * `Callback URL`: `https://xxxxx.ngrok-free.app/webhook`
    * `Payload`: `Meta format (v3)`
    * `Eventos`: `Message`, `Sent`, `Delivered`, `Read`, `Failed`

5.  **Regla Sandbox (Gupshup):**
    * Cada miembro debe escribir “hola” al número sandbox para habilitar recepción durante 24h. Repetir si pasa el tiempo.
    * Otra alternativa es compartir el link del bot en etapa de sandbox, dicho link está en gupshup.ai y en las configuraciones de la app creada, en la sección de "Opt-ins", desplegar "Onboarding mechanism", desplegar más abajo para que en la sección de "Click URL", justo debajo está el link para compartir a los miembros del club que participan en Toastmasters.

6.  **Prueba rápida (desde el número admin):**
    * Enviar “hola” al sandbox.
    * Enviar “INICIAR” para comenzar una ronda.
    * Enviar “ESTADO” para ver pendientes.
    * Responder desde los miembros con “ACEPTO” o “RECHAZO”.

**Consejo:** Abre `http://127.0.0.1:4040` para inspeccionar requests/responses de ngrok.

---
## Buenas prácticas de configuración/secrets

* `.env`: nunca en Git. Usa `.env.example` como plantilla.
* **Producción:** define variables de entorno en el servicio (Render/Railway/Heroku, Docker/compose, systemd) o usa un Secret Manager.
* Si alguna clave se subió por error: **ROTAR la clave y limpiar el historial.**

---
## Despliegue (alto nivel)

1.  Subir el repo a GitHub.
2.  PaaS (Render/Railway/Heroku) o un VPS detrás de Nginx+Gunicorn.
3.  Variables de entorno = valores de `.env`.
4.  Comando de inicio (ejemplo simple): `python src/app.py`
5.  Configurar Webhook de Gupshup a la URL pública de tu servicio:
    `https://<tu-app>.onrender.com/webhook`

---
## Solución de problemas comunes

* **No recibo mensajes en pruebas:**
    * Asegúrate de que enviaste “hola” al sandbox (ventana 24h).
    * Verifica que el webhook apunta a `/webhook` con protocolo https y Meta v3.
    * Revisa que `ADMIN_NUMBERS` tenga tu número en formato E.164 sin “+”.
* **ngrok no muestra tráfico:**
    * Abre `http://127.0.0.1:4040` para ver cada request.
* **Cambió la URL de ngrok:**
    * Actualiza el Webhook en Gupshup con la nueva URL.
    * Confirmar EditWebhook

---
## Contribución

* Crea una rama: `git checkout -b feature/nueva-funcionalidad`
* Commit con mensajes claros: `feat|fix|chore|docs: ...`
* Abre un Pull Request.

---
## Licencia

Proprietary. Consulta el archivo `LICENSE` para detalles.
