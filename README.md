# Plataforma de Tickets JANOTEC

Plataforma web para que los clientes de **Jano Tec S.A.S.** radiquen solicitudes de soporte
(CCTV, alarmas, control de acceso, redes…) y el área de soporte les haga
seguimiento, mientras el cliente ve el avance en tiempo real.

**Stack:** Python 3.11+ · Django 5.2 · MySQL 8 · Bootstrap 5 (servido localmente, sin CDN)

---

## Qué hace

### Flujo de un ticket técnico
**Generado → Recepcionado → En cronograma → Autorizado → Visita técnica ejecutada → Cerrado**
(o **Pendiente nueva visita** si no quedó solucionado; vuelve a programarse).

**1. Registro de la solicitud.** La registran los **clientes** (a nombre de su empresa) y, a nombre de cualquier
cliente, el **personal JANOTEC**, la **central de atención** y los **ejecutivos comerciales**. Campos:
quién lo gestiona y fecha de registro (automáticos), empresa, sede, departamento, municipio, dirección,
tipo de ticket (mantenimiento, servicio técnico, toma de requerimientos), sistema a intervenir (CCTV, alarma de
intrusión, redes de comunicaciones, energía solar, control de acceso), descripción, días y **rangos de fecha y hora
sin disponibilidad**, foto de la novedad, teléfono, **varios correos** para las respuestas, nivel de urgencia,
**trabajo seguro en alturas** y **acompañamiento SST**. Se genera el radicado `JNT-2026-000001`.

**2. Recepción y verificación** (coordinador TIC, dirección). En la bandeja se ven todos los tickets; se puede
**editar cualquier campo** (queda nota interna de qué cambió) y marcarlo **«Recepcionado»**.

**3. Cronograma semanal** (menú *Cronograma*, reemplaza cronogramajanotec.netlify.app). Los tickets recepcionados
se **arrastran a un día**: se llenan solos cliente, sede, actividad, tipo y «notificar a». Se eligen horario,
encargados, acompañantes, insumos, SST, vehículo y viáticos. Si el horario cae cuando el cliente dijo que no puede,
**sale una alerta y la actividad queda en rojo** (también si no tiene correo o si el cliente la rechaza).
Con **«Enviar autorización»** le llega al cliente un correo con día, hora, técnicos y los botones **Aceptar / Rechazar**,
con la **cédula y los aportes a seguridad social** de cada técnico adjuntos (y los **certificados de alturas** si la
actividad es en alturas). Al aceptar, el ticket pasa a «Autorizado» y, si lleva SST, se envía la solicitud de
acompañamiento a `NOTIFICAR_SST_EMAIL`. Si la visita se reprograma, los enlaces anteriores dejan de valer.

**Pestañas del cronograma** (las mismas de la plataforma anterior):
- **Correos y envío:** cada técnico recibe su cronograma de la semana con los insumos por sede y la administrativa
  (`NOTIFICAR_ADMINISTRATIVA_EMAIL`) el consolidado de todas las sedes, con vista previa antes de enviar.
  Si después del envío se agrega, retira o cambia una actividad, el tablero lo avisa y **«Notificar cambios»** envía
  «IMPORTANTE – Cambios en el cronograma» con qué cambió, solo a los técnicos afectados y a la administrativa.
- **Insumos de la semana:** suma lo escrito en cada actividad («3 batería 12V 7.5 A, 20 m UTP») para alistar el almacén.
- **Indicadores:** tipo más frecuente, cliente más recurrente, sede a priorizar y gráficas por tipo, técnico,
  cliente, sede, línea y soportes por cliente.
- **Historial:** consulta cualquier periodo (este mes, trimestre, año o fechas a elección): actividades, semanas,
  soportes correctivos, cliente más recurrente y gráficas por tipo, semana, técnico, línea, cliente y sede.
- **Directorio:** administrativa, SST y contactos (cliente o almacén) con su correo; los técnicos son los usuarios.
  Los contactos se agregan a «Notificar a» con un clic. Sin correo, la actividad queda en rojo.
- **Configuración del envío:** envío automático sí/no, hora, qué semana (siguiente o en curso), «remite como»,
  aviso a contactos y el **link de solo lectura** de la semana para la administrativa (no necesita usuario).
- En el tablero: vista **Tablero** o **Tabla** (las columnas de la plataforma anterior), **duplicar** y **correo**
  de cada actividad, **correo SST** (ver, copiar o enviar ya), **Exportar PDF (WhatsApp)**, **Respaldo** (.json),
  **Importar** (el .json de cronogramajanotec o de esta plataforma, sin duplicar) y **Vaciar** la semana.
