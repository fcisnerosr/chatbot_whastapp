# Chatbot WhatsApp

📦 Versión: `0.1.0`  
👤 Autor: [Data and Flow Consulting](mailto:fcisnerosr@outlook.es)  
⚖️ Licencia: Proprietary

---

## ✨ Descripción

Chatbot para programar de manera automática las sesiones de Toastmasters.  
Este proyecto fue generado automáticamente con [Cookiecutter](https://cookiecutter.readthedocs.io/), siguiendo buenas prácticas de estructura y organización en Python.  
Integra **Flask** y **Gupshup API** para automatizar la asignación de roles en reuniones.

---

## 🚀 Requisitos

- Python 3.12+
- [Mamba](https://mamba.readthedocs.io/) o [Conda](https://docs.conda.io/)
- Pip (si prefieres usarlo para dependencias)
- Cuenta y credenciales de **Gupshup**

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

Crea un archivo `.env` en la raíz del proyecto con tus credenciales:

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

## 🛠️ Uso local

1. Activa tu entorno virtual.  
2. Desde la raíz del proyecto ejecuta:

```bash
python src/app.py
```

Esto levanta un servidor Flask en `http://0.0.0.0:5000`.

### Exponer con ngrok

Si corres localmente, necesitas exponer tu puerto con [ngrok](https://ngrok.com/):

```bash
ngrok http 5000
```

Configura el webhook en Gupshup con la URL pública que genere ngrok, por ejemplo:

```
https://abcd1234.ngrok.io/webhook
```

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
