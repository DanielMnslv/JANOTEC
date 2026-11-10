"""Envía a cada usuario un enlace personal para que cree su propia contraseña.

Las contraseñas iniciales se asignaron al azar; con este correo cada persona elige la suya. El enlace es el mismo
de «¿Olvidó su contraseña?»: personal, de un solo uso y válido por PASSWORD_RESET_TIMEOUT (7 días). Nadie, ni el
administrador, ve la contraseña que la persona elija.

    python manage.py invitar_cambio_clave                    # solo muestra a quién le llegaría (no envía nada)
    python manage.py invitar_cambio_clave --enviar           # envía al personal de JANOTEC
    python manage.py invitar_cambio_clave --con-clientes     # incluye a los usuarios de las empresas cliente
    python manage.py invitar_cambio_clave --solo jguana mtibamoso --enviar

Nunca le llega al superusuario ni a los usuarios de prueba (con «prueba» en el usuario), ni a usuarios
inactivos o sin correo.
"""
from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from accounts.models import Usuario
from accounts.prueba import PREFIJO

ASUNTO = "Cree su contraseña personal en la plataforma JANOTEC"


def enlace(usuario):
    """El enlace personal para crear la contraseña (el mismo de «¿Olvidó su contraseña?»)."""
    ruta = reverse("password_reset_confirm", kwargs={"uidb64": urlsafe_base64_encode(force_bytes(usuario.pk)),
                                                     "token": default_token_generator.make_token(usuario)})
    return settings.SITIO_URL.rstrip("/") + ruta


def cuerpo(usuario):
    """El texto del correo; el diseño HTML lo agrega config.correo al enviarlo."""
    dias = settings.PASSWORD_RESET_TIMEOUT // 86400
    sitio = settings.SITIO_URL.rstrip("/")
    return "\n".join([
        f"Hola {usuario.get_full_name() or usuario.username},",
        "",
        "Por seguridad, cada usuario de la plataforma de soporte JANOTEC debe tener una contraseña personal que solo "
        "usted conozca. La contraseña que tiene hoy fue asignada de forma provisional.",
        "",
        f"Su usuario: {usuario.username}",
        "",
        f"Cree su nueva contraseña en este enlace (es personal, sirve una sola vez y vence en {dias} días):",
        enlace(usuario),
        "",
        "Recomendaciones: mínimo 8 caracteres, que no se parezca a su usuario ni a su nombre y que no sea solo números.",
        "",
        f"Después ingrese en {sitio}{reverse('login')} con su usuario y la nueva contraseña. Si el enlace vence, use "
        "«¿Olvidó su contraseña?» en la pantalla de ingreso.",
        "",
        "Cordialmente,",
    ])


def destinatarios(con_clientes=False, solo=None):
    """Usuarios activos con correo, sin el superusuario ni los de prueba; los clientes solo si se piden."""
    qs = (Usuario.objects.filter(is_active=True, is_superuser=False).exclude(email="").exclude(username="admin")
          .exclude(username__startswith=PREFIJO).exclude(username__icontains="prueba")
          .order_by("rol", "username"))
    if not con_clientes:
        qs = qs.exclude(rol=Usuario.Rol.CLIENTE)
    if solo:
        qs = qs.filter(username__in=solo)
    return list(qs)


class Command(BaseCommand):
    help = "Envía a cada usuario (menos el administrador) el enlace para crear su propia contraseña."

    def add_arguments(self, parser):
        parser.add_argument("--enviar", action="store_true", help="Envía de verdad (sin esto solo muestra la lista).")
        parser.add_argument("--con-clientes", action="store_true", help="Incluye a los usuarios de las empresas cliente.")
        parser.add_argument("--solo", nargs="+", metavar="USUARIO", help="Solo a estos usuarios (por su nombre de usuario).")

    def handle(self, enviar=False, con_clientes=False, solo=None, **opciones):
        usuarios = destinatarios(con_clientes, solo)
        self.stdout.write(f"Modo de correo: {settings.CORREO_MODO} · enlaces válidos {settings.PASSWORD_RESET_TIMEOUT // 86400} días · "
                          f"sitio {settings.SITIO_URL}")
        for u in usuarios:
            self.stdout.write(f"  {u.username:<22} {u.get_rol_display():<28} {u.email}")
        self.stdout.write(f"Total: {len(usuarios)} usuario(s).")
        if not enviar:
            self.stdout.write(self.style.WARNING("No se envió nada. Revise la lista y repita con --enviar."))
            return
        enviados, fallidos = 0, []
        for u in usuarios:
            try:
                send_mail(ASUNTO, cuerpo(u), settings.DEFAULT_FROM_EMAIL, [u.email], fail_silently=False)
                enviados += 1
            except Exception as e:  # noqa: BLE001 — que un correo malo no detenga a los demás
                fallidos.append(f"{u.username} ({u.email}): {e}")
        self.stdout.write(self.style.SUCCESS(f"Enviados: {enviados}."))
        for f in fallidos:
            self.stdout.write(self.style.ERROR(f"No se pudo enviar a {f}"))
