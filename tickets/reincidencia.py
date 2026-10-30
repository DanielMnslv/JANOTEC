"""Aviso de servicio repetido: al abrir un ticket de soporte, el personal ve si esa sede ya tuvo soporte del mismo sistema
hace poco. Las visitas de mantenimiento preventivo no cuentan: son programadas, no una falla que volvió."""
import re
from datetime import timedelta

from .models import ActaSoporte, Ticket

DIAS = 30
SISTEMA_POR_SERVICIO = {
    "Mantenimiento CCTV IP": "CCTV", "Instalación CCTV IP": "CCTV", "Alarma de intrusión": "ALARMA",
    "Monitoreo 24/7": "ALARMA", "Control de acceso": "CONTROL DE ACCESO", "Fibra óptica / red": "REDES",
    "Energía fotovoltaica": "ENERGIA SOLAR", "Radiocomunicaciones": "RADIO",
}
PREVENTIVO = re.compile(r"preventiv|peri[oó]dic", re.I)


def visitas_previas(ticket):
    """Actas de soporte de la misma sede y sistema en los 30 días anteriores al ticket. Sin sede no se puede comparar."""
    if ticket.tipo != Ticket.Tipo.SOPORTE or not ticket.sede_id:
        return []
    actas = ActaSoporte.objects.filter(sede=ticket.sede, actividad__icontains="Soporte", fecha__lt=ticket.creado,
                                       fecha__gte=ticket.creado - timedelta(days=DIAS)).exclude(ticket=ticket)
    sistema = SISTEMA_POR_SERVICIO.get(ticket.categoria.nombre)
    if sistema:  # «Otro» o «Diagnóstico técnico» no dicen el sistema: se muestra todo lo de la sede
        actas = actas.filter(sistema=sistema)
    return [a for a in actas.select_related("tecnico").order_by("-fecha") if not PREVENTIVO.search(a.diagnostico)]
