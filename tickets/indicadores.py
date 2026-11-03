"""Indicadores de gestión del área por periodo (mes, trimestre, semestre, año o rango).

Cada indicador trae su fórmula, la meta y el valor del periodo anterior de la misma duración, para el
informe que se presenta al cerrar el periodo. Las metas se ajustan en settings.INDICADORES_METAS.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.conf import settings
from django.db.models import Avg, Count, Min, Q
from django.utils import timezone

from .models import ActaSoporte, Aplazamiento, Seguimiento, Ticket

E = Ticket.Estado

METAS = {
    "recepcion_horas": 24,      # recepcionar en máximo un día
    "solucion_dias": 5,         # resolver en máximo 5 días
    "cierre": 90,               # % de lo recibido en el periodo que quedó cerrado
    "primera_visita": 80,       # % resuelto sin nueva visita
    "satisfaccion": 8.0,        # promedio de 1 a 10
    "satisfechos": 85,          # % de calificaciones de 8 o más
    "cronograma": 90,           # % de visitas programadas que no se aplazaron
    "aprobacion": 40,           # % de propuestas aprobadas
    "alistamiento_dias": 3,     # de la propuesta aprobada a la entrega del material
    "entregas_confirmadas": 95, # % de actas de entrega que el técnico confirmó
    "compras_a_tiempo": 90,     # % de órdenes recibidas a más tardar en la fecha estimada
    "material_cuadrado": 100,   # % de tickets cerrados con el material cuadrado
}
METAS.update(getattr(settings, "INDICADORES_METAS", {}))

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]
PERIODOS = [("mes", "Este mes"), ("mes_anterior", "Mes anterior"), ("trimestre", "Este trimestre"),
            ("semestre", "Este semestre"), ("anio", "Este año"), ("rango", "Rango de fechas")]


def _inicio(dia):
    return timezone.make_aware(datetime.combine(dia, time.min))


def _fecha(texto):
    try:
        return date.fromisoformat(texto)
    except (TypeError, ValueError):
        return None


def periodo(params, hoy=None):
    """(clave, desde, hasta, etiqueta) según ?periodo= y, para un rango, ?desde=&hasta= (AAAA-MM-DD)."""
    hoy = hoy or timezone.localdate()
    clave = params.get("periodo") or "mes"
    if clave == "mes_anterior":
        hasta = hoy.replace(day=1) - timedelta(days=1)
        desde = hasta.replace(day=1)
    elif clave == "trimestre":
        desde = date(hoy.year, 3 * ((hoy.month - 1) // 3) + 1, 1)
        hasta = hoy
    elif clave == "semestre":
        desde = date(hoy.year, 1 if hoy.month <= 6 else 7, 1)
        hasta = hoy
    elif clave == "anio":
        desde, hasta = date(hoy.year, 1, 1), hoy
    elif clave == "rango" and _fecha(params.get("desde")) and _fecha(params.get("hasta")):
        desde, hasta = sorted([_fecha(params.get("desde")), _fecha(params.get("hasta"))])
    else:
        clave, desde, hasta = "mes", hoy.replace(day=1), hoy
    corte = f" (al {hasta:%d/%m/%Y})" if hasta == hoy and clave != "rango" else ""
    etiqueta = {
        "mes": f"{MESES[desde.month - 1].capitalize()} de {desde.year}{corte}",
        "mes_anterior": f"{MESES[desde.month - 1].capitalize()} de {desde.year}",
        "trimestre": f"Trimestre {(desde.month - 1) // 3 + 1} de {desde.year}{corte}",
        "semestre": f"Semestre {1 if desde.month <= 6 else 2} de {desde.year}{corte}",
        "anio": f"Año {desde.year}{corte}",
    }.get(clave, f"{desde:%d/%m/%Y} al {hasta:%d/%m/%Y}")
    return clave, desde, hasta, etiqueta


def _pct(parte, total):
    return round(100 * parte / total, 1) if total else None


def _horas(delta):
    return delta.total_seconds() / 3600


def _dias_de_solucion(cerrados):
    """Promedio de días del registro a la visita ejecutada (o al cierre, si se cerró sin visita)."""
    dias = [((resuelto or cerrado) - creado).total_seconds() / 86400
            for creado, resuelto, cerrado in cerrados.values_list("creado", "fecha_resolucion", "fecha_cierre")]
    return round(sum(dias) / len(dias), 1) if dias else None


@dataclass
class Indicador:
    clave: str
    nombre: str
    valor: float | None
    unidad: str
    formula: str
    meta: float | None = None
    mayor_es_mejor: bool = True
    anterior: float | None = None
    detalle: str = ""

    @property
    def cumple(self):
        if self.valor is None or self.meta is None:
            return None
        return self.valor >= self.meta if self.mayor_es_mejor else self.valor <= self.meta

    @property
    def tendencia(self):
        """«mejor», «peor» o «igual» frente al periodo anterior."""
        if self.valor is None or self.anterior is None or round(self.valor, 1) == round(self.anterior, 1):
            return "igual" if self.valor is not None and self.anterior is not None else None
        sube = self.valor > self.anterior
        return "mejor" if sube == self.mayor_es_mejor else "peor"


def _medir(tickets, desde, hasta, ve_comercial):
    """Valores crudos de un periodo (los mismos se calculan para el periodo anterior)."""
    ini, fin = _inicio(desde), _inicio(hasta + timedelta(days=1))
    recibidos = tickets.filter(creado__gte=ini, creado__lt=fin).exclude(estado=E.CANCELADO)
    cerrados = tickets.filter(estado=E.CERRADO, fecha_cierre__gte=ini, fecha_cierre__lt=fin)
    v = {"recibidos": recibidos.count(), "cerrados": cerrados.count()}
    v["cierre"] = _pct(recibidos.filter(estado=E.CERRADO).count(), v["recibidos"])

    # Recepción: del registro a «Recepcionado»
    recepciones = dict(Seguimiento.objects.filter(ticket__in=recibidos, estado_nuevo=E.RECEPCIONADO)
                       .values("ticket_id").annotate(primera=Min("creado")).values_list("ticket_id", "primera"))
    horas = [_horas(recepciones[pk] - creado) for pk, creado in recibidos.values_list("pk", "creado") if pk in recepciones]
    v["recepcion_horas"] = round(sum(horas) / len(horas), 1) if horas else None

    # Solución: del registro a la visita ejecutada
    v["solucion_dias"] = _dias_de_solucion(cerrados)

    # Primera visita: cerrados que nunca pasaron por «Pendiente nueva visita»
    con_nueva_visita = cerrados.filter(seguimientos__estado_nuevo=E.PENDIENTE_VISITA).distinct().count()
    v["primera_visita"] = _pct(v["cerrados"] - con_nueva_visita, v["cerrados"])

    # Satisfacción: calificaciones dadas en el periodo
    calificados = tickets.filter(fecha_calificacion__gte=ini, fecha_calificacion__lt=fin, calificacion__isnull=False)
    c = calificados.aggregate(p=Avg("calificacion"), n=Count("id"), buenos=Count("id", filter=Q(calificacion__gte=8)))
    v["satisfaccion"] = round(c["p"], 1) if c["p"] is not None else None
    v["calificaciones"] = c["n"]
    v["satisfechos"] = _pct(c["buenos"], c["n"])
    v["respuesta_encuesta"] = _pct(cerrados.filter(calificacion__isnull=False).count(), v["cerrados"])
    v["distribucion"] = {n: calificados.filter(calificacion=n).count() for n in range(1, Ticket.ESCALA_CALIFICACION + 1)}

    # Cronograma: visitas programadas en el periodo que no se aplazaron
    from cronograma.models import Actividad
    programadas = Actividad.objects.filter(fecha__gte=desde, fecha__lte=hasta).count()
    aplazadas = Aplazamiento.objects.filter(ticket__in=tickets, creado__gte=ini, creado__lt=fin).count()
    v["programadas"], v["aplazadas"] = programadas, aplazadas
    v["cronograma"] = _pct(programadas, programadas + aplazadas)

    actas = ActaSoporte.objects.filter(fecha__gte=ini, fecha__lt=fin)  # las de la plataforma y las de SafetyTrack
    v["actas"] = actas.count()
    v["actas_no_programadas"] = actas.filter(origen=ActaSoporte.Origen.PLATAFORMA, ticket__isnull=True).count()
    v["pendientes"] = tickets.filter(creado__lt=fin).filter(Q(fecha_cierre__isnull=True) | Q(fecha_cierre__gte=fin)).count()

    if ve_comercial:
        from comercial.models import FichaComercial as F
        # Como en «recibidas»: lo cancelado (p. ej. una prueba) no cuenta
        comerciales = F.objects.exclude(ticket__estado=E.CANCELADO).exclude(etapa=F.Etapa.CANCELADA)
        enviadas = comerciales.filter(fecha_envio__gte=ini, fecha_envio__lt=fin)
        respondidas = comerciales.filter(fecha_respuesta__gte=ini, fecha_respuesta__lt=fin)
        aprobadas = respondidas.filter(etapa__in=[F.Etapa.APROBADA, F.Etapa.INSTALACION])
        v["propuestas"] = enviadas.count()
        v["aprobadas"] = aprobadas.count()
        v["aprobacion"] = _pct(v["aprobadas"], respondidas.count())
        v["valor_aprobado"] = sum(f.valor_propuesta or 0 for f in aprobadas)
    v.update(_medir_bodega(tickets, ini, fin))
    return v


def _medir_bodega(tickets, ini, fin):
    """Bodega: alistamiento, actas de entrega confirmadas, compras a tiempo y material cuadrado."""
    from inventario.models import ActaEntrega, MovimientoActivo, MovimientoInsumo, OrdenCompra
    from inventario.solicitudes import cuadre

    v = {}
    actas = ActaEntrega.objects.filter(solicitud__ticket__in=tickets, creado__gte=ini, creado__lt=fin)
    dias = []
    for t_id, primera in actas.values_list("solicitud__ticket").annotate(primera=Min("creado")):
        if ActaEntrega.objects.filter(solicitud__ticket_id=t_id, creado__lt=primera).exists():
            continue  # cuenta la primera entrega del ticket
        desde = (Seguimiento.objects.filter(ticket_id=t_id, estado_nuevo=E.EN_INVENTARIO, creado__lte=primera)
                 .order_by("-creado").values_list("creado", flat=True).first())
        if desde:
            dias.append((primera - desde).total_seconds() / 86400)
    v["alistamiento_dias"] = round(sum(dias) / len(dias), 1) if dias else None
    v["alistados"] = len(dias)
    v["actas_entrega"] = actas.count()
    v["entregas_confirmadas"] = _pct(actas.filter(recibido_en__isnull=False).count(), v["actas_entrega"])
    recibidas = OrdenCompra.objects.filter(estado="recibida", actualizado__gte=ini, actualizado__lt=fin)
    v["compras_recibidas"] = recibidas.count()
    v["compras_a_tiempo"] = _pct(sum(1 for o in recibidas if o.fecha_estimada and timezone.localtime(o.actualizado).date()
                                     <= o.fecha_estimada), v["compras_recibidas"])
    con_material = {pk for modelo in (MovimientoInsumo, MovimientoActivo)
                    for pk in modelo.objects.filter(tipo="entrega", ticket__in=tickets).values_list("ticket_id", flat=True)}
    cerrados = tickets.filter(pk__in=con_material, estado=E.CERRADO, fecha_cierre__gte=ini, fecha_cierre__lt=fin)
    v["cerrados_con_material"] = cerrados.count()
    v["material_cuadrado"] = _pct(sum(1 for t in cerrados if not cuadre(t)["pendientes"]), v["cerrados_con_material"])
    return v


def calcular(tickets, desde, hasta, ve_comercial=False):
    """Indicadores del periodo, comparados con el periodo anterior de la misma duración."""
    v = _medir(tickets, desde, hasta, ve_comercial)
    dias = (hasta - desde).days + 1
    antes = _medir(tickets, desde - timedelta(days=dias), desde - timedelta(days=1), ve_comercial)
    m = METAS

    indicadores = [
        Indicador("cierre", "Eficacia en el cierre", v["cierre"], "%",
                  "Solicitudes recibidas en el periodo que ya están cerradas ÷ solicitudes recibidas", m["cierre"],
                  anterior=antes["cierre"], detalle=f"{v['recibidos']} recibidas"),
        Indicador("recepcion_horas", "Oportunidad en la recepción", v["recepcion_horas"], "h",
                  "Promedio de horas desde que el cliente registra la solicitud hasta que se recepciona",
                  m["recepcion_horas"], mayor_es_mejor=False, anterior=antes["recepcion_horas"]),
        Indicador("solucion_dias", "Tiempo de solución", v["solucion_dias"], " días",
                  "Promedio de días desde el registro hasta la visita ejecutada, de las solicitudes cerradas en el periodo",
                  m["solucion_dias"], mayor_es_mejor=False, anterior=antes["solucion_dias"],
                  detalle=f"{v['cerrados']} cerradas"),
        Indicador("primera_visita", "Solución en la primera visita", v["primera_visita"], "%",
                  "Solicitudes cerradas que no necesitaron una nueva visita ÷ solicitudes cerradas", m["primera_visita"],
                  anterior=antes["primera_visita"]),
        Indicador("cronograma", "Cumplimiento del cronograma", v["cronograma"], "%",
                  "Visitas programadas en el periodo ÷ (programadas + aplazadas)", m["cronograma"],
                  anterior=antes["cronograma"], detalle=f"{v['programadas']} programadas · {v['aplazadas']} aplazadas"),
        Indicador("satisfaccion", "Satisfacción del cliente", v["satisfaccion"], "/10",
                  "Promedio de las calificaciones (1 a 10) recibidas en el periodo", m["satisfaccion"],
                  anterior=antes["satisfaccion"], detalle=f"{v['calificaciones']} calificaciones"),
        Indicador("satisfechos", "Clientes satisfechos", v["satisfechos"], "%",
                  "Calificaciones de 8 o más ÷ calificaciones recibidas", m["satisfechos"], anterior=antes["satisfechos"]),
    ]
    if ve_comercial:
        indicadores.append(Indicador(
            "aprobacion", "Aprobación de propuestas", v["aprobacion"], "%",
            "Propuestas aprobadas ÷ propuestas respondidas por el cliente en el periodo", m["aprobacion"],
            anterior=antes["aprobacion"], detalle=f"{v['aprobadas']} aprobadas de {v['propuestas']} enviadas"))
    indicadores += [
        Indicador("alistamiento_dias", "Tiempo de alistamiento (bodega)", v["alistamiento_dias"], " días",
                  "Promedio de días desde que la propuesta queda aprobada («En inventario») hasta la primera acta de entrega",
                  m["alistamiento_dias"], mayor_es_mejor=False, anterior=antes["alistamiento_dias"],
                  detalle=f"{v['alistados']} instalaciones alistadas"),
        Indicador("entregas_confirmadas", "Entregas confirmadas por el técnico", v["entregas_confirmadas"], "%",
                  "Actas de entrega del periodo que el técnico confirmó ÷ actas de entrega", m["entregas_confirmadas"],
                  anterior=antes["entregas_confirmadas"], detalle=f"{v['actas_entrega']} actas de entrega"),
        Indicador("compras_a_tiempo", "Compras a tiempo", v["compras_a_tiempo"], "%",
                  "Órdenes de compra recibidas a más tardar en la fecha estimada ÷ órdenes recibidas", m["compras_a_tiempo"],
                  anterior=antes["compras_a_tiempo"], detalle=f"{v['compras_recibidas']} recibidas"),
        Indicador("material_cuadrado", "Material cuadrado al cierre", v["material_cuadrado"], "%",
                  "Tickets cerrados con material de bodega en los que entregado = instalado + devuelto ÷ esos tickets",
                  m["material_cuadrado"], anterior=antes["material_cuadrado"], detalle=f"{v['cerrados_con_material']} cerrados con material"),
    ]
    return {"indicadores": indicadores, "valores": v, "anteriores": antes}


def por_responsable(tickets, desde, hasta):
    """Gestión de cada técnico en el periodo: actas, cerrados, tiempo de solución y satisfacción."""
    from accounts.models import Usuario

    ini, fin = _inicio(desde), _inicio(hasta + timedelta(days=1))
    filas = []
    for u in Usuario.tecnicos().exclude(rol=Usuario.Rol.SUPERVISOR, tambien_tecnico=False).order_by("first_name", "last_name"):
        cerrados = tickets.filter(asignado_a=u, estado=E.CERRADO, fecha_cierre__gte=ini, fecha_cierre__lt=fin)
        calif = tickets.filter(asignado_a=u, fecha_calificacion__gte=ini, fecha_calificacion__lt=fin).aggregate(
            p=Avg("calificacion"), n=Count("id"))
        fila = {
            "nombre": u.get_full_name() or u.username,
            "actas": ActaSoporte.objects.filter(tecnico=u, fecha__gte=ini, fecha__lt=fin).count(),
            "cerrados": cerrados.count(),
            "abiertos": tickets.filter(asignado_a=u, estado__in=Ticket.ESTADOS_ABIERTOS).count(),
            "solucion_dias": _dias_de_solucion(cerrados),
            "satisfaccion": round(calif["p"], 1) if calif["p"] is not None else None,
            "calificaciones": calif["n"],
        }
        if fila["actas"] or fila["cerrados"] or fila["abiertos"]:
            filas.append(fila)
    return filas


def por_cliente(tickets, desde, hasta, limite=10):
    ini, fin = _inicio(desde), _inicio(hasta + timedelta(days=1))
    return list(
        tickets.filter(creado__gte=ini, creado__lt=fin).exclude(estado=E.CANCELADO)
        .values("empresa__razon_social")
        .annotate(n=Count("id"), cerrados=Count("id", filter=Q(estado=E.CERRADO)), satisfaccion=Avg("calificacion"))
        .order_by("-n")[:limite]
    )


def por_tipo(tickets, desde, hasta):
    ini, fin = _inicio(desde), _inicio(hasta + timedelta(days=1))
    recibidos = tickets.filter(creado__gte=ini, creado__lt=fin).exclude(estado=E.CANCELADO)
    conteo = dict(recibidos.values_list("tipo").annotate(n=Count("id")))
    total = sum(conteo.values())
    return [(etiqueta, conteo.get(valor, 0), _pct(conteo.get(valor, 0), total)) for valor, etiqueta in Ticket.Tipo.choices
            if conteo.get(valor)]
