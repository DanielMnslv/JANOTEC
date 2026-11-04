"""Modo de pruebas del correo: lo que iba para un cliente llega a un buzón de pruebas.

Con CORREO_CLIENTES_PRUEBA en el .env, todo destinatario que no sea del personal JANOTEC (ni un buzón
NOTIFICAR_*) se reemplaza por ese correo, y el asunto dice a quién iba. El personal recibe normal.

Los correos del cliente PRUEBA no le llegan a nadie más que a quienes prueban: lo de comercial y bodega a
CORREO_COMERCIAL_PRUEBA, y lo demás (coordinación, técnicos, dirección, cliente) a CORREO_CLIENTES_PRUEBA.
"""
import re

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.db.models import Q


def correos_internos():
    from accounts.models import Usuario

    internos = {e.lower() for e in Usuario.objects.exclude(email="").exclude(rol=Usuario.Rol.CLIENTE, is_superuser=False)
                .values_list("email", flat=True)}
    for nombre in dir(settings):
        if nombre.startswith("NOTIFICAR_") and isinstance(getattr(settings, nombre), str):
            internos |= {c.strip().lower() for c in getattr(settings, nombre).replace(",", ";").split(";") if c.strip()}
    return internos


RADICADO = re.compile(r"JNT-\d{4}-\d{6}")
MARCA_PRUEBA = re.compile(r"\bPRUEBA\b")


def es_de_prueba(mensaje):
    """Si nombra radicados, todos deben ser del cliente PRUEBA; si no nombra ninguno, lo dice el asunto."""
    from tickets.models import Ticket

    textos = [mensaje.subject, mensaje.body] + [a[0] for a in getattr(mensaje, "alternatives", []) if isinstance(a[0], str)]
    radicados = set(RADICADO.findall(" ".join(textos)))
    if not radicados:
        return bool(MARCA_PRUEBA.search(mensaje.subject))
    empresas = list(Ticket.objects.filter(radicado__in=radicados).values_list("empresa__razon_social", flat=True))
    return len(empresas) == len(radicados) and all(MARCA_PRUEBA.match(e or "") for e in empresas)


def correos_comercial_y_bodega():
    from accounts.models import Usuario

    correos = {e.lower() for e in Usuario.objects.exclude(email="").filter(
        Q(rol__in=[Usuario.Rol.BODEGA, Usuario.Rol.COMERCIAL, Usuario.Rol.ADMINISTRATIVO]) | Q(tambien_comercial=True)
        | Q(tambien_administrativo=True)).values_list("email", flat=True)}
    for buzon in (settings.NOTIFICAR_COMERCIAL_EMAIL, settings.NOTIFICAR_ADMINISTRATIVA_EMAIL):
        correos |= {c.strip().lower() for c in buzon.replace(",", ";").split(";") if c.strip()}
    return correos


def a_quienes_prueban(mensaje):
    """Cambia los destinatarios por quienes prueban; el asunto conserva a quién iba."""
    prueba = settings.CORREO_CLIENTES_PRUEBA
    comercial = settings.CORREO_COMERCIAL_PRUEBA or prueba
    de_comercial = correos_comercial_y_bodega()
    originales = list(dict.fromkeys(list(mensaje.to or []) + list(mensaje.cc or []) + list(mensaje.bcc or [])))
    destinos = {comercial if d.split("<")[-1].strip(" >").lower() in de_comercial else prueba for d in originales}
    mensaje.to, mensaje.cc, mensaje.bcc = sorted(destinos), [], []
    mensaje.subject = f"[PRUEBA · iba para: {', '.join(originales)}] {mensaje.subject}"


class RedirigirClientesBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self.real = get_connection(settings.EMAIL_BACKEND_REAL, fail_silently=fail_silently, **kwargs)

    def open(self):
        return self.real.open()

    def close(self):
        return self.real.close()

    def send_messages(self, mensajes):
        prueba = settings.CORREO_CLIENTES_PRUEBA
        internos = correos_internos() | {prueba.lower()}
        for m in mensajes:
            if es_de_prueba(m):
                a_quienes_prueban(m)
                continue
            externos = []
            for campo in ("to", "cc", "bcc"):
                lista = getattr(m, campo) or []
                fuera = [d for d in lista if d.split("<")[-1].strip(" >").lower() not in internos]
                externos += fuera
                setattr(m, campo, [d for d in lista if d not in fuera])
            if externos:
                m.to = list(m.to) + [prueba]
                m.subject = f"[PRUEBA · iba para: {', '.join(externos)}] {m.subject}"
        return self.real.send_messages(mensajes)


class TodoAPruebasBackend(RedirigirClientesBackend):
    """Para simulaciones: TODO correo (también los del personal) llega solo al buzón de pruebas."""

    def send_messages(self, mensajes):
        prueba = settings.CORREO_CLIENTES_PRUEBA
        for m in mensajes:
            originales = list(m.to or []) + list(m.cc or []) + list(m.bcc or [])
            m.to, m.cc, m.bcc = [prueba], [], []
            m.subject = f"[PRUEBA · iba para: {', '.join(dict.fromkeys(originales))}] {m.subject}"
        return self.real.send_messages(mensajes)
