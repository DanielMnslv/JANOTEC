from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from tickets.permisos import tickets_visibles


def comercial_requerido(vista):
    """Área comercial y dirección."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not request.user.ve_comercial:
            raise PermissionDenied
        return vista(request, *args, **kwargs)

    return envoltura


def obtener_ticket_comercial(usuario, radicado):
    """Ticket con ficha comercial que el usuario puede ver (404 si no)."""
    return get_object_or_404(tickets_visibles(usuario).filter(ficha__isnull=False), radicado=radicado)
