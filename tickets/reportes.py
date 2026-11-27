"""Reportes descargables (18/11/2026): todos los listados en Excel en una sola página, con el mismo periodo.

Antes cada listado (si existía) tenía su botón en una pantalla distinta. Aquí cada reporte dice qué trae, quién lo
puede bajar y se descarga para el periodo elegido (el mismo selector del Centro de indicadores). Los indicadores con
meta están en el Centro; esto son los datos fila por fila para revisar o cruzar en Excel.

Para agregar un reporte: una función ``(usuario, desde, hasta) -> (columnas, anchos, filas)`` y su entrada en
``REPORTES`` con el permiso.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import render
from django.utils import timezone

from comercial import trimestre as tri

from . import centro
from . import indicadores as ind


def _nombre(u):
    return (u.get_full_name() or u.username) if u else ""


def _fecha(momento):
    """Fecha local de un momento (para que Excel la trate como fecha)."""
    if not isinstance(momento, datetime):
        return momento  # ya es fecha (o vacío)
    return timezone.localtime(momento).date()


def _rango(desde, hasta):
    return ind._inicio(desde), ind._inicio(hasta + timedelta(days=1))


def _real(qs, campo, usuario):
    """Sin los clientes PRUEBA, salvo para los usuarios de prueba (que solo ven lo de prueba)."""
    from accounts.prueba import es_de_prueba

    return qs if es_de_prueba(usuario) else tri._sin_prueba(qs, campo)


# ---------------------------------------------------------------------------------------------------------------------
# Cada reporte: (columnas, anchos, filas)
# ---------------------------------------------------------------------------------------------------------------------
def tickets(usuario, desde, hasta):
    from .permisos import tickets_visibles

    ini, fin = _rango(desde, hasta)
    qs = (_real(tickets_visibles(usuario), "empresa", usuario).filter(creado__gte=ini, creado__lt=fin)
          .select_related("empresa", "sede", "categoria", "falla", "asignado_a").order_by("creado"))
    filas = []
    for t in qs:
        termino = t.fecha_resolucion or t.fecha_cierre
        filas.append([t.radicado, _fecha(t.creado), str(t.empresa or ""), str(t.sede or ""), t.get_tipo_display(),
                      str(t.categoria or ""), t.falla_display, t.asunto, t.get_estado_display(), t.get_prioridad_display(),
                      _nombre(t.asignado_a), _fecha(t.fecha_cierre),
                      round((termino - t.creado).total_seconds() / 86400, 1) if termino else None, t.calificacion])
    return (["Radicado", "Registrado", "Cliente", "Sede", "Tipo", "Servicio", "Falla", "Asunto", "Estado", "Prioridad",
             "Técnico", "Cerrado", "Días de solución", "Calificación"],
            (16, 12, 34, 24, 16, 26, 26, 44, 20, 11, 26, 12, 12, 12), filas)


def actas(usuario, desde, hasta):
    from .models import ActaSoporte

    ini, fin = _rango(desde, hasta)
    qs = (_real(ActaSoporte.objects.filter(fecha__gte=ini, fecha__lt=fin), "empresa", usuario)
          .select_related("empresa", "sede", "tecnico", "ticket").order_by("fecha"))
    filas = [[a.numero, _fecha(a.fecha), str(a.empresa or ""), a.sede_texto or str(a.sede or ""),
              _nombre(a.tecnico) or a.tecnico_texto, a.actividad, a.sistema,
              a.get_modalidad_display() if a.modalidad else "", a.get_resultado_display() if a.resultado else "",
              a.ticket.radicado if a.ticket else "No programado", a.get_origen_display()] for a in qs]
    return (["Acta N°", "Fecha", "Cliente", "Sede", "Técnico", "Actividad", "Sistema", "Modalidad", "Resultado", "Ticket",
             "Origen"], (10, 12, 34, 26, 26, 22, 22, 12, 24, 16, 14), filas)


def horas_extras(usuario, desde, hasta):
    from cronograma.horas_validacion import alertas, mapa
    from cronograma.models import ReporteHorasExtra

    qs = (ReporteHorasExtra.objects.filter(fecha__gte=desde, fecha__lte=hasta)
          .select_related("colaborador", "validado_por", "registrado_por", "cerrado_por").order_by("fecha", "hora_salida"))
    filas = [[_nombre(r.colaborador), r.fecha, r.rol_corto, r.hora_salida.strftime("%H:%M"),
              r.hora_llegada.strftime("%H:%M") if r.hora_llegada else "Sin terminar", round(r.minutos / 60, 2), r.cliente,
              mapa(r.lat_salida, r.lon_salida), mapa(r.lat_llegada, r.lon_llegada),
              r.get_validacion_display(), "; ".join(alertas(r)), _nombre(r.validado_por)] for r in qs]
    return (["Colaborador", "Fecha", "Fue", "Inicio", "Fin", "Horas", "Cliente", "Ubicación del inicio", "Ubicación del fin",
             "Validación", "Alertas", "Validó"], (30, 12, 13, 9, 11, 8, 34, 40, 40, 26, 50, 26), filas)


def viaticos(usuario, desde, hasta):
    from cronograma.models import SolicitudViaticos

    qs = (_real(SolicitudViaticos.objects.filter(fecha__gte=desde, fecha__lte=hasta), "ticket__empresa", usuario)
          .select_related("ticket", "solicitada_por", "gestionada_por").order_by("fecha"))
    filas = [[v.numero, v.fecha, v.ticket.radicado if v.ticket else "", v.destino, v.para_texto, v.personas,
              v.conceptos_texto, v.get_estado_display(), float(v.valor_entregado) if v.valor_entregado is not None else None,
              _nombre(v.solicitada_por), _nombre(v.gestionada_por)] for v in qs]
    return (["Solicitud", "Fecha", "Ticket", "Destino", "Para", "Personas", "Conceptos", "Estado", "Valor entregado",
             "Pidió", "Gestionó"], (12, 12, 16, 30, 20, 10, 34, 14, 15, 26, 26), filas)


def ordenes_compra(usuario, desde, hasta):
    from inventario.models import OrdenCompra

    ini, fin = _rango(desde, hasta)
    qs = (_real(OrdenCompra.objects.filter(creado__gte=ini, creado__lt=fin), "ticket__empresa", usuario)
          .select_related("proveedor", "ticket").prefetch_related("lineas").order_by("creado"))
    filas = []
    for o in qs:
        t = o.totales()
        filas.append([o.numero, _fecha(o.creado), str(o.proveedor or ""), o.ticket.radicado if o.ticket else "",
                      o.get_estado_display(), o.forma_pago_texto, float(t["base"]), float(t["iva"]), float(t["total"]),
                      o.fecha_estimada, _fecha(o.recibida_en), _fecha(o.pagada_en)])
    return (["Orden", "Creada", "Proveedor", "Ticket", "Estado", "Forma de pago", "Base", "IVA", "Total", "Llegada estimada",
             "Recibida", "Pagada"], (14, 12, 34, 16, 18, 22, 15, 13, 15, 14, 12, 12), filas)


def propuestas(usuario, desde, hasta):
    ini, fin = _rango(desde, hasta)
    qs = tri._versiones().filter(enviada__gte=ini, enviada__lt=fin).select_related("enviada_por").order_by("enviada")
    filas = [[x.codigo, x.ficha.ticket.radicado, str(x.ficha.ticket.empresa or ""), x.numero, _fecha(x.enviada),
              x.entregada_cliente, x.get_estado_display(), float(x.valor) if x.valor is not None else None,
              _fecha(x.respondida), x.motivo, _nombre(x.enviada_por)] for x in qs]
    return (["Propuesta", "Ticket", "Cliente", "Versión", "Enviada", "Entregada al cliente", "Estado", "Valor antes de IVA",
             "Respondida", "Motivo si no se aprobó", "Envió"], (20, 16, 34, 9, 12, 14, 14, 16, 12, 40, 26), filas)


@dataclass(frozen=True)
class Reporte:
    clave: str
    nombre: str
    area: str
    icono: str
    descripcion: str
    permiso: Callable
    filas: Callable


REPORTES = [
    Reporte("tickets", "Solicitudes (tickets)", "Técnica", "bi-ticket-detailed",
            "Cada solicitud registrada en el periodo: cliente, sede, servicio, estado, técnico, días de solución y calificación.",
            lambda u: u.es_supervisor, tickets),
    Reporte("actas", "Actas de soporte", "Técnica", "bi-clipboard-check",
            "Cada acta del periodo: cliente, sede, técnico, sistema, modalidad, resultado y ticket.",
            lambda u: u.es_supervisor, actas),
    Reporte("horas_extras", "Horas extras", "Técnica", "bi-clock-history",
            "Cada reporte del periodo con sus horas, el cliente, la validación de la coordinación y sus alertas.",
            lambda u: u.gestiona_horas_extras, horas_extras),
    Reporte("viaticos", "Viáticos", "Administrativa y financiera", "bi-wallet2",
            "Cada solicitud de viáticos del periodo: destino, para quién, conceptos, estado y valor entregado.",
            lambda u: u.es_administrativo or u.es_supervisor or u.ve_financiera, viaticos),
    Reporte("ordenes_compra", "Órdenes de compra", "Bodega y compras", "bi-receipt",
            "Cada orden creada en el periodo: proveedor, ticket, estado, forma de pago, base, IVA, total y fechas.",
            lambda u: u.es_supervisor or u.gestiona_inventario or u.ve_financiera, ordenes_compra),
    Reporte("propuestas", "Propuestas enviadas", "Comercial y proyectos", "bi-briefcase",
            "Cada versión de propuesta enviada en el periodo: cliente, valor, estado y respuesta del cliente.",
            lambda u: u.es_supervisor or u.ve_comercial, propuestas),
]


def disponibles(usuario):
    return [r for r in REPORTES if r.permiso(usuario)]


def _excel(r, p, usuario):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    columnas, anchos, filas = r.filas(usuario, p.desde, p.fin)
    libro = Workbook()
    hoja = libro.active
    hoja.title = r.nombre[:31]
    tri.encabezado_excel(hoja, columnas, anchos)
    for fila in filas:
        hoja.append(fila)
        for n, valor in enumerate(fila, start=1):
            if hasattr(valor, "year"):
                hoja.cell(hoja.max_row, n).number_format = "DD/MM/YYYY"
            elif isinstance(valor, float) and columnas[n - 1] in ("Base", "IVA", "Total", "Valor entregado", "Valor antes de IVA"):
                hoja.cell(hoja.max_row, n).number_format = tri.FORMATOS_EXCEL["$"]
    hoja.append([])
    hoja.append([f"{r.nombre} · {p.nombre} · {len(filas)} fila(s) · generado el {timezone.localtime():%d/%m/%Y %H:%M}"
                 f" por {_nombre(usuario)}"])
    hoja.cell(hoja.max_row, 1).font = Font(name="Arial", italic=True, size=9)
    tri.fuente_arial(libro)
    return tri.respuesta_excel(libro, f"{r.clave}_{p.desde:%Y-%m-%d}_{p.fin:%Y-%m-%d}.xlsx")


def vista(request):
    """La página con todos los reportes que la persona puede bajar; ?reporte=<clave> descarga ese en Excel."""
    lista = disponibles(request.user)
    if not lista:
        raise PermissionDenied
    p = centro.periodo(request.GET)
    clave = request.GET.get("reporte")
    if clave:
        r = next((x for x in lista if x.clave == clave), None)
        if r is None:
            raise Http404("Ese reporte no existe o no está a su cargo.")
        return _excel(r, p, request.user)
    consulta = request.GET.copy()
    consulta.pop("reporte", None)
    areas = []
    for r in lista:
        if not areas or areas[-1][0] != r.area:
            areas.append((r.area, []))
        areas[-1][1].append(r)
    return render(request, "tickets/reportes_descargables.html", {
        "areas": areas, "p": p, "periodos": centro.PERIODOS, "consulta": consulta.urlencode(),
        "ve_centro": request.user.es_supervisor,
    })
