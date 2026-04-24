# GitHub PRs → Clockify Sync

Dos scripts Python que sincronizan tu actividad de GitHub PRs con entradas de tiempo en Clockify.

```
GitHub API ──► generate.py ──► clockify_YYYY-MM.yaml ──► (revisión manual) ──► push.py ──► Clockify
```

---

## Instalación

```bash
# Requisitos: Python 3.10+
python --version

# Instalar dependencias
pip install -r requirements.txt
```

---

## Configuración

Copia el fichero de ejemplo y rellena tus credenciales:

```bash
cp config.yaml config.local.yaml   # nunca subas el real a git
```

### Cómo obtener cada credencial

| Campo | Dónde encontrarlo |
|---|---|
| `github_token` | GitHub → Settings → Developer settings → **Personal access tokens** → Generate new token → scope: `repo` (lectura) |
| `clockify_api_key` | Clockify → icono de perfil (arriba a la derecha) → **Profile settings** → API → API key |
| `clockify_workspace_id` | Clockify → Settings → **Workspace** → el ID aparece en la URL: `clockify.me/workspaces/{ID}/settings` |
| `clockify_user_id` | Clockify → Profile settings → la URL: `clockify.me/user/{ID}` — o déjalo en blanco si no lo sabes, push.py lo necesita pero en este setup se pone a mano |

### Opciones de configuración

```yaml
# Nombre de usuario de GitHub (exactamente como aparece en los PRs)
github_user: "mi_usuario"
github_token: "ghp_..."

clockify_api_key: "..."
clockify_workspace_id: "..."
clockify_user_id: "..."          # ID numérico largo de Clockify

# Zona horaria para calcular las horas de inicio de las entradas (09:00 local)
timezone: "Europe/Madrid"        # o "UTC", "America/New_York", etc.

repos:
  - github_repo: "org/repo-uno"
    clockify_project: "Nombre exacto del proyecto en Clockify"
  - github_repo: "org/repo-dos"
    clockify_project: "Nombre exacto del otro proyecto en Clockify"

default_hours: 8.0               # horas totales por día con actividad

# Cómo repartir horas si un día tiene PRs en los dos repos:
#   "equal"  → cada proyecto recibe default_hours / 2
#   "manual" → horas quedan en null para que las rellenes tú
split_strategy: "equal"

# Qué fecha de cada PR determina en qué día se crea la entrada:
#   "created_at" → fecha en que se abrió el PR
#   "merged_at"  → fecha en que se fusionó (los PRs abiertos se ignoran)
pr_date_field: "created_at"

max_pages: 10   # límite de seguridad: 10 páginas × 100 PRs = 1000 PRs por repo
```

---

## Flujo de uso recomendado

### Paso 1 — Generar el YAML

```bash
# Mes actual
python generate.py

# Mes concreto
python generate.py --month 2026-04

# Con config alternativo y log detallado
python generate.py --month 2026-04 --config config.local.yaml --verbose
```

Genera `clockify_2026-04.yaml`. Ejemplo de salida:

```yaml
month: 2026-04
generated_at: "2026-04-23T10:00:00"
entries:
  - date: "2026-04-03"
    entries:
      - project: "Proyecto A"
        hours: 8.0
        description: "PR #42 Fix payment webhook | PR #43 Refactor service layer"
  - date: "2026-04-07"
    entries:
      - project: "Proyecto A"
        hours: 4.0
        description: "PR #44 Add email notifications"
      - project: "Proyecto B"
        hours: 4.0
        description: "PR #11 Setup initial project structure"
```

### Paso 2 — Revisar y ajustar el YAML (opcional)

Abre `clockify_2026-04.yaml` en tu editor y:
- Cambia horas si algún día fue más corto
- Rellena los campos `hours: null` (si usas `split_strategy: manual`)
- Añade días manualmente si trabajaste sin abrir PRs
- Elimina días que no quieras registrar

### Paso 3 — Simulación

Antes de crear nada real, comprueba qué haría el script:

```bash
python push.py --file clockify_2026-04.yaml --dry-run
```

### Paso 4 — Crear entradas reales

```bash
python push.py --file clockify_2026-04.yaml
```

### Paso 5 — Reejecutar es seguro

El script detecta duplicados (mismo día + mismo proyecto + misma descripción) y los salta automáticamente. Puedes ejecutar push.py varias veces sin crear entradas duplicadas.

---

## Referencia de comandos

### generate.py

```bash
# Opciones disponibles
python generate.py --help

# Mes actual (default)
python generate.py

# Mes específico
python generate.py --month 2026-04

# Config alternativo
python generate.py --config /ruta/config.yaml

# Log detallado (DEBUG)
python generate.py --verbose
```

### push.py

```bash
# Opciones disponibles
python push.py --help

# Procesar el YAML del mes actual (default)
python push.py

# YAML específico
python push.py --file clockify_2026-04.yaml

# Simulación sin crear nada
python push.py --file clockify_2026-04.yaml --dry-run

# Solo un día concreto
python push.py --file clockify_2026-04.yaml --date 2026-04-07

# Solo un día, en simulación
python push.py --file clockify_2026-04.yaml --date 2026-04-07 --dry-run

# Log detallado
python push.py --verbose
```

---

## Comportamiento ante errores

| Situación | Comportamiento |
|---|---|
| Token de GitHub inválido | Mensaje claro y salida inmediata |
| Rate limit de GitHub | Aviso con timestamp de reset |
| Repo de GitHub no encontrado | Mensaje con URL y salida |
| API key de Clockify inválida | Mensaje y salida |
| Proyecto Clockify no encontrado | Lista los proyectos disponibles y sale |
| Entrada duplicada | La salta y lo indica en el resumen |
| `hours: null` en el YAML | La salta, la lista al final como "pendiente de revisión" |

---

## Añadir días sin PRs

El YAML generado solo incluye días con actividad en GitHub. Si trabajaste un día sin abrir PRs, añade la entrada manualmente:

```yaml
  - date: "2026-04-10"
    entries:
      - project: "Proyecto A"
        hours: 8.0
        description: "Reuniones y revisiones internas"
```

---

## Notas sobre la API de GitHub

- El token solo necesita el scope `repo` (lectura de PRs).
- Con autenticación, el límite es 5.000 peticiones/hora (más que suficiente).
- `max_pages: 10` en el config equivale a 1.000 PRs por repo. Auméntalo si tu repo tiene historial muy largo.
- Si usas `pr_date_field: merged_at`, solo se incluyen PRs que ya estén fusionados.

---

## Notas sobre la API de Clockify

- Las entradas se crean con inicio a las **09:00 hora local** (según `timezone` en config) y duración según `hours`.
- La detección de duplicados compara: mismo día local + mismo proyecto + misma descripción exacta.
- Si cambias la descripción en el YAML y vuelves a ejecutar, se creará una entrada nueva (la antigua no se borra).
