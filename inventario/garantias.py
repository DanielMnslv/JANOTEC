"""Garantías y devoluciones al proveedor: el equipo sale de bodega al proveedor y vuelve reparado, cambiado por otro,
se convierte en nota crédito o regresa porque la garantía no aplicó. Cada paso queda en el kardex del equipo."""
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction

from . import servicios
from .models import Activo, GarantiaProveedor, Lugar, MovimientoActivo, Proveedor

G = GarantiaProveedor.Estado
T = MovimientoActivo.Tipo
E = Activo.Estado
DIAS_GARANTIA = 365  # un año de garantía de fábrica, como dicen las propuestas


def vence(activo):
    """Hasta cuándo tiene garantía de fábrica, o None si no se sabe la fecha de compra."""
    return activo.fecha_compra + timedelta(days=DIAS_GARANTIA) if activo.fecha_compra else None


@transaction.atomic
def abrir(activo, motivo, fecha, usuario, proveedor="", radicado="", ticket=None):
    if activo.estado in (E.BAJA, E.VENDIDO):
        raise ValidationError(f"{activo.placa} está {activo.get_estado_display().lower()}: no se puede mandar a garantía.")
    if activo.garantias.filter(estado=G.EN_PROVEEDOR).exists():
        raise ValidationError(f"{activo.placa} ya está en garantía con el proveedor.")
    nombre = (proveedor or activo.proveedor or "").strip()
    prov = Proveedor.objects.get_or_create(razon_social=nombre)[0] if nombre else None
    g = GarantiaProveedor.objects.create(activo=activo, proveedor=prov, factura=activo.factura, motivo=motivo, enviada=fecha,
                                         radicado=radicado, ticket=ticket, creado_por=usuario)
    servicios.mover_activo(activo, T.GARANTIA, Lugar.PROVEEDOR, usuario, estado=E.GARANTIA, fecha=fecha,
                           proveedor=nombre, factura=activo.factura, ticket=ticket,
                           observaciones=f"{g.numero}: {motivo}"[:500])
    return g


@transaction.atomic
def resolver(g, estado, fecha, usuario, resultado="", serial_nuevo="", valor_nota=None):
    """Cierra el caso y deja el equipo donde corresponde."""
    if not g.abierta:
        raise ValidationError(f"{g.numero} ya está cerrada.")
    if fecha < g.enviada:
        raise ValidationError("La fecha de respuesta no puede ser anterior al envío.")
    activo = g.activo
    nota = f"{g.numero}: {GarantiaProveedor.Estado(estado).label.lower()}" + (f". {resultado}" if resultado else "")
    if estado == G.REEMPLAZADO:
        serial_nuevo = serial_nuevo.strip()
        if not serial_nuevo:
            raise ValidationError("Escriba el serial del equipo que llegó de reemplazo.")
        if Activo.objects.filter(serial=serial_nuevo).exclude(pk=activo.pk).exists():
            raise ValidationError(f"Ya hay otro activo con el serial {serial_nuevo}.")
        g.serial_anterior, activo.serial = activo.serial, serial_nuevo  # sigue con la misma placa
        activo.save(update_fields=["serial"])
        nota += f" (serial {g.serial_anterior or '—'} → {serial_nuevo})"
    if estado == G.NOTA_CREDITO:
        servicios.mover_activo(activo, T.BAJA, Lugar.FUERA, usuario, estado=E.BAJA, fecha=fecha, observaciones=nota[:500])
    else:
        # Si no aplicó la garantía vuelve a revisión (mantenimiento); si quedó bien, disponible
        servicios.mover_activo(activo, T.GARANTIA, Lugar.BODEGA, usuario, estado=E.MANTENIMIENTO if estado == G.RECHAZADA
                               else E.DISPONIBLE, fecha=fecha, observaciones=nota[:500])
    g.estado, g.resuelta, g.resultado, g.valor_nota = estado, fecha, resultado[:300], valor_nota
    g.save()
    return g
