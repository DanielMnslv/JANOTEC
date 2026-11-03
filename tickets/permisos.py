from functools import wraps

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from .models import Ticket


def tickets_visibles(usuario):
    """Tickets que un usuario puede ver.

    - Dirección (y superusuario): todos, de las dos áreas.
    - Coordinador del área técnica: los que están en el área técnica (no ve lo que sigue en comercial).
    - Técnico: los que le asignaron o en cuyas visitas del cronograma participa.
    - Área comercial: los que tienen ficha comercial (también los que ya envió al área técnica, para seguirlos)
      y las solicitudes técnicas que radicó para un cliente.
    - Central de atención: las que radicó.
    - Cliente con empresa: todos los de su empresa, de principio a fin.
    """
    from django.db.models import Q

    qs = Ticket.objects.select_related(
        "empresa", "sede", "categoria", "solicitante", "asignado_a", "ficha"
    )
    if usuario.es_direccion:
        return qs
    if usuario.es_supervisor:
        return qs.filter(area=Ticket.Area.TECNICA)
    if usuario.es_personal:
        return qs.filter(
            Q(asignado_a=usuario) | Q(actividades__encargados=usuario)
        ).distinct()
    if usuario.es_comercial:
        return qs.filter(Q(ficha__isnull=False) | Q(radicado_por=usuario) | Q(asignado_a=usuario))
    if usuario.es_central:
        return qs.filter(Q(radicado_por=usuario) | Q(asignado_a=usuario))
    if usuario.es_bodega:
        return qs.filter(asignado_a=usuario)
    if usuario.empresa_id:
        return qs.filter(empresa_id=usuario.empresa_id)
    return qs.filter(solicitante=usuario)


def actas_visibles(usuario):
    """Actas de soporte (historial de servicio) que un usuario puede ver.

    - Área administrativa: todas.
    - Encargado: las que hizo y las de los clientes que tiene asignados (contexto para atenderlos).
    - Cliente: las de su empresa.
    """
    from django.db.models import Q

    from .models import ActaSoporte

    qs = ActaSoporte.objects.select_related("empresa", "sede", "tecnico")
    if usuario.es_supervisor:
        return qs
    if usuario.es_personal:
        return qs.filter(
            Q(tecnico=usuario) | Q(empresa__tickets__asignado_a=usuario)
        ).distinct()
    if usuario.empresa_id:
        return qs.filter(empresa_id=usuario.empresa_id)
    return qs.none()


def obtener_ticket(usuario, radicado):
    """Devuelve el ticket o 404 si no existe o el usuario no puede verlo
    (no se revela si un radicado ajeno existe)."""
    return get_object_or_404(tickets_visibles(usuario), radicado=radicado)


def gestiona_ticket(usuario, ticket):
    """Quién trabaja el ticket desde la vista interna: el personal técnico, o la persona a la que se le asignó."""
    return usuario.es_personal or (usuario.es_interno and ticket.asignado_a_id == usuario.pk)


def personal_requerido(vista):
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not request.user.es_personal:
            raise PermissionDenied
        return vista(request, *args, **kwargs)

    return envoltura


def supervisor_requerido(vista):
    """Solo el área administrativa (supervisor o superusuario)."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not request.user.es_supervisor:
            raise PermissionDenied
        return vista(request, *args, **kwargs)

    return envoltura


def comercial_o_supervisor(vista):
    """Área comercial y área administrativa."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not request.user.ve_comercial:
            raise PermissionDenied
        return vista(request, *args, **kwargs)

    return envoltura


def radicador_requerido(vista):
    """Quien registra solicitudes: clientes, personal JANOTEC, central de atención y comercial."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not (request.user.es_cliente or request.user.radica_para_clientes):
            raise PermissionDenied
        return vista(request, *args, **kwargs)

    return envoltura


def cliente_requerido(vista):
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not request.user.es_cliente:
            raise PermissionDenied
        return vista(request, *args, **kwargs)

    return envoltura
