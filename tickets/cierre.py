"""Cierre automático de tickets resueltos que el cliente no confirmó ni reabrió.

Se ejecuta de dos formas, para que nunca dependa de una sola:
- ``python manage.py cerrar_resueltos``, programado en el servidor (cron / Programador de tareas).
- ``revisar_si_toca()``, que las vistas principales llaman como red de seguridad
  (como máximo una vez cada 10 minutos por proceso).
"""
from django.core.cache import cache

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
