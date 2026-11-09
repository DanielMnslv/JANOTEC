"""Foto del estado de una instalación, para comparar el equipo local con el servidor de producción.

    python manage.py estado_plataforma

Muestra la versión del código, las migraciones pendientes, la configuración que no es secreta,
las variables del .env que faltan, las librerías, cuántos registros hay de cada cosa y cuántos
archivos hay en media. Nunca muestra contraseñas ni claves: de las variables secretas solo dice
si están puestas o no.
"""
import importlib
import os
import subprocess
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

# Apps propias de la plataforma: de estas se cuentan los registros.
APPS_PROPIAS = ("accounts", "tickets", "comercial", "cronograma", "inventario")

# Variables del .env que la plataforma lee (las de .env.example más las que se agregaron después).
VARIABLES = (
    "DJANGO_SECRET_KEY", "DJANGO_DEBUG", "DJANGO_ALLOWED_HOSTS", "DJANGO_CSRF_TRUSTED_ORIGINS",
    "DB_ENGINE", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT",
    "EMAIL_BACKEND", "EMAIL_HOST", "EMAIL_PORT", "EMAIL_USE_SSL", "EMAIL_USE_TLS",
    "EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD", "DEFAULT_FROM_EMAIL",
    "NOTIFICAR_SOPORTE_EMAIL", "SITIO_URL", "RADICADO_PREFIJO", "ADJUNTO_MAX_MB", "ADJUNTO_EXTENSIONES",
    "CORREO_MODO", "CORREO_CLIENTES_PRUEBA", "CORREO_COMERCIAL_PRUEBA",
)
# De estas solo se dice si están puestas; su valor nunca se imprime.
SECRETAS = ("DJANGO_SECRET_KEY", "DB_PASSWORD", "EMAIL_HOST_PASSWORD", "DB_USER", "EMAIL_HOST_USER")

LIBRERIAS = ("django", "MySQLdb", "dotenv", "openpyxl", "reportlab")


class Command(BaseCommand):
    help = "Muestra el estado de esta instalación (código, migraciones, .env, librerías, datos y archivos) sin secretos."

    def handle(self, *args, **opciones):
        base = Path(settings.BASE_DIR)
        self._titulo("Código")
        self.stdout.write(f"  Último commit: {self._git(base)}")

        self._titulo("Migraciones")
        ejecutor = MigrationExecutor(connection)
        pendientes = ejecutor.migration_plan(ejecutor.loader.graph.leaf_nodes())
        if pendientes:
            self.stdout.write(self.style.ERROR(f"  {len(pendientes)} pendiente(s): " + ", ".join(f"{m.app_label}.{m.name}" for m, _ in pendientes)))
        else:
            self.stdout.write(self.style.SUCCESS("  Todas aplicadas"))

        self._titulo("Configuración (.env)")
        for nombre in VARIABLES:
            valor = os.environ.get(nombre)
            if valor is None:
                self.stdout.write(self.style.WARNING(f"  {nombre}: FALTA"))
            elif nombre in SECRETAS:
                self.stdout.write(f"  {nombre}: puesta")
            else:
                self.stdout.write(f"  {nombre}: {valor}")

        self._titulo("Librerías")
        for nombre in LIBRERIAS:
            try:
                modulo = importlib.import_module(nombre)
                self.stdout.write(f"  {nombre}: {getattr(modulo, '__version__', 'OK')}")
            except ImportError:
                self.stdout.write(self.style.ERROR(f"  {nombre}: NO INSTALADA"))

        self._titulo("Datos (registros por tabla)")
        for etiqueta in APPS_PROPIAS:
            for modelo in apps.get_app_config(etiqueta).get_models():
                self.stdout.write(f"  {etiqueta}.{modelo.__name__}: {modelo.objects.count()}")
        self._ultimo_ticket()

        self._titulo("Archivos (media)")
        media = Path(settings.MEDIA_ROOT)
        if media.exists():
            for carpeta in sorted(p for p in media.iterdir() if p.is_dir()):
                total = sum(1 for f in carpeta.rglob("*") if f.is_file())
                self.stdout.write(f"  {carpeta.name}: {total}")
        else:
            self.stdout.write(self.style.WARNING(f"  No existe {media}"))

    def _titulo(self, texto):
        """Escribe el encabezado de cada bloque."""
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n== {texto} =="))

    def _git(self, base):
        """Devuelve el commit actual (corto, fecha y mensaje) o un aviso si git no responde."""
        try:
            return subprocess.run(
                ["git", "log", "-1", "--format=%h %ci %s"], cwd=base, capture_output=True, text=True, timeout=10,
            ).stdout.strip() or "(sin git)"
        except (OSError, subprocess.SubprocessError):
            return "(sin git)"

    def _ultimo_ticket(self):
        """Muestra el radicado y la fecha del último ticket creado, para saber dónde se está trabajando."""
        from tickets.models import Ticket

        ultimo = Ticket.objects.order_by("-id").first()
        if ultimo:
            self.stdout.write(f"  Último ticket: {ultimo.radicado} ({ultimo.creado:%Y-%m-%d %H:%M})")
