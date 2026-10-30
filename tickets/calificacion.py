"""Calificación de la atención (1 a 10) desde el correo de cierre, sin usuario ni contraseña.

El correo trae 10 estrellas: al tocar una, la nota queda registrada de inmediato y el cliente ve el
agradecimiento (puede dejar un comentario si quiere). La página envía la nota sola con JavaScript: así
los antivirus del correo, que abren los enlaces sin ejecutar la página, no califican por el cliente.
"""
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import Seguimiento, Ticket

SAL = "tickets.calificar"
VIGENCIA = 90 * 24 * 3600  # el enlace del correo sirve tres meses
ESCALA = Ticket.ESCALA_CALIFICACION


def enlace(ticket, nota=None):
    firma = signing.dumps(ticket.radicado, salt=SAL)
    url = f"{settings.SITIO_URL.rstrip('/')}{reverse('calificar_correo', args=[firma])}"
    return f"{url}?n={nota}" if nota else url


def registrar(ticket, nota, comentario="", autor=None):
    """Guarda la calificación (una sola vez) y la deja en el historial del ticket."""
    with transaction.atomic():
        ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
        if ticket.calificacion:
            return False
        ticket.calificacion = nota
        ticket.comentario_calificacion = comentario.strip()[:1000]
        ticket.fecha_calificacion = timezone.now()
        ticket.save(update_fields=["calificacion", "comentario_calificacion", "fecha_calificacion", "actualizado"])
        Seguimiento.objects.create(
            ticket=ticket, autor=autor, tipo=Seguimiento.Tipo.CALIFICACION,
            mensaje=f"Calificación del cliente: {nota}/{ESCALA}"
            + (f" — {ticket.comentario_calificacion}" if ticket.comentario_calificacion else ""),
        )
    return True


def agregar_comentario(ticket, texto):
    """Comentario que el cliente deja después de calificar (una vez)."""
    texto = texto.strip()[:1000]
    if not texto or not ticket.calificacion or ticket.comentario_calificacion:
        return False
    ticket.comentario_calificacion = texto
    ticket.save(update_fields=["comentario_calificacion", "actualizado"])
    Seguimiento.objects.create(ticket=ticket, tipo=Seguimiento.Tipo.CALIFICACION,
                               mensaje=f"Comentario del cliente sobre la atención: {texto}")
    return True


def _nota(valor):
    try:
        n = int(valor)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= ESCALA else None


@require_http_methods(["GET", "POST"])
def calificar_correo(request, firma):
    try:
        radicado = signing.loads(firma, salt=SAL, max_age=VIGENCIA)
    except signing.BadSignature:
        messages.error(request, "El enlace para calificar venció o no es válido. Puede responder el correo con su calificación.")
        return redirect("consulta")
    ticket = Ticket.objects.filter(radicado=radicado).select_related("empresa", "sede").first()
    if ticket is None:
        raise Http404
    error = ""
    nota = _nota(request.POST.get("calificacion") if request.method == "POST" else request.GET.get("n"))
    if request.method == "POST":
        if ticket.calificacion:
            agregar_comentario(ticket, request.POST.get("comentario", ""))
            return redirect(request.path)
        if ticket.estado == Ticket.Estado.CERRADO:
            if nota is None:
                error = "Elija de 1 a 10 estrellas."
            else:
                registrar(ticket, nota, request.POST.get("comentario", ""))
                return redirect(request.path)
    ticket.refresh_from_db(fields=["calificacion", "comentario_calificacion", "estado"])
    # Llegó desde una estrella del correo: la página registra esa nota sola, sin que el cliente haga nada más
    automatica = request.method == "GET" and nota is not None and not ticket.calificacion and ticket.estado == Ticket.Estado.CERRADO
    return render(request, "tickets/calificar.html", {
        "ticket": ticket, "nota": nota, "error": error, "escala": range(ESCALA, 0, -1), "automatica": automatica,
    })
