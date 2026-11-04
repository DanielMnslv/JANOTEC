from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from tickets.permisos import tickets_visibles


def _exige(propiedad, explicacion):
    """Decorador que deja pasar solo a quien tenga la propiedad del usuario (si no, 403)."""
    def decorador(vista):
        """Envuelve la vista con la comprobación del permiso y añade el permiso a su documentación."""
        @wraps(vista)
        def envoltura(request, *args, **kwargs):
            """Responde 403 si el usuario no tiene la propiedad exigida; si la tiene, ejecuta la vista."""
            if not getattr(request.user, propiedad, False):
                raise PermissionDenied
            return vista(request, *args, **kwargs)
        envoltura.__doc__ = (vista.__doc__ or "") + f"\n\n    Permiso: {explicacion}."
        return envoltura
    return decorador


# Entrar a los requerimientos (bandeja y ficha): comercial, administrativa y dirección
comercial_requerido = _exige("ve_comercial", "comercial, administrativa o dirección")
# Radicar, pedir el levantamiento, registrar el informe y traspasar: comercial y dirección
requerimientos_requerido = _exige("gestiona_requerimientos", "comercial o dirección")
# Cotizador y catálogo de precios: administrativa y dirección
propuestas_requerido = _exige("arma_propuestas", "administrativa o dirección")


def obtener_ticket_comercial(usuario, radicado):
    """Ticket con ficha comercial que el usuario puede ver (404 si no)."""
    return get_object_or_404(tickets_visibles(usuario).filter(ficha__isnull=False), radicado=radicado)
