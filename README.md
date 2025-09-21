# Chatbot WhatsApp

📦 Versión: `0.1.0`  
👤 Autor: [Data and Flow Consulting](mailto:fcisnerosr@outlook.es)  
⚖️ Licencia: Proprietary

---

## ✨ Descripción

Chatbot para programar de manera automática las sesiones de Toastmasters.  
Este proyecto fue generado automáticamente con [Cookiecutter](https://cookiecutter.readthedocs.io/), siguiendo buenas prácticas de estructura y organización en Python.  
Integra **Flask** y **Gupshup API** para automatizar la asignación de roles en reuniones.  
Se expone mediante **ngrok** para pruebas locales.

---

## 🚀 Requisitos

- Python 3.12+
- [Mamba](https://mamba.readthedocs.io/) o [Conda](https://docs.conda.io/)
- Pip (si prefieres usarlo para dependencias)
- Cuenta y credenciales de **Gupshup**
- Cuenta gratuita de **ngrok** (📌 actualmente vinculada con la cuenta de GitHub de Paco)

---

## 📦 Instalación

Clona el repositorio y crea el entorno:

```bash
mamba env create -f environment.yml
mamba activate chatbot-whatsapp
```

O bien con `pip`:

```bash
pip install -r requirements.txt
```

Crea un archivo `.env` en la raíz del proyecto con tus credenciales de **Gupshup**:

```env
GUPSHUP_API_KEY=tu_api_key
GUPSHUP_APP_NAME=RolesClubBot
GUPSHUP_SOURCE=917834811114
VERIFY_TOKEN=rolesclub-verify
ADMIN_NUMBERS=521XXXXXXXXXX,521YYYYYYYYYY
PORT=5000
```

---

## 📂 Estructura del proyecto

```
chatbot_whastapp/
├── src/             # código fuente principal
│   └── app.py       # servidor Flask del bot
├── data/            # datos locales (miembros, estado)
│   ├── members.json
│   └── state.json
├── tests/           # pruebas unitarias
├── notebooks/       # notebooks de exploración
├── scripts/         # scripts auxiliares
├── README.md        # este archivo
├── LICENSE
├── requirements.txt
├── environment.yml
└── pyproject.toml
```

---
## 🛠️ Uso local (con ngrok)

### 1. Levantar Flask  
Desde la raíz del proyecto, activa tu entorno virtual y corre el bot:

```bash
python src/app.py
```

Esto levanta el servidor Flask en:

- `http://127.0.0.1:5000`  
- `http://0.0.0.0:5000`

---

### 2. Autenticación en ngrok (solo una vez por equipo/PC)  
Antes de exponer el puerto, es necesario **vincular ngrok con la cuenta del equipo** (la de Paco en GitHub).  

1. Copia el **Authtoken privado de ngrok** desde el dashboard:  
   👉 https://dashboard.ngrok.com/get-started/your-authtoken  

2. En tu terminal, pega el comando (reemplaza `XXXXX` con el token copiado):  

```bash
ngrok config add-authtoken XXXXX
```

⚠️ **Importante:** este token es **privado** (como una contraseña). **No debe compartirse en repositorios ni en archivos públicos**.  

---

### 3. Exponer el puerto con ngrok  
Ejecuta en otra terminal:

```bash
ngrok http 5000
```

Verás algo como:

```
Forwarding  https://abcd1234.ngrok-free.app -> http://localhost:5000
```

Esa **URL pública** es la que debes registrar como Webhook en **Gupshup**.  

📌 Nota: la cuenta de ngrok usada está registrada a nombre de **Paco (con GitHub)**.  


Esa URL pública es la que usaremos en Gupshup.  
📌 Nota: la cuenta de ngrok usada está registrada a nombre de Paco (con GitHub).

### 4. Configurar el **Webhook** en Gupshup:

- Ir a Gupshup > tu app > Webhooks > Add/Edit Webhook
- Pegar la URL de ngrok con `/webhook` al final:

```bash
[https://dba192d5aa01.ngrok-free.app/webhook](https://dba192d5aa01.ngrok-free.app/webhook)
```

### 5. Guarda los cambios.  

Con eso, cualquier mensaje que llegue al sandbox de Gupshup será enviado a tu bot en local.

---

## ☁️ Despliegue en la nube

Plataformas recomendadas:
- [Render](https://render.com)
- [Railway](https://railway.app)
- [Heroku](https://www.heroku.com)

Pasos:
1. Sube el repo a GitHub.  
2. Conecta el repo a la plataforma.  
3. Configura las variables de entorno (igual que en `.env`).  
4. Asegura que el comando de inicio sea:

```bash
python src/app.py
```

5. Configura la URL pública de tu app en el webhook de Gupshup:

```
https://<tu-app>.onrender.com/webhook
```

---

## ✅ Verificación rápida

1. Envía un mensaje desde WhatsApp al número de Gupshup.  
2. El bot debe responder con los comandos configurados:  
   - Admin: `INICIAR`, `ESTADO`, `CANCELAR`, `RESET`  
   - Usuario: `ACEPTO`, `RECHAZO`, `MI ROL`  

---

## 🤝 Contribución

1. Haz un **fork** del repositorio.  
2. Crea una nueva rama (`git checkout -b feature/nueva-funcionalidad`).  
3. Haz tus cambios y confirma (`git commit -m "Add: nueva funcionalidad"`).  
4. Envía un **pull request** 🚀.  

---

## 📄 Licencia

Este proyecto está bajo la licencia **Proprietary**.  
Consulta el archivo `LICENSE` para más detalles.
