"""Envía un correo de prueba con la configuración del .env y dice exactamente qué falló, si falla.

    python manage.py probar_correo alguien@janotec.app
"""
from django.conf import settings
from django.core.mail import get_connection, send_mail
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Envía un correo de prueba para verificar el servidor de correo (SMTP)."

    def add_arguments(self, parser):
        parser.add_argument("destino", help="Correo que recibe la prueba")

    def handle(self, destino, **opciones):
        self.stdout.write(
            f"Servidor: {settings.EMAIL_HOST}:{settings.EMAIL_PORT} · SSL={settings.EMAIL_USE_SSL} · TLS={settings.EMAIL_USE_TLS}\n"
            f"Usuario: {settings.EMAIL_HOST_USER or '(vacío)'} · Remitente: {settings.DEFAULT_FROM_EMAIL}\n"
            f"Backend: {settings.EMAIL_BACKEND} → {getattr(settings, 'EMAIL_BACKEND_REAL', '')} · Modo: {settings.CORREO_MODO}"
        )
        if "console" in getattr(settings, "EMAIL_BACKEND_REAL", settings.EMAIL_BACKEND):
            self.stdout.write(self.style.WARNING(
                "EMAIL_BACKEND es de consola: el correo se imprime aquí y NO sale. "
                "Ponga EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend en el .env."))
        if settings.EMAIL_USE_SSL and settings.EMAIL_USE_TLS:
            raise CommandError("EMAIL_USE_SSL y EMAIL_USE_TLS no pueden estar los dos en True (con el puerto 465 use solo SSL).")
        try:
            conexion = get_connection()
            conexion.open()
            send_mail("Prueba de correo · Plataforma JANOTEC",
                      "Este es un correo de prueba de la plataforma JANOTEC.\n"
                      "Si lo recibe, las notificaciones (tickets, cronograma, actas) ya pueden salir.",
                      settings.DEFAULT_FROM_EMAIL, [destino], connection=conexion)
            conexion.close()
        except Exception as e:  # noqa: BLE001 — se muestra el motivo exacto
            raise CommandError(f"No se pudo enviar: {type(e).__name__}: {e}") from e
        if "console" in getattr(settings, "EMAIL_BACKEND_REAL", settings.EMAIL_BACKEND):
            raise CommandError("El correo NO salió: el .env sigue con el backend de consola (solo se imprime arriba).")
        if settings.CORREO_MODO == "todo":
            destino = settings.CORREO_CLIENTES_PRUEBA + f" (modo «todo»: iba para {destino})"
        self.stdout.write(self.style.SUCCESS(f"Enviado a {destino}. Revise la bandeja de entrada (y la de spam)."))