- **Envío automático de los jueves** (reemplaza n8n): programar `python manage.py enviar_cronograma` (cron en Linux,
  Programador de tareas en Windows). Envía la semana siguiente; si ya se envió, solo el aviso de cambios; si no hay
  nada nuevo, no envía nada. `--simular` muestra qué enviaría.

**Documentos del personal** (*Cronograma → Documentos del personal*): se suben una vez y se **actualizan** cada vez
que cambian (el anterior queda en el historial). La plataforma avisa si falta alguno o está vencido antes de enviar.
Cada técnico puede actualizar los suyos en *Mis documentos*.

**4. Visita.** El técnico de la visita diligencia el acta; si quedó solucionado el ticket pasa a «Visita técnica
ejecutada» y el cliente **confirma** o pide una nueva visita. Si no responde en **3 días**
(`TICKET_DIAS_CIERRE_AUTOMATICO`) se cierra solo; luego puede **calificar** la atención.

El cliente sigue todo con una **barra de progreso** y una **línea de tiempo**; las **notas internas** nunca las ve.
**Reportes**: tickets por estado, sistema, empresa, carga por técnico, tiempo de atención y satisfacción.

### Acta de soporte desde el ticket
El técnico diligencia el **acta de soporte** desde el ticket, con los mismos campos del formato
«Acta de Soporte, Instalación, Desinstalación V1»: actividad, sistema, diagnóstico, relación de equipos con
fotos, resultado, observaciones, **firmas táctiles** (técnico, acompañante y representante del cliente),
copia por correo y ubicación GPS. Queda en el historial de la sede y el cliente la ve en su solicitud.
Las actas de la plataforma se numeran desde **100001** (las de SafetyTrack conservan su número).

En el acta, el técnico puede reportar una **oportunidad comercial** («una venta jala otra»): servicios que el
cliente podría necesitar y una nota. Es interna (el cliente no la ve) y llega a la bandeja **Oportunidades** del
área comercial (Nueva → Contactado → Cotizado → Ganada / Descartada) y por correo a `NOTIFICAR_COMERCIAL_EMAIL`.

> La cámara del celular y el GPS solo funcionan en **HTTPS** (o en `localhost`): en producción el sitio
> debe ir con certificado (ver «Paso a producción»).

### Dos áreas, un mismo ticket
La plataforma separa el **área técnica** (Juan Daniel Guana, coordinador) del **área comercial**. Cada una
ve en su panel solo lo suyo; **Dirección** (Miguel Ángel Tibamoso) y el superusuario ven las dos.
Un requerimiento comercial conserva **el mismo número `JNT-…`** de principio a fin mientras pasa de un área a otra,
y el cliente lo sigue siempre desde *Mis solicitudes*:

1. **Comercial** radica la ficha (contacto, días en que no atiende, necesidad, calificación, presupuesto, anexos),
   la recibe del **cliente** («Solicitar cotización») o la convierte desde una **oportunidad** del acta.
   Puede ser de un **prospecto**; pasa a cliente cuando aprueba.
2. Si hace falta visita, comercial pide **levantamiento técnico**: el ticket entra a la bandeja técnica de Guana,
   que asigna técnico y visita. El técnico **devuelve el resultado** (desde el ticket o marcando la casilla del acta)
   y el ticket vuelve a comercial. En un levantamiento no se puede «resolver»: solo devolver.
3. Comercial adjunta la **propuesta (PDF)**; al marcarla enviada le llega al cliente por correo, que la
   **aprueba o rechaza** desde su solicitud (rechazada ⇒ se cierra).
4. Aprobada ⇒ comercial la **traspasa a instalación**: el mismo ticket pasa al área técnica; comercial lo sigue en consulta.

Lo interno de comercial (presupuesto, nivel de decisión, valor de la propuesta) no lo ven el cliente ni los técnicos.
Las **oportunidades** que reportan los técnicos llegan al área comercial, no al coordinador técnico.

