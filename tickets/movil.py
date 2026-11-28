from datetime import timedelta

from django.http import JsonResponse
from django.db.models import Q
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import Ticket
from .permisos import tickets_visibles
from cronograma.models import Actividad


@never_cache
@require_GET
def visitas(request):
    if not request.user.is_authenticated:
        return JsonResponse({"error": "Inicia sesión para consultar tus visitas."}, status=401)
    if not request.user.es_personal:
        return JsonResponse({"error": "Esta cuenta no tiene acceso al área técnica."}, status=403)

    visibles = tickets_visibles(request.user)
    tickets = visibles.filter(estado__in=Ticket.ESTADOS_ABIERTOS).order_by("-actualizado", "-pk")[:100]
    hoy = timezone.localdate()
    lunes = hoy - timedelta(days=hoy.weekday())
    actividades = Actividad.objects.filter(
        encargados=request.user, fecha__range=(lunes, lunes + timedelta(days=6)),
    ).filter(Q(ticket__isnull=True) | Q(ticket_id__in=visibles.values("pk"))).select_related("ticket")
    return JsonResponse({
        "nombre": request.user.get_full_name() or request.user.username,
        "hoy": hoy.isoformat(),
        "cronograma": [{
            "fecha": actividad.fecha.isoformat(),
            "inicio": actividad.hora_inicio.strftime("%H:%M"),
            "fin": actividad.hora_fin.strftime("%H:%M"),
            "cliente": actividad.cliente,
            "sede": actividad.sede,
            "actividad": actividad.actividad,
            "radicado": actividad.ticket.radicado if actividad.ticket else "",
            "insumos": actividad.insumos,
            "autorizacion": actividad.get_autorizacion_display() if actividad.ticket_id else "",
        } for actividad in actividades],
        "visitas": [{
            "radicado": ticket.radicado,
            "asunto": ticket.asunto,
            "cliente": ticket.empresa.razon_social,
            "sede": str(ticket.sede) if ticket.sede else "",
            "direccion": ticket.ubicacion,
            "estado": ticket.get_estado_display(),
            "descripcion": ticket.descripcion,
        } for ticket in tickets],
    })
