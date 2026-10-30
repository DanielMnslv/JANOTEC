"""Contratos de monitoreo: totales por cliente y el ajuste anual del valor mensual."""
from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction

from .models import AjusteMonitoreo, ServicioMonitoreo

DOS = Decimal("0.01")


def por_empresa(servicios):
    """[{empresa, servicios, total}] con el total mensual de los servicios activos de cada cliente."""
    grupos = {}
    for s in servicios:
        g = grupos.setdefault(s.sede.empresa_id, {"empresa": s.sede.empresa, "servicios": [], "total": Decimal(0), "activos": 0})
        g["servicios"].append(s)
        if s.activo:
            g["total"] += s.total_mensual
            g["activos"] += 1
    return list(grupos.values())


@transaction.atomic
def aplicar_ajuste(empresa, anio, porcentaje, usuario, nota=""):
    """Sube el valor de los servicios activos del cliente que aún tienen el valor de un año anterior.

    Devuelve cuántos se ajustaron; los que ya están en ese año no se tocan (así no se ajusta dos veces).
    """
    porcentaje = Decimal(porcentaje)
    hechos = 0
    for s in ServicioMonitoreo.objects.select_for_update().filter(sede__empresa=empresa, activo=True, anio_valor__lt=anio):
        nuevo = (s.valor_mensual * (1 + porcentaje / 100)).quantize(DOS, ROUND_HALF_UP)
        AjusteMonitoreo.objects.create(servicio=s, anio=anio, porcentaje=porcentaje, valor_anterior=s.valor_mensual,
                                       valor_nuevo=nuevo, nota=nota, aplicado_por=usuario)
        s.valor_mensual, s.anio_valor = nuevo, anio
        s.save(update_fields=["valor_mensual", "anio_valor", "actualizado"])
        hechos += 1
    return hechos
