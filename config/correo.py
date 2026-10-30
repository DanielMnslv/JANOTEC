"""Modo de pruebas del correo: lo que iba para un cliente llega a un buzón de pruebas.

Con CORREO_CLIENTES_PRUEBA en el .env, todo destinatario que no sea del personal JANOTEC (ni un buzón
NOTIFICAR_*) se reemplaza por ese correo, y el asunto dice a quién iba. El personal recibe normal.
"""
from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend


def correos_internos():
    from accounts.models import Usuario

    internos = {e.lower() for e in Usuario.objects.exclude(email="").exclude(rol=Usuario.Rol.CLIENTE, is_superuser=False)
                .values_list("email", flat=True)}
    for nombre in dir(settings):
        if nombre.startswith("NOTIFICAR_") and isinstance(getattr(settings, nombre), str):
            internos |= {c.strip().lower() for c in getattr(settings, nombre).replace(",", ";").split(";") if c.strip()}
    return internos


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
