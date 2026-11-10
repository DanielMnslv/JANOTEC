"""Filtros y etiquetas propios para las plantillas: fechas al estilo del correo y casos de «Salud de los procesos»."""

from datetime import timedelta

from django import template
from django.utils import formats, timezone

register = template.Library()


@register.filter
def fecha_correo(valor):
    """Fecha como en la bandeja de un correo: «6:34 p. m.» hoy, «Ayer», «21 oct» este año, «21/10/2025» antes."""
    if not valor:
        return ""
    local = timezone.localtime(valor)
    hoy = timezone.localdate()
    if local.date() == hoy:
        return formats.date_format(local, "g:i a")
    if local.date() == hoy - timedelta(days=1):
        return "Ayer"
    if local.year == hoy.year:
        return formats.date_format(local, "j M").lower()
    return formats.date_format(local, "d/m/Y")


@register.simple_tag
def casos_area(usuario, *areas):
    """Los casos de «Salud de los procesos» de esas áreas que le tocan a quien ve la bandeja (sin revisar)."""
    from tickets import diagnostico
    return diagnostico.casos_para(usuario, areas or None)


@register.simple_tag
def siguiente_paso(ticket, usuario):
    """El siguiente paso del ticket para la columna «Siguiente paso» (ver ``pasos.siguiente``)."""
    from tickets import pasos
    return pasos.siguiente(ticket, usuario)


@register.simple_tag
def siguiente_paso_comercial(ficha, usuario):
    """El siguiente paso del requerimiento comercial (ver ``comercial.pasos.siguiente``)."""
    from comercial import pasos
    return pasos.siguiente(ficha, usuario)


@register.inclusion_tag("tickets/_devolucion.html", takes_context=True)
def devolucion_ticket(context, ticket):
    """Recuadro «Devolver al área anterior» y avisos de devoluciones pendientes (ver ``tickets.devoluciones``)."""
    from tickets import devoluciones

    request = context.get("request")
    usuario = getattr(request, "user", None)
    if not ticket or not usuario or not usuario.is_authenticated or not usuario.es_interno:
        return {"mostrar": False}
    pendientes = [(d, devoluciones.INFO[d.tramo], devoluciones.puede_resolver(usuario, d)) for d in devoluciones.pendientes(ticket)]
    return {"mostrar": True, "ticket": ticket, "pendientes": pendientes, "opciones": devoluciones.opciones(ticket, usuario),
            "volver": request.get_full_path(), "csrf_token": context.get("csrf_token")}