### Roles
| Rol | Qué puede hacer |
|---|---|
| **Superusuario** (programación) | Todo, incluido crear coordinadores y Dirección. Se crea con `python manage.py createsuperuser` |
| **Dirección** | Ve y gestiona las dos áreas (bandeja técnica, comercial, oportunidades, reportes, *Configuración*) |
| **Área técnica (coordinador)** | Tickets del área técnica: direcciona, fija prioridad, asigna técnico y visita; reportes y *Configuración*. No ve lo que está en comercial |
| **Área comercial** | Requerimientos comerciales y oportunidades: radica, pide levantamientos, envía propuestas y traspasa a instalación. No ve los tickets técnicos puros |
| **Encargado (técnico)** | Solo sus tickets asignados: respuestas al cliente, notas internas, actas y avance de estado. No asigna ni cambia prioridad |
| **Central de atención** | Registra solicitudes a nombre de cualquier cliente y sigue las que registró |
| **Cliente** | Su propio usuario: **Mis solicitudes** (técnicas y comerciales), **Radicar solicitud** y **Solicitar cotización**. Limitado a su empresa |

En **Configuración** (grupo «Área administrativa», asignado automáticamente por el rol) el coordinador
crea y edita empresas, sedes, categorías, actas y usuarios **cliente, técnico y comercial**. No puede borrar empresas ni
usuarios (se desactivan), ni crear coordinadores o Dirección, ni tocar cuentas de superusuario.

Los adjuntos **no son públicos**: se descargan por una vista que verifica permisos en cada solicitud.

---

## Instalación local (Windows / Linux / macOS)

### 1. Requisitos
- Python 3.11 o superior
- MySQL 8 (o MariaDB 10.6+)
  > **XAMPP:** las versiones actuales de XAMPP traen MariaDB **10.4**, que Django 5.2 **no acepta**
  > (error `MariaDB 10.5 or later is required`). Instale MySQL 8 (MySQL Installer para Windows,
  > puerto 3306, deteniendo antes el MySQL de XAMPP) o use `DB_ENGINE=sqlite` mientras tanto.
- En Windows, `mysqlclient` se instala con `pip` sin compilar. En Linux (Ubuntu/Debian) instale antes:
  `sudo apt install python3-dev default-libmysqlclient-dev build-essential pkg-config`

### 2. Base de datos
En la consola de MySQL:
```sql
CREATE DATABASE janotec_tickets CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'janotec'@'localhost' IDENTIFIED BY 'una-clave-segura';
GRANT ALL PRIVILEGES ON janotec_tickets.* TO 'janotec'@'localhost';
FLUSH PRIVILEGES;
```

### 3. Proyecto
```bash
cd janotec_tickets
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

copy .env.example .env        # Windows   (Linux/Mac: cp .env.example .env)
# Edite .env: DB_PASSWORD y, si quiere, DJANGO_SECRET_KEY

python manage.py migrate
python manage.py inicializar          # crea las categorías base
python manage.py createsuperuser      # su usuario administrador
python manage.py runserver
```
Abra <http://127.0.0.1:8000>.

### 4. Datos de prueba (opcional, solo desarrollo)
> Solo en una base aparte (`DB_ENGINE=sqlite`): el comando se niega a correr si la base ya tiene
> datos reales importados, para no mezclarlos.
```bash
python manage.py datos_demo
```
Crea 2 empresas, sedes, 4 tickets en distintos estados y estos usuarios
(contraseña de todos: `Janotec2026*`):

| Usuario | Rol |
|---|---|
| `cliente1` | Cliente · Comercializadora El Llano |
| `cliente2` | Cliente · Clínica Demo del Meta |
| `tecnico1`, `tecnico2` | Encargados (técnicos) |
| `coordinador` | Área administrativa (supervisor) |

> ¿Sin MySQL a la mano? Ponga `DB_ENGINE=sqlite` en `.env` para probar rápido.

### 4b. Datos reales (clientes, sedes, personal e historial de actas)
Los datos consolidados de la carpeta «3. CLIENTES» están en
`datos_iniciales/Datos maestros JANOTEC.xlsx` (excluido de git: contiene datos reales).
Revise y corrija el Excel (hoja **PENDIENTES** primero) y cárguelo:
```bash
python manage.py importar_maestros "datos_iniciales/Datos maestros JANOTEC.xlsx" --simular   # ver qué haría
python manage.py importar_maestros "datos_iniciales/Datos maestros JANOTEC.xlsx"
```
Se puede repetir tras cada corrección: actualiza sin duplicar y nunca borra ni cambia contraseñas.
El personal se crea **sin contraseña**: cada persona la define con *¿Olvidó su contraseña?*
(por eso necesita correo) o el supervisor se la asigna en Configuración.

