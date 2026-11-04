"""Eliminar un ticket del todo (dirección y coordinación), con motivo y registro.

No se puede deshacer: se borran el ticket, su historial, adjuntos (también los archivos), ficha comercial, cotización,
informes, solicitudes de material y sus solicitudes de cotización, y las visitas que aún no han pasado.

Para no perder soportes, en un cliente real se bloquea si el ticket ya tiene:
    · actas de servicio (lo firmado por el cliente),
    · material entregado o movimientos de inventario,
    · órdenes de compra que no estén canceladas,
    · visitas del cronograma que ya pasaron.
Los tickets del cliente PRUEBA se borran siempre, con sus actas y visitas: son ensayos.

Lo que queda: un registro en Configuración → Historial (quién, cuándo, radicado, cliente, asunto y motivo) y un aviso
por correo a dirección cuando lo elimina coordinación.
"""
import logging

from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from config.correo import MARCA_PRUEBA

from . import notificaciones as notif
from .models import Ticket

log = logging.getLogger(__name__)


def puede_eliminar(usuario):
    """Dirección y coordinación (y el superusuario) pueden eliminar tickets."""
    return usuario.is_authenticated and (usuario.es_direccion or usuario.es_coordinador)


def es_de_prueba(ticket):
    """True si el ticket es del cliente PRUEBA (los ensayos se borran sin restricciones)."""
    return bool(MARCA_PRUEBA.match(ticket.empresa.razon_social or ""))


def bloqueos(ticket):
    """Por qué no se puede eliminar este ticket (lista vacía = se puede).

    Solo aplica a clientes reales: lo que ya es soporte de un servicio o de una compra no se borra.
    """
    if es_de_prueba(ticket):
        return []
    from inventario.models import ActaEntrega, MovimientoActivo, MovimientoInsumo, OrdenCompra

    motivos = []
    if ticket.actas.exists():
        motivos.append("tiene actas de servicio firmadas")
    if (ActaEntrega.objects.filter(solicitud__ticket=ticket).exists()
            or MovimientoActivo.objects.filter(ticket=ticket).exists() or MovimientoInsumo.objects.filter(ticket=ticket).exists()):
        motivos.append("ya se le entregó o descontó material del inventario")
    if OrdenCompra.objects.filter(solicitud__ticket=ticket).exclude(estado=OrdenCompra.Estado.CANCELADA).exists():
        motivos.append("tiene órdenes de compra (cancélelas primero)")
    if ticket.actividades.filter(fecha__lt=timezone.localdate()).exists():
        motivos.append("tiene visitas del cronograma que ya pasaron")
    return motivos


def _resumen(ticket):
    """Texto que identifica al ticket borrado en el registro y en el aviso."""
    return f"{ticket.radicado} · {ticket.empresa} · {ticket.asunto}"[:200]


def _correos_direccion():
    """Correos de dirección para el aviso (si no hay, el buzón de soporte)."""
    from inventario.financiera import correos_direccion
    return correos_direccion()


def eliminar(ticket, autor, motivo):
    """Borra el ticket con todo lo suyo y deja el registro. Devuelve el resumen del ticket borrado.

    El aviso a dirección sale antes de borrar: así el correo de un ticket PRUEBA se reconoce como prueba y no le llega
    a nadie más que a quienes prueban.
    """
    from inventario.models import SolicitudCotizacion

    motivo = " ".join(motivo.split())[:500]
    resumen = _resumen(ticket)
    prueba = es_de_prueba(ticket)
    if not autor.es_direccion:
        notif._enviar(f"[{ticket.radicado}] Ticket eliminado por {autor.get_full_name() or autor.username}",
                      f"Se eliminó del todo el ticket {resumen}.\n\nMotivo: {motivo}\n\nQueda el registro en Configuración → "
                      "Historial. No se puede recuperar.", _correos_direccion())
    archivos = [a.archivo for a in ticket.adjuntos.all() if a.archivo]
    with transaction.atomic():
        # Lo que no se borra solo con el ticket (quedaría suelto, sin ticket)
        SolicitudCotizacion.objects.filter(solicitud__ticket=ticket).delete()
        if hasattr(ticket, "ficha"):
            SolicitudCotizacion.objects.filter(propuesta=ticket.ficha).delete()
        visitas = ticket.actividades.all() if prueba else ticket.actividades.filter(fecha__gte=timezone.localdate())
        visitas.delete()
        if prueba:
            ticket.actas.all().delete()
        LogEntry.objects.create(user=autor, content_type=ContentType.objects.get_for_model(Ticket),
                                object_id=str(ticket.pk), object_repr=resumen, action_flag=DELETION,
                                change_message=f"Ticket eliminado desde la plataforma. Motivo: {motivo}")
        ticket.delete()
        transaction.on_commit(lambda: _borrar_archivos(archivos))
    log.info("Ticket eliminado por %s: %s. Motivo: %s", autor, resumen, motivo)
    return resumen


def _borrar_archivos(archivos):
    """Borra del disco los adjuntos del ticket eliminado (si alguno falla, solo queda en el registro)."""
    for archivo in archivos:
        try:
            archivo.storage.delete(archivo.name)
        except Exception:  # noqa: BLE001
            log.exception("No se pudo borrar el archivo %s", archivo.name)
