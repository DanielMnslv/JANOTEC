"""Cierre automático de tickets resueltos que el cliente no confirmó ni reabrió.

Se ejecuta de dos formas, para que nunca dependa de una sola:
- ``python manage.py cerrar_resueltos``, programado en el servidor (cron / Programador de tareas).
- ``revisar_si_toca()``, que las vistas principales llaman como red de seguridad
  (como máximo una vez cada 10 minutos por proceso).
"""
from django.core.cache import cache
from django.utils import timezone

from . import notificaciones
from .models import Ticket

INTERVALO_SEGUNDOS = 600


def cerrar_vencidos():
    """Cierra los tickets resueltos que vencieron sin respuesta del cliente y le avisa a cada cliente.

    Devuelve la lista de tickets cerrados.
    """
    cerrados = Ticket.cerrar_resueltos_vencidos()
    for ticket, seguimiento in cerrados:
        notificaciones.actualizacion_para_cliente(ticket, seguimiento)
    return [t for t, _ in cerrados]


def revisar_si_toca():
    # cache.add solo guarda si la clave no existe: así se ejecuta una vez por intervalo
    """Ejecuta el cierre automático si no se ha hecho en los últimos 10 minutos (red de seguridad del cron)."""
    if cache.add("tickets:cierre_automatico", True, INTERVALO_SEGUNDOS):
        cerrar_vencidos()


# --- Cierre y finalización por el área técnica, sin propuesta -----------------------------------------
# Hay solicitudes que pasaron (o iban a pasar) por comercial, pero al final el área técnica lo resolvió todo:
# no hace falta propuesta. Coordinación las cierra desde «Direccionar y gestionar» con el motivo.
MINIMO_MOTIVO = 10


def puede_finalizar(usuario, ticket):
    """Coordinación técnica (o dirección) cierra cualquier ticket abierto que no esté ya en una instalación aprobada."""
    return usuario.es_supervisor and ticket.abierto


def finalizar_sin_propuesta(ticket, autor, motivo, avisar_cliente=True):
    """Cierra el ticket como atendido por el área técnica, sin propuesta.

    - Si tenía ficha comercial aún sin propuesta aprobada, la ficha queda «Cancelada» (ya no se cotiza).
    - El ticket vuelve al área técnica y queda «Cerrado»; las devoluciones pendientes se dan por resueltas.
    - El motivo queda como nota interna; al cliente le llega el cierre (si se pide) con un texto general.
    Devuelve el seguimiento del cierre.
    """
    from django.db import transaction

    from .models import Devolucion, Seguimiento

    motivo = " ".join((motivo or "").split())
    E = Ticket.Estado
    with transaction.atomic():
        ficha = getattr(ticket, "ficha", None)
        if ficha is not None:
            Etapa = type(ficha).Etapa
            if ficha.etapa not in (Etapa.APROBADA, Etapa.INSTALACION, Etapa.CANCELADA, Etapa.RECHAZADA):
                ficha.etapa = Etapa.CANCELADA
                ficha.save(update_fields=["etapa"])
        if ticket.area != Ticket.Area.TECNICA:
            ticket.area = Ticket.Area.TECNICA
            ticket.save(update_fields=["area", "actualizado"])
        Seguimiento.objects.create(
            ticket=ticket, autor=autor, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
            mensaje=f"Cerrado y finalizado por el área técnica, sin propuesta. Motivo: {motivo}")
        Devolucion.objects.filter(ticket=ticket, resuelta__isnull=True).update(
            resuelta=timezone.now(), resuelta_por=autor, nota_resuelta="El área técnica cerró el ticket sin propuesta.")
        seguimiento = ticket.cambiar_estado(
            E.CERRADO, autor, "El área técnica atendió y finalizó su solicitud.", interno=not avisar_cliente)
    if avisar_cliente:
        notificaciones.actualizacion_para_cliente(ticket, seguimiento)
    return seguimiento