### 5. Pruebas automáticas
```bash
python manage.py test tickets
```
34 pruebas que cubren radicados, historial de actas por rol, importación de datos maestros, el formulario de radicación (el cliente no fija prioridad ni responsable),
roles (el encargado solo ve sus tickets y no reasigna; solo el área administrativa asigna), permisos por empresa, notas internas ocultas al cliente,
adjuntos protegidos, flujo de confirmación/reapertura, calificación y notificaciones.

---

## Puesta en marcha diaria (alta de clientes)

1. Entrar como supervisor → **Configuración** → *Empresas cliente* → crear la empresa con su NIT
   y, en la misma pantalla, sus **sedes**.
2. *Usuarios* → crear el usuario del cliente: rol **Cliente**, empresa, correo y teléfono.
3. Entregar al cliente la URL, su usuario y una contraseña temporal (puede cambiarla en el menú de su nombre,
   o usar *¿Olvidó su contraseña?*).
4. Para técnicos: crear el usuario con rol **Encargado (técnico)**; para quienes direccionan y
   asignan, rol **Área administrativa (supervisor)** y marcar *Es staff* para que entren a Configuración.

---

## Paso a producción (servidor Linux, ej. Ubuntu en un VPS)

1. **`.env` de producción**
   ```ini
   DJANGO_DEBUG=False
   DJANGO_SECRET_KEY=<genere una con: python -c "import secrets;print(secrets.token_urlsafe(50))">
   DJANGO_ALLOWED_HOSTS=soporte.janotec.com.co
   DJANGO_CSRF_TRUSTED_ORIGINS=https://soporte.janotec.com.co
   SITIO_URL=https://soporte.janotec.com.co
   EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
   EMAIL_HOST=...   EMAIL_HOST_USER=...   EMAIL_HOST_PASSWORD=...
   NOTIFICAR_SOPORTE_EMAIL=soporte@janotec.com.co
   ```
2. **Preparar**
   ```bash
   pip install -r requirements.txt
   python manage.py migrate
   python manage.py inicializar
   python manage.py collectstatic --noinput
   python manage.py check --deploy
   ```
3. **Servir con Gunicorn** (servicio systemd):
   ```
   gunicorn config.wsgi:application --bind 127.0.0.1:8001 --workers 3
   ```
4. **Nginx** delante, con HTTPS (Let's Encrypt / certbot):
   ```nginx
   server {
       server_name soporte.janotec.com.co;
       client_max_body_size 50M;
       location /static/ { alias /ruta/janotec_tickets/staticfiles/; }
       # /media/ NO se publica: los adjuntos se sirven por Django con control de acceso
       location / {
           proxy_pass http://127.0.0.1:8001;
           proxy_set_header Host $host;
           proxy_set_header X-Forwarded-Proto $scheme;
           proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       }
   }
   ```
5. **Cierre automático de resueltos**: programe cada hora
   ```
   0 * * * *  cd /ruta/janotec_tickets && .venv/bin/python manage.py cerrar_resueltos
   ```
   (En Windows: Programador de tareas, acción `.venv\Scripts\python.exe manage.py cerrar_resueltos`.)
   Además, la plataforma revisa sola cada 10 minutos cuando alguien la usa, como respaldo.
6. **Copias de seguridad**: base de datos (`mysqldump` diario) **y** la carpeta `media/` (adjuntos).

---

## Estructura

```
config/            settings (lee .env), urls
accounts/          Usuario (rol), Empresa, Sede
tickets/
  models.py        Ticket, Seguimiento, Adjunto, Categoria, ContadorRadicado
  permisos.py      quién ve qué (toda la seguridad de acceso está aquí)
  views.py         portal cliente, panel soporte, descargas, reportes
  forms.py         formularios y validación de adjuntos
  notificaciones.py correos
  management/commands/  inicializar, datos_demo
  tests.py
templates/         base, login, portal cliente, panel soporte
static/            css propio + Bootstrap e íconos locales
```

## Actualizar el servidor

Los cambios se hacen y se prueban en el equipo de desarrollo, se suben a GitHub y el servidor los baja:

```bash
cd /ruta/de/la/plataforma && ./actualizar.sh
```

El script baja lo último de GitHub, instala dependencias, aplica migraciones, prepara los estilos y reinicia. En el servidor solo se edita a mano el `.env`.

## Ideas para siguientes versiones
- Tiempos de respuesta (SLA) por prioridad con alertas de vencimiento.
- Notificaciones por WhatsApp (API de Meta / Twilio).
- Firma del cliente y acta de servicio en PDF al cerrar una visita.
- Exportar reportes a Excel.
