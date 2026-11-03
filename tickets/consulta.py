"""Consulta sin usuario (como en janotecsas.netlify.app): radicado + teléfono, o el enlace firmado del correo.

Solo muestra lo que el cliente ya puede ver: etapas, fechas estimadas, notas y la visita programada.
"""
import re

from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from . import etapas
from .models import Ticket

SAL = "tickets.consulta"
VIGENCIA = 180 * 24 * 3600  # el enlace del correo sirve seis meses
INTENTOS_MAX = 10
VENTANA = 15 * 60


def _digitos(texto):
    """Solo los dígitos de un teléfono, sin el indicativo 57 de Colombia."""
    d = re.sub(r"\D", "", texto or "")
    return d[2:] if len(d) == 12 and d.startswith("57") else d


def telefono_coincide(ticket, telefono):
    """El teléfono escrito debe ser el de contacto del ticket (sin espacios, guiones ni +57)."""
    escrito = _digitos(telefono)
    if len(escrito) < 7:
        return False
    registrados = [_digitos(ticket.contacto_telefono), _digitos(getattr(ticket.solicitante, "telefono", ""))]
    return any(r and (r == escrito or r.endswith(escrito[-10:]) and len(escrito) >= 10) for r in registrados)


def enlace_consulta(ticket):
    """Enlace firmado del correo para que el cliente vea su solicitud sin usuario ni contraseña."""
    firma = signing.dumps(ticket.radicado, salt=SAL)
    return f"{settings.SITIO_URL.rstrip('/')}{reverse('consulta_token', args=[firma])}"


def _ip(request):
    """Dirección IP de quien consulta (para limitar los intentos)."""
    return request.META.get("REMOTE_ADDR", "")


def _clave_intentos(request):
    """Clave de caché con los intentos fallidos de consulta desde esa IP."""
    return f"consulta-intentos:{_ip(request)}"


@require_http_methods(["GET", "POST"])
def consulta(request):
    """Consulta pública con radicado y teléfono de contacto. Tras 10 intentos fallidos en 15 minutos se bloquea
    esa IP un rato; si coincide, lleva al enlace firmado de la solicitud.
    """
    if request.method == "POST":
        clave = _clave_intentos(request)
        intentos = cache.get(clave, 0)
        if intentos >= INTENTOS_MAX:
            messages.error(request, "Demasiados intentos. Espere 15 minutos e intente de nuevo.")
            return render(request, "tickets/consulta.html", status=429)
        radicado = request.POST.get("radicado", "").strip().upper()
        ticket = Ticket.objects.filter(radicado=radicado).select_related("solicitante").first()
        if ticket and telefono_coincide(ticket, request.POST.get("telefono", "")):
            cache.delete(clave)
            return redirect("consulta_token", firma=signing.dumps(ticket.radicado, salt=SAL))
        cache.set(clave, intentos + 1, VENTANA)
        messages.error(request, "No encontramos una solicitud con ese radicado y teléfono de contacto.")
        return render(request, "tickets/consulta.html", {"radicado": radicado})
    return render(request, "tickets/consulta.html")


def consulta_token(request, firma):
    """Lo que ve el cliente de su solicitud con el enlace firmado: etapas, fechas y las últimas notas visibles."""
    try:
        radicado = signing.loads(firma, salt=SAL, max_age=VIGENCIA)
    except signing.BadSignature:
        messages.error(request, "El enlace de consulta venció o no es válido. Consulte con su radicado y teléfono.")
        return redirect("consulta")
    ticket = Ticket.objects.filter(radicado=radicado).select_related("empresa", "sede", "categoria").first()
    if ticket is None:
        raise Http404
    visibles = ticket.seguimientos.filter(interno=False).order_by("-creado")[:5]
    return render(request, "tickets/consulta_resultado.html", {
        "ticket": ticket, "etapas": etapas.etapas_cliente(ticket), "visibles": visibles,
    })
