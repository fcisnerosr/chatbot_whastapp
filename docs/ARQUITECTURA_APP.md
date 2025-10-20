# Arquitectura Detallada de app.py

**Última actualización:** 20 de octubre, 2025  
**Propósito:** Documentación completa del funcionamiento de src/app.py para facilitar el mantenimiento futuro

---

## Índice
1. [Visión General](#visión-general)
2. [Configuración y Arranque](#configuración-y-arranque)
3. [Sistema Multi-Club](#sistema-multi-club)
4. [Persistencia de Datos](#persistencia-de-datos)
5. [Lógica de Asignación de Roles](#lógica-de-asignación-de-roles)
6. [Sistema de Menús Numéricos](#sistema-de-menús-numéricos)
7. [Procesamiento de Mensajes (Webhook)](#procesamiento-de-mensajes-webhook)
8. [Flujos de Usuario](#flujos-de-usuario)
9. [Administración de Miembros](#administración-de-miembros)
10. [Puntos de Extensión](#puntos-de-extensión)

---

## Visión General

`app.py` es el servidor Flask que maneja todo el bot de asignación de roles para Toastmasters. El bot:

- **Soporta múltiples clubes** de forma aislada (cada club con sus propios miembros, roles y estado)
- **Usa menús numéricos** para toda la interacción (usuario envía "1", "2", "3", etc.)
- **Asigna roles inteligentemente** considerando:
  - Nivel del miembro vs dificultad del rol
  - Ciclo de roles (evita que un miembro repita hasta completar todos)
  - Disponibilidad (evita asignar múltiples roles al mismo miembro en una ronda)
- **Persiste estado** en JSON (atómico, thread-safe)
- **Se integra con Gupshup** para enviar/recibir mensajes de WhatsApp

---

## Configuración y Arranque

### Variables de Entorno Requeridas

```python
# .env
GUPSHUP_API_KEY=...          # API key de Gupshup
GUPSHUP_APP_NAME=...         # Nombre de la app
GUPSHUP_SOURCE=917834811114  # Número del bot (E.164 sin '+')
CLUBS_DIR=data/clubs         # Directorio con clubes
VERIFY_TOKEN=...             # Token para webhook
PORT=5000                    # Puerto Flask
```

### Proceso de Carga

1. **`load_dotenv()`**: Carga variables desde `.env`
2. **`load_config()`**: Valida variables críticas y crea objeto `Config`
3. **`load_all_clubs()`**: Lee `data/clubs/registry.json` y carga cada club:
   - Lee `club.json` (miembros, roles)
   - Lee `state.json` (estado de ronda)
   - Crea contexto `Ctx` por cada club
   - Guarda contextos en diccionario global `_CTX`

### Estructura del Contexto (Ctx)

```python
@dataclass
class Ctx:
    club_id: str              # Identificador único (ej: "club_1")
    club: Club                # Modelo POO con members y roles
    state_store: StateStore   # Manejador de state.json
    club_file: Path           # Ruta a club.json
    admins: Set[str]          # WAIDs de administradores
    all_numbers: Tuple        # Todos los WAIDs del club
    members_index: Set[str]   # Índice rápido de miembros
```

---

## Sistema Multi-Club

### Archivo registry.json

Formato:
```json
{
  "clubs": {
    "club_1": {
      "admins": ["5215634948177"]
    },
    "club_2": {
      "admins": ["5212293655442", "5215634948177"]
    }
  }
}
```

### Funciones Clave

- **`admin_clubs(waid)`**: Devuelve lista de clubes donde el waid es admin
- **`member_club(waid)`**: Devuelve el club al que pertenece como miembro
- **`infer_user_club(waid)`**: Infiere club por contexto (miembro, admin, o invitación pendiente)

### Webhook Unificado

El webhook es **único** (`/webhook`) para todos los clubes. El bot determina el club correcto por:
1. **Sesión en memoria**: Si el usuario ya está navegando menús, usa `session["club"]`
2. **Membresía**: Si es miembro de un club, usa ese
3. **Administración**: Si es admin de un solo club, usa ese
4. **Inferencia**: Si tiene invitación pendiente o rol asignado, usa el club correspondiente

---

## Persistencia de Datos

### state.json - Estado de Ronda

```json
{
  "round": 3,
  "pending": {
    "Evaluador del tiempo": {
      "candidate": "5215551234567",
      "declined_by": [],
      "accepted": false
    }
  },
  "accepted": {
    "Evaluador gramatical": {
      "waid": "5219991234567",
      "name": "Paco"
    }
  },
  "members_cycle": {
    "5215551234567": ["Evaluador de muletillas"],
    "5219991234567": ["Evaluador del tiempo", "Evaluador gramatical"]
  },
  "last_summary": "...",
  "canceled": false
}
```

### Escritura Atómica

`_dump_json_atomic()` garantiza que nunca quede un archivo corrupto:
1. Escribe a archivo temporal
2. Llama `os.replace()` (atómico en POSIX)
3. El reemplazo es instantáneo, no hay estado intermedio

### StateStore

Usa un `Lock` para evitar condiciones de carrera cuando múltiples threads/procesos acceden al mismo `state.json`.

---

## Lógica de Asignación de Roles

### Inicio de Ronda (`start_new_round`)

1. **Validación**: Verifica que no haya roles pendientes de ronda anterior
2. **Incrementa round**: `st["round"] += 1`
3. **Ordenamiento por dificultad**: Asigna primero los roles más difíciles
4. **Selección de candidatos**: Por cada rol:
   - Excluye miembros que ya tienen roles asignados en esta ronda
   - Excluye miembros con roles pendientes
   - Llama `choose_candidate_hier()` para seleccionar
5. **Envía invitaciones**: A cada candidato con menú numérico (1 Aceptar / 2 Rechazar / 3 Después)

### Algoritmo de Selección de Candidatos (`choose_candidate_hier`)

**Estrategia jerárquica:**

```
Prioridad 1: Miembros con nivel >= dificultad del rol Y que NO lo han hecho
Prioridad 2: Miembros con nivel >= dificultad Y que SÍ lo han hecho (repetir)
Prioridad 3 (fallback): Miembros con nivel < dificultad (orden descendente)
  - Primero: nivel inmediatamente inferior sin repetir
  - Luego: nivel inmediatamente inferior con repetir
  - Y así sucesivamente hasta nivel 1
```

**Ejemplo:**
- Rol: "Evaluador gramatical" (dificultad 2)
- Miembros:
  - Paco (nivel 2) → Ya hizo este rol
  - Roger (nivel 2) → No lo ha hecho
  - Daniel (nivel 1)

**Selección:** Roger (prioridad 1: nivel suficiente, no ha hecho el rol)

### Ciclo de Roles (`members_cycle`)

Cada miembro tiene un historial de roles hechos. Cuando completa todos los roles del club, su ciclo se resetea:

```python
if len(done_list) >= len(ctx.club.roles):
    done_list = []  # Reinicia ciclo
```

### Aceptar Rol (`handle_accept`)

1. Busca rol pendiente para el waid
2. Marca como `accepted: true`
3. Agrega a `st["accepted"]`
4. Actualiza `members_cycle`
5. **Persiste en club.json**: Actualiza `member.roles_done` y nivel
6. Guarda estado
7. Llama `check_and_announce_if_complete()` para ver si todos aceptaron

### Rechazar Rol (`handle_reject`)

1. Busca rol pendiente para el waid
2. Agrega waid a `declined_by`
3. Busca nuevo candidato (excluyendo todos los que rechazaron)
4. Si hay nuevo candidato:
   - Actualiza `info["candidate"]`
   - Envía invitación al nuevo candidato
5. Si no hay más candidatos:
   - Elimina el rol de `pending`
   - Notifica a admins que no hay opciones

---

## Sistema de Menús Numéricos

### Sesiones en Memoria

```python
SESSION: Dict[str, dict] = {}  # {waid: session_data}

# Estructura de session_data:
{
  "mode": "root" | "member" | "admin" | "admin_pick",
  "club": "club_1",
  "awaiting": None | "admin_add_member" | "admin_remove_member" | "pick_admin_club",
  "buffer": None  # Para datos temporales si se necesitan
}
```

### Flujo de Navegación

```
┌─────────────┐
│  Menú Raíz  │ ← Punto de entrada
└──────┬──────┘
       │
       ├─→ Menú Miembro (si es miembro)
       │   ├─→ 1) Mi rol
       │   ├─→ 2) Estado de ronda
       │   └─→ 9) Volver
       │
       ├─→ Menú Admin (si es admin)
       │   ├─→ 1) Iniciar ronda
       │   ├─→ 2) Ver estado
       │   ├─→ 3) Cancelar ronda
       │   ├─→ 4) Resetear estado
       │   ├─→ 5) Ver miembros
       │   ├─→ 6) Agregar miembro → [flujo texto libre]
       │   ├─→ 7) Eliminar miembro → [flujo texto libre]
       │   ├─→ 8) Cambiar de club
       │   └─→ 9) Volver
       │
       └─→ Mi estado de rol (todos)
```

### Renderizado de Menús

- **`render_root_menu(waid)`**: Menú principal adaptado al usuario
- **`render_member_menu(ctx)`**: Opciones para miembros
- **`render_admin_menu(ctx)`**: Opciones para administradores
- **`render_admin_club_picker(aclubs)`**: Selector de club (si admin de múltiples)

---

## Procesamiento de Mensajes (Webhook)

### Flujo del Webhook POST

```python
@app.route("/webhook", methods=["POST"])
def webhook_post():
    # 1. Extrae waid y body del mensaje
    # 2. Carga sesión del usuario
    # 3. Determina contexto (club actual)
    
    # PRIORIDAD 1: Si tiene invitación pendiente y envía 1/2/3
    if is_number and role_pending and body in ("1", "2", "3"):
        # Procesa aceptar/rechazar/postponer
        # Muestra menú raíz después
        continue
    
    # PRIORIDAD 2: Router por modo de sesión
    if s["mode"] == "root":
        # Procesa opciones del menú raíz
    elif s["mode"] == "member":
        # Procesa opciones del menú miembro
    elif s["mode"] == "admin":
        # Procesa opciones del menú admin
        # Si awaiting="admin_add_member", espera texto libre
        # Si awaiting="admin_remove_member", espera texto libre
    elif s["mode"] == "admin_pick":
        # Procesa selección de club
    
    # PRIORIDAD 3: Compatibilidad con comandos texto
    if body in ("mi rol", "acepto", "rechazo"):
        # Procesa comando legacy
    
    # FALLBACK: Muestra menú raíz
    send_text(waid, render_root_menu(waid))
```

### Detección de Números

```python
is_number = re.fullmatch(r"\d{1,3}", body) is not None
```

Si el usuario envía solo dígitos (1-3 dígitos), se interpreta como opción de menú.

### Invitaciones Pendientes

Tienen **máxima prioridad**. Si un usuario tiene una invitación pendiente y envía "1", "2", o "3", se procesa esa invitación inmediatamente, sin importar en qué menú estaba.

---

## Flujos de Usuario

### Flujo: Miembro Recibe Invitación

1. Admin inicia ronda → Bot envía: "Hola [Nombre], se te propone [Rol]... 1) Aceptar / 2) Rechazar / 3) Después"
2. Usuario envía "1" → Bot confirma y actualiza estado
3. Bot verifica si todos aceptaron → Si sí, broadcast del resumen final

### Flujo: Admin Inicia Ronda

1. Admin envía "1" en menú admin
2. Bot llama `start_new_round()`
3. Bot asigna candidatos a cada rol
4. Bot envía invitaciones a todos los candidatos
5. Bot notifica a admins si algún rol quedó sin candidato

### Flujo: Admin Agrega Miembro

1. Admin envía "6" en menú admin
2. Bot responde: "Envía: Nombre, 55XXXXXXXX"
3. Bot establece `awaiting="admin_add_member"`
4. Usuario envía "Paco, 5551234567"
5. Bot llama `admin_add_member()`
6. Bot actualiza club.json, state.json, e índices en memoria
7. Bot confirma y regresa a menú admin

---

## Administración de Miembros

### Agregar Miembro (`admin_add_member`)

**Pasos:**
1. Normaliza nombre y número (convierte a E.164)
2. Valida que no exista ese número en el club
3. Crea instancia `Member` con nivel inicial (default 1)
4. Agrega a `ctx.club.members`
5. Actualiza `st["members_cycle"][waid] = []`
6. **Persiste club.json**
7. **Actualiza índices en memoria**: `ctx.members_index.add(waid)` y `ctx.all_numbers`

**Importante:** Los pasos 7 son críticos. Sin actualizar los índices, el bot no reconocería al nuevo miembro hasta reiniciar el servidor.

### Eliminar Miembro (`admin_remove_member`)

**Validaciones:**
1. Busca miembro por waid o nombre
2. Verifica que NO tenga roles pendientes o aceptados en ronda actual
3. Si pasa validación:
   - Remueve de `ctx.club.members`
   - Elimina de `st["members_cycle"]`
   - Persiste cambios
   - **Actualiza índices en memoria**

---

## Puntos de Extensión

### Para Agregar "Palabra del Día"

**Ubicación:** `handle_accept()` (línea ~475)

**Lógica sugerida:**
```python
if role == "Evaluador gramatical":
    # Guardar en sesión que estamos esperando palabra
    set_session(waid, awaiting="word_of_day_step1", buffer={"role": role})
    send_text(waid, "Por favor, envía la palabra del día:")
    return "Esperando palabra..."

# Luego en webhook_post, antes del router de menús:
if awaiting == "word_of_day_step1":
    set_session(waid, awaiting="word_of_day_step2", buffer={...buffer, "palabra": body_raw})
    send_text(waid, "Ahora envía el significado:")
    continue

if awaiting == "word_of_day_step2":
    set_session(waid, awaiting="word_of_day_step3", buffer={...buffer, "significado": body_raw})
    send_text(waid, "Finalmente, envía un ejemplo de uso:")
    continue

if awaiting == "word_of_day_step3":
    buffer["ejemplo"] = body_raw
    # Guardar en state.json
    st["word_of_the_day"] = {
        "palabra": buffer["palabra"],
        "significado": buffer["significado"],
        "ejemplo": buffer["ejemplo"],
        "waid": waid
    }
    ctx.state_store.save(st)
    # Confirmar rol
    # ... (lógica normal de handle_accept)
```

### Para Generar PDF

**Ubicación:** Nueva función `generate_agenda_pdf(ctx)`

**Datos disponibles:**
```python
st = ctx.state_store.load()
roles_asignados = st["accepted"]  # {role: {waid, name}}
palabra_del_dia = st.get("word_of_the_day", {})
reunion_numero = st["round"]
```

**Integración con LaTeX:**
```python
import subprocess

def generate_agenda_pdf(ctx: Ctx) -> Path:
    st = ctx.state_store.load()
    
    # Plantilla LaTeX
    template = r"""
    \documentclass{article}
    \usepackage[utf8]{inputenc}
    \usepackage[spanish]{babel}
    \begin{document}
    \title{Reunión {{ round }} - {{ club_id }}}
    \maketitle
    
    \section{Roles}
    {% for role, info in roles.items() %}
    \item \textbf{{ role }}: {{ info.name }}
    {% endfor %}
    
    \section{Palabra del Día}
    \textbf{Palabra:} {{ word.palabra }} \\
    \textbf{Significado:} {{ word.significado }} \\
    \textbf{Ejemplo:} {{ word.ejemplo }}
    \end{document}
    """
    
    # Renderizar con Jinja2
    # Compilar con pdflatex
    # Devolver Path al PDF generado
```

---

## Debugging y Logs

### Nivel de Log

Ajusta en `.env`:
```
LOG_LEVEL=DEBUG  # Para ver todos los mensajes
LOG_LEVEL=INFO   # Normal
LOG_LEVEL=WARNING  # Solo advertencias y errores
```

### Logs Importantes

```python
log.info("Cargado club %s (miembros=%d)", club_id, len(members))  # Al arrancar
log.info("Mensaje de %s: %s", waid, body)  # Cada mensaje recibido
log.warning("Gupshup %s: %s", status, text)  # Errores de API
log.exception("Error procesando webhook")  # Excepciones no controladas
```

### Inspeccionar Estado

Para ver el estado de un club en cualquier momento:
```python
ctx = _CTX["club_1"]
st = ctx.state_store.load()
print(json.dumps(st, indent=2))
```

---

## Preguntas Frecuentes

### ¿Cómo agregar un nuevo club?

1. Crea `data/clubs/nuevo_club/club.json` con estructura:
```json
{
  "members": [...],
  "roles": [...]
}
```

2. Agrega entrada en `data/clubs/registry.json`:
```json
{
  "clubs": {
    "nuevo_club": {"admins": ["521XXXXXXXXXX"]}
  }
}
```

3. Reinicia el servidor

### ¿Cómo cambiar la dificultad de un rol?

Edita `club.json`:
```json
{
  "name": "Evaluador gramatical",
  "difficulty": 3  ← Cambia este valor
}
```

Reinicia el servidor.

### ¿Por qué un miembro no recibe invitaciones?

Verifica:
1. Su `level` es >= `difficulty` del rol
2. No está en la lista de `excluded` (ya tiene otro rol pendiente)
3. Su waid está correctamente en `club.json`
4. El club está cargado en `_CTX` (revisa logs al arranque)

### ¿Cómo resetear completamente un club?

Opción 1 (desde el bot): Admin envía "4" en menú admin → "Resetear estado"

Opción 2 (manual): Elimina o vacía `data/clubs/<club_id>/state.json` y reinicia

---

## Mantenimiento Futuro

### Antes de hacer cambios:

1. **Lee esta documentación** para entender el flujo completo
2. **Revisa los comentarios** en el código (secciones 1-6)
3. **Prueba en un club de test** antes de producción
4. **Actualiza esta documentación** si cambias la arquitectura

### Testing recomendado:

1. Crear club de prueba con 3-5 miembros ficticios
2. Iniciar ronda y aceptar/rechazar roles
3. Agregar/eliminar miembros en medio de una ronda
4. Simular múltiples rechazos hasta agotar candidatos
5. Verificar que el ciclo de roles se reinicia correctamente

---

**Fin del documento** | Cualquier duda, revisa el código con estos comentarios como guía.
