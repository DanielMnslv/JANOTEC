"""Alerta semanal de reposición: los lunes (o la primera vez que alguien entra a la plataforma esa semana) bodega recibe
la lista de insumos que quedaron por debajo del stock mínimo, contando lo libre (sin lo apartado para tickets)."""
import logging
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

log = logging.getLogger(__name__)
CERO = Decimal("0")


def por_reponer():
    """[{insumo, bodega, apartado, libre, minimo}] de los insumos que quedaron por debajo de su stock mínimo."""
    from . import servicios
    from .models import Insumo
    from .solicitudes import total_apartado

    saldos = servicios.saldos_insumos()
    apartado = total_apartado()
    filas = []
    for i in Insumo.objects.filter(activo=True, stock_minimo__gt=0).order_by("descripcion"):
        bodega = saldos[i.pk]["bodega"] if i.pk in saldos else CERO
        libre = bodega - apartado.get(i.codigo, CERO)
        if libre < i.stock_minimo:
            filas.append({"insumo": i, "bodega": bodega, "apartado": apartado.get(i.codigo, CERO), "libre": libre,
                          "minimo": i.stock_minimo})
    return filas


def modelos_por_reponer():
    """[{referencia, descripcion, bodega, minimo}] de los modelos de equipo con mínimo que ya llegaron a él (en bodega)."""
    from .productos import filas_equipos
    filas, _ = filas_equipos()
    return [f for f in filas if f["minimo"] and f["bodega"] < f["minimo"]]


def enviar(forzar=False):
    """Envía la alerta de la semana (una sola vez por semana, desde el lunes). Devuelve cuántos insumos reportó."""
    from accounts.models import Usuario
    from tickets import notificaciones as notif

    from .models import AvisoSemanal

    hoy = timezone.localdate()
    semana = f"stock-{hoy.isocalendar()[0]}-{hoy.isocalendar()[1]:02d}"
    if not forzar and AvisoSemanal.objects.filter(clave=semana).exists():
        return 0
    filas = por_reponer()
    modelos = modelos_por_reponer()
    AvisoSemanal.objects.update_or_create(clave=semana, defaults={"cantidad": len(filas) + len(modelos)})
    if not filas and not modelos:
        return 0
    destinos = list(Usuario.objects.filter(is_active=True, rol="bodega").exclude(email="").values_list("email", flat=True))
    if not destinos:
        return 0

    def n(v):
        return format(Decimal(v).normalize(), "f")

    lineas = [f"Esta semana hay {len(filas)} insumo(s) por debajo del stock mínimo (sin contar lo apartado para tickets):", ""] if filas else []
    lineas += [f"- {f['insumo'].codigo} · {f['insumo'].descripcion}: libres {n(max(f['libre'], CERO))} "
               f"(en bodega {n(f['bodega'])}, apartado {n(f['apartado'])}) · mínimo {n(f['minimo'])}" for f in filas]
    if modelos:
        lineas += ["", f"Equipos por debajo del stock mínimo ({len(modelos)}):"]
        lineas += [f"- {m['codigo']} · {m['descripcion'][:70]}: en bodega {m['bodega']} · mínimo {m['minimo']}" for m in modelos]
    lineas += ["", "Cotice y compre lo necesario; al registrar la compra en el inventario la alerta se actualiza.",
               f"Ver lo que está por reponer: {settings.SITIO_URL.rstrip('/')}/inventario/productos/?bajos=1"]
    notif._enviar(f"Inventario por reponer esta semana ({len(filas) + len(modelos)})", "\n".join(lineas), destinos)
    return len(filas) + len(modelos)


def revisar_si_toca():
    """Desde las vistas: el lunes o después, si esta semana aún no se envió. Nunca interrumpe a quien está trabajando."""
    try:
        enviar()
    except Exception:  # noqa: BLE001
        log.exception("No se pudo enviar la alerta semanal de stock mínimo")
