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
