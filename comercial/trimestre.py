"""Indicadores comerciales y de proyectos por trimestre, comparados con el trimestre anterior.

Los pidió la coordinación TIC (12/11/2026). Cada uno sale de lo que ya se registra en la plataforma:

* **Propuestas entregadas**: versiones de propuesta que salieron al cliente en el trimestre (la fecha en que el
  comercial la entregó o, si no la registró, la del envío).
* **Propuestas aprobadas**: versiones que el cliente aprobó en el trimestre y su porcentaje sobre las que respondió
  (aprobadas + no aprobadas).
* **Margen de utilidad** (cotizado): de las propuestas aprobadas, (venta − costo) ÷ venta con el costo de cada línea
  del cotizador y el precio con el margen. Las propuestas sin cotizador (solo el PDF) no tienen costo y no cuentan.
* **Margen de rentabilidad** (real): de los proyectos entregados, venta aprobada frente a lo que de verdad costó:

  - el material que salió de bodega para el ticket, neto de lo devuelto, a costo promedio de compra sin IVA;
  - los equipos con placa entregados al ticket que no volvieron a bodega, a su valor de adquisición;
  - los servicios subcontratados en las órdenes de compra del proyecto.

  Lo comprado entra a bodega y sale con la entrega, por eso no se suma también la orden de compra del material.
  La mano de obra propia no tiene costo registrado: el margen real la incluye como utilidad.
* **Margen de rentabilidad consolidado**: suma de utilidades ÷ suma de ventas de los proyectos entregados (pesa más
  el proyecto más grande); también se muestra el promedio simple por proyecto.
* **Rentabilidad de clientes nuevos**: utilidad cotizada aprobada de clientes que antes del trimestre no tenían
  propuestas aprobadas, actas ni solicitudes técnicas.
* **Proyectos entregados**: tickets con propuesta aprobada que se cerraron o cuyo informe de entrega se aprobó en el
  trimestre.
* **Desempeño del área técnica**: actas del trimestre solucionadas ÷ (solucionadas + solucionadas parcialmente).

Lo de prueba (clientes que empiezan por «PRUEBA») y lo cancelado no cuenta.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from tickets.indicadores import Indicador, _inicio, _pct

CERO = Decimal(0)


# ---------------------------------------------------------------------------------------------------------------------
# Trimestres
# ---------------------------------------------------------------------------------------------------------------------
@dataclass
class Trimestre:
    """Un trimestre del año (1 a 4) con sus fechas."""
    anio: int
    numero: int

    @property
    def desde(self):
        """Primer día del trimestre."""
        return date(self.anio, 3 * (self.numero - 1) + 1, 1)

    @property
    def hasta(self):
        """Último día del trimestre."""
        siguiente = self.siguiente
        return siguiente.desde - timedelta(days=1)

    @property
    def anterior(self):
        """El trimestre anterior (el 4 del año pasado si es el 1)."""
        return Trimestre(self.anio - 1, 4) if self.numero == 1 else Trimestre(self.anio, self.numero - 1)

    @property
    def siguiente(self):
        """El trimestre siguiente."""
        return Trimestre(self.anio + 1, 1) if self.numero == 4 else Trimestre(self.anio, self.numero + 1)

    @property
    def clave(self):
        """Texto para la URL: 2026-4."""
        return f"{self.anio}-{self.numero}"

    @property
    def nombre(self):
        """Trimestre 4 de 2026 (oct–dic)."""
        meses = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
        return f"Trimestre {self.numero} de {self.anio} ({meses[self.desde.month - 1]}–{meses[self.hasta.month - 1]})"

    @classmethod
    def de_fecha(cls, dia):
        """El trimestre al que pertenece la fecha."""
        return cls(dia.year, (dia.month - 1) // 3 + 1)

    @classmethod
    def de_texto(cls, texto, hoy):
        """El trimestre de ?t=AAAA-N; el actual si no es válido."""
        try:
            anio, numero = (int(x) for x in (texto or "").split("-"))
            if 2000 <= anio <= 2100 and 1 <= numero <= 4:
                return cls(anio, numero)
        except ValueError:
            pass
        return cls.de_fecha(hoy)


def ultimos(hoy, cuantos=8):
    """Los últimos trimestres (el actual primero), para elegir en la pantalla."""
    t, lista = Trimestre.de_fecha(hoy), []
    for _ in range(cuantos):
        lista.append(t)
        t = t.anterior
    return lista


# ---------------------------------------------------------------------------------------------------------------------
# Venta y costos
# ---------------------------------------------------------------------------------------------------------------------
def _sin_prueba(qs, campo):
    """Quita lo de los clientes de prueba (su nombre empieza por «PRUEBA»)."""
    return qs.exclude(**{f"{campo}__razon_social__istartswith": "PRUEBA"})


def valores_propuesta(version):
    """(venta, costo) de una versión de la propuesta, antes de IVA. El costo es None si no hay cotizador con margen
    (en las propuestas anteriores al margen, el valor de la línea ya era el precio de venta)."""
    cotizacion = getattr(version, "cotizacion", None)
    if cotizacion is None or not cotizacion.lineas.exists():
        return Decimal(version.valor or 0), None
    venta = cotizacion.totales()["base"]
    if not cotizacion.margen or cotizacion.margen >= 100:
        return venta, None
    costo = sum((l.cantidad * l.valor_unitario for l in cotizacion.lineas.all()), CERO)
    return venta, costo


def version_aprobada(ticket):
    """La última versión aprobada de la propuesta del ticket (None si no tiene)."""
    ficha = getattr(ticket, "ficha", None)
    if ficha is None:
        return None
    return ficha.versiones.filter(estado="aprobada").order_by("-numero").first()


def _costo_promedio_sin_iva(insumos):
    """Costo unitario sin IVA de cada insumo: total pagado en las compras ÷ unidades compradas."""
    from inventario.models import MovimientoInsumo

    totales = defaultdict(lambda: [CERO, CERO])
    compras = MovimientoInsumo.objects.filter(tipo="compra", valor_unitario__isnull=False, insumo__in=insumos)
    for c in compras.values("insumo_id", "cantidad", "valor_unitario"):
        totales[c["insumo_id"]][0] += c["cantidad"] * c["valor_unitario"]
        totales[c["insumo_id"]][1] += c["cantidad"]
    return {k: v[0] / v[1] for k, v in totales.items() if v[1]}


def costo_real(ticket):
    """Lo que de verdad costó el proyecto: {'material', 'equipos', 'servicios', 'total', 'sin_costo'}.
    ``sin_costo`` lista lo que salió de bodega sin un costo conocido (el total queda incompleto)."""
    from inventario.models import Lugar, MovimientoActivo, MovimientoInsumo, OrdenCompra

    T = MovimientoInsumo.Tipo
    netos = defaultdict(lambda: CERO)
    nombres = {}
    for m in MovimientoInsumo.objects.filter(ticket=ticket, tipo__in=[T.ENTREGA, T.SALIDA, T.DEVOLUCION]).select_related("insumo"):
        netos[m.insumo_id] += -m.cantidad if m.tipo == T.DEVOLUCION else m.cantidad
        nombres[m.insumo_id] = m.insumo.descripcion
    costos = _costo_promedio_sin_iva([pk for pk, n in netos.items() if n > 0])
    material, sin_costo = CERO, []
    for pk, cantidad in netos.items():
        if cantidad <= 0:
            continue
        if pk in costos:
            material += cantidad * costos[pk]
        else:
            sin_costo.append(nombres[pk])

    equipos, vistos = CERO, set()
    for m in MovimientoActivo.objects.filter(ticket=ticket, tipo__in=[T.ENTREGA, T.SALIDA]).select_related("activo"):
        a = m.activo
        if a.pk in vistos or a.lugar == Lugar.BODEGA:
            continue  # cada equipo una vez; si volvió a bodega no es costo del proyecto
        vistos.add(a.pk)
        if a.valor is None:
            sin_costo.append(a.descripcion or a.placa)
        else:
            equipos += a.valor

    servicios = CERO
    ordenes = (OrdenCompra.objects.filter(Q(ticket=ticket) | Q(solicitud__ticket=ticket))
               .exclude(estado__in=["borrador", "cancelada"]).distinct().prefetch_related("lineas"))
    for o in ordenes:
        servicios += sum((l.cantidad * l.valor_unitario for l in o.lineas.all() if l.es_servicio), CERO)

    return {"material": material, "equipos": equipos, "servicios": servicios, "total": material + equipos + servicios,
            "sin_costo": sin_costo}


def _margen(venta, costo):
    """Margen sobre la venta en %, con un decimal; None sin venta."""
    return round(float(100 * (venta - costo) / venta), 1) if venta else None


# ---------------------------------------------------------------------------------------------------------------------
# Medición de un trimestre
# ---------------------------------------------------------------------------------------------------------------------
def _versiones():
    """Versiones de propuesta que cuentan: sin las de prueba ni las de requerimientos cancelados."""
    from comercial.models import FichaComercial, PropuestaVersion
    from tickets.models import Ticket

    qs = (PropuestaVersion.objects.exclude(ficha__ticket__estado=Ticket.Estado.CANCELADO)
          .exclude(ficha__etapa=FichaComercial.Etapa.CANCELADA)
          .select_related("ficha__ticket__empresa", "cotizacion"))
    return _sin_prueba(qs, "ficha__ticket__empresa")


def clientes_con_historia(antes_de):
    """Ids de las empresas que ya eran clientes antes de esa fecha: con propuesta aprobada, acta o solicitud técnica."""
    from comercial.models import PropuestaVersion
    from tickets.models import ActaSoporte, Ticket

    ini = _inicio(antes_de)
    ids = set(PropuestaVersion.objects.filter(estado="aprobada", respondida__lt=ini)
              .values_list("ficha__ticket__empresa_id", flat=True))
    ids |= set(ActaSoporte.objects.filter(fecha__lt=ini).exclude(empresa=None).values_list("empresa_id", flat=True))
    ids |= set(Ticket.objects.filter(creado__lt=ini, ficha__isnull=True).exclude(empresa=None)
               .values_list("empresa_id", flat=True))
    return ids


def proyectos_entregados(desde, hasta):
    """[(ticket, fecha de entrega)] de los tickets con propuesta aprobada entregados en el periodo: el cierre o el
    informe de entrega aprobado, lo que pasó primero."""
    from tickets.models import InformeBorrador, Ticket

    ini, fin = _inicio(desde), _inicio(hasta + timedelta(days=1))
    con_propuesta = _sin_prueba(Ticket.objects.filter(ficha__versiones__estado="aprobada"), "empresa") \
        .exclude(estado=Ticket.Estado.CANCELADO).distinct()
    fechas = {}
    for pk, cierre in con_propuesta.filter(estado=Ticket.Estado.CERRADO).values_list("pk", "fecha_cierre"):
        if cierre:
            fechas[pk] = cierre
    informes = InformeBorrador.objects.filter(ticket__in=con_propuesta, tipo=InformeBorrador.Tipo.ENTREGA,
                                              estado=InformeBorrador.Estado.APROBADO, aprobado_en__isnull=False)
    for pk, aprobado in informes.values_list("ticket_id", "aprobado_en"):
        fechas[pk] = min(fechas.get(pk, aprobado), aprobado)
    dentro = {pk: f for pk, f in fechas.items() if ini <= f < fin}
    tickets = Ticket.objects.filter(pk__in=dentro).select_related("empresa", "sede", "ficha")
    return sorted(((t, dentro[t.pk]) for t in tickets), key=lambda x: x[1])


@dataclass
class Proyecto:
    """Rentabilidad de un proyecto entregado."""
    ticket: object
    entregado: object
    propuesta: str
    venta: Decimal
    costo_cotizado: Decimal | None
    costo: dict = field(default_factory=dict)

    @property
    def utilidad(self):
        """Venta menos costo real."""
        return self.venta - self.costo["total"]

    @property
    def margen_real(self):
        """Margen de rentabilidad real en %."""
        return _margen(self.venta, self.costo["total"])

    @property
    def margen_cotizado(self):
        """Margen con el que se cotizó (None sin costos en el cotizador)."""
        return _margen(self.venta, self.costo_cotizado) if self.costo_cotizado is not None else None

    @property
    def incompleto(self):
        """Salió material sin costo conocido: el costo real está por debajo de lo que fue."""
        return bool(self.costo["sin_costo"])


def medir(desde, hasta):
    """Valores crudos de un periodo (los mismos se calculan para el trimestre anterior)."""
    from tickets.models import ActaSoporte

    ini, fin = _inicio(desde), _inicio(hasta + timedelta(days=1))
    v = {}

    # Propuestas entregadas: la fecha de entrega al cliente o, si no se registró, la del envío
    versiones = _versiones()
    entregadas = [x for x in versiones.filter(Q(enviada__gte=ini, enviada__lt=fin)
                                              | Q(entregada_cliente__gte=desde, entregada_cliente__lte=hasta))
                  if desde <= (x.entregada_cliente or timezone.localtime(x.enviada).date()) <= hasta]
    v["entregadas"] = len(entregadas)
    v["valor_entregado"] = sum((Decimal(x.valor or 0) for x in entregadas), CERO)

    # Propuestas aprobadas y no aprobadas (respondidas en el periodo)
    respondidas = versiones.filter(respondida__gte=ini, respondida__lt=fin)
    aprobadas = list(respondidas.filter(estado="aprobada"))
    v["aprobadas"] = len(aprobadas)
    v["rechazadas"] = respondidas.filter(estado="rechazada").count()
    v["aprobacion"] = _pct(v["aprobadas"], v["aprobadas"] + v["rechazadas"])

    # Margen de utilidad cotizado de lo aprobado, y lo de los clientes nuevos
    antiguos = clientes_con_historia(desde)
    venta = costo = venta_nuevos = costo_nuevos = CERO
    sin_costos, nuevos = 0, set()
    v["valor_aprobado"] = CERO
    for x in aprobadas:
        vta, cto = valores_propuesta(x)
        v["valor_aprobado"] += vta
        if cto is None:
            sin_costos += 1
            continue
        venta, costo = venta + vta, costo + cto
        empresa = x.ficha.ticket.empresa_id
        if empresa and empresa not in antiguos:
            venta_nuevos, costo_nuevos = venta_nuevos + vta, costo_nuevos + cto
            nuevos.add(empresa)
    v["margen_utilidad"] = _margen(venta, costo)
    v["utilidad_cotizada"] = venta - costo
    v["aprobadas_sin_costos"] = sin_costos
    v["utilidad_nuevos"] = venta_nuevos - costo_nuevos
    v["clientes_nuevos"] = len(nuevos)
    v["margen_nuevos"] = _margen(venta_nuevos, costo_nuevos)
    v["participacion_nuevos"] = _pct(float(v["utilidad_nuevos"]), float(v["utilidad_cotizada"])) \
        if v["utilidad_cotizada"] > 0 else None

    # Proyectos entregados y su rentabilidad real
    proyectos = []
    for t, cuando in proyectos_entregados(desde, hasta):
        version = version_aprobada(t)
        vta, cto = valores_propuesta(version) if version else (CERO, None)
        codigo = version.codigo.rsplit("-V", 1)[0] if version and version.codigo else ""
        proyectos.append(Proyecto(t, cuando, codigo, vta, cto, costo_real(t)))
    v["proyectos"] = proyectos
    v["entregados"] = len(proyectos)
    con_venta = [p for p in proyectos if p.venta]
    v["venta_entregada"] = sum((p.venta for p in con_venta), CERO)
    v["costo_real"] = sum((p.costo["total"] for p in con_venta), CERO)
    v["utilidad_real"] = v["venta_entregada"] - v["costo_real"]
    v["margen_consolidado"] = _margen(v["venta_entregada"], v["costo_real"])
    margenes = [p.margen_real for p in con_venta]
    v["margen_promedio"] = round(sum(margenes) / len(margenes), 1) if margenes else None
    v["incompletos"] = sum(1 for p in con_venta if p.incompleto)

    # Desempeño del área técnica según el resultado de las actas
    actas = _sin_prueba(ActaSoporte.objects.filter(fecha__gte=ini, fecha__lt=fin), "empresa")
    R = ActaSoporte.Resultado
    v["actas"] = actas.count()
    v["solucionadas"] = actas.filter(resultado=R.SOLUCIONADO).count()
    v["parciales"] = actas.filter(resultado=R.PARCIAL).count()
    v["recomendaciones"] = actas.filter(resultado=R.RECOMENDACIONES).count()
    v["desempeno"] = _pct(v["solucionadas"], v["solucionadas"] + v["parciales"])
    return v


def por_tecnico(desde, hasta):
    """Actas de cada técnico en el periodo: solucionadas, parciales y su porcentaje."""
    from tickets.models import ActaSoporte

    ini, fin = _inicio(desde), _inicio(hasta + timedelta(days=1))
    R = ActaSoporte.Resultado
    filas = defaultdict(lambda: {"solucionadas": 0, "parciales": 0, "recomendaciones": 0})
    actas = _sin_prueba(ActaSoporte.objects.filter(fecha__gte=ini, fecha__lt=fin), "empresa").select_related("tecnico")
    for a in actas:
        nombre = (a.tecnico.get_full_name() or a.tecnico.username) if a.tecnico else (a.tecnico_texto or "Sin técnico")
        clave = {R.SOLUCIONADO: "solucionadas", R.PARCIAL: "parciales"}.get(a.resultado, "recomendaciones")
        filas[nombre][clave] += 1
    lista = []
    for nombre, f in sorted(filas.items()):
        f["nombre"] = nombre
        f["total"] = f["solucionadas"] + f["parciales"] + f["recomendaciones"]
        f["desempeno"] = _pct(f["solucionadas"], f["solucionadas"] + f["parciales"])
        lista.append(f)
    return lista


# ---------------------------------------------------------------------------------------------------------------------
# Indicadores
# ---------------------------------------------------------------------------------------------------------------------
def _pesos(valor):
    """$1.574.275"""
    return "$" + f"{int(Decimal(valor).quantize(Decimal('1'))):,}".replace(",", ".")


def calcular(trimestre, hoy=None):
    """Los indicadores del trimestre frente al trimestre anterior completo."""
    from django.conf import settings

    metas = {"aprobacion": 40, "margen_utilidad": None, "margen_consolidado": None, "desempeno": None}
    metas.update({k: v for k, v in getattr(settings, "INDICADORES_METAS", {}).items() if k in metas})
    hasta = min(trimestre.hasta, hoy) if hoy else trimestre.hasta
    v = medir(trimestre.desde, hasta)
    a = medir(trimestre.anterior.desde, trimestre.anterior.hasta)

    indicadores = [
        Indicador("entregadas", "Propuestas entregadas", v["entregadas"], "", "Versiones de propuesta entregadas al "
                  "cliente en el trimestre (la fecha de entrega o, si no se registró, la del envío)",
                  anterior=a["entregadas"], detalle=f"{_pesos(v['valor_entregado'])} antes de IVA"),
        Indicador("aprobadas", "Propuestas aprobadas", v["aprobadas"], "", "Versiones que el cliente aprobó en el trimestre",
                  anterior=a["aprobadas"], detalle=f"{_pesos(v['valor_aprobado'])} antes de IVA"),
        Indicador("aprobacion", "Tasa de aprobación", v["aprobacion"], "%",
                  "Propuestas aprobadas ÷ (aprobadas + no aprobadas) respondidas en el trimestre", metas["aprobacion"],
                  anterior=a["aprobacion"], detalle=f"{v['aprobadas']} aprobadas · {v['rechazadas']} no aprobadas"),
        Indicador("margen_utilidad", "Margen de utilidad (cotizado)", v["margen_utilidad"], "%",
                  "De las propuestas aprobadas: (venta − costo cotizado) ÷ venta, antes de IVA", metas["margen_utilidad"],
                  anterior=a["margen_utilidad"], detalle=f"Utilidad {_pesos(v['utilidad_cotizada'])}"
                  + (f" · {v['aprobadas_sin_costos']} sin costos en el cotizador" if v["aprobadas_sin_costos"] else "")),
        Indicador("margen_promedio", "Margen de rentabilidad (promedio por proyecto)", v["margen_promedio"], "%",
                  "Promedio del margen real de cada proyecto entregado: (venta − costo real) ÷ venta",
                  anterior=a["margen_promedio"], detalle=f"{v['entregados']} proyectos entregados"),
        Indicador("margen_consolidado", "Margen de rentabilidad consolidado", v["margen_consolidado"], "%",
                  "Suma de utilidades reales ÷ suma de ventas de los proyectos entregados en el trimestre",
                  metas["margen_consolidado"], anterior=a["margen_consolidado"],
                  detalle=f"Utilidad real {_pesos(v['utilidad_real'])} de {_pesos(v['venta_entregada'])}"
                  + (f" · {v['incompletos']} con costo incompleto" if v["incompletos"] else "")),
        Indicador("utilidad_nuevos", "Rentabilidad de clientes nuevos", float(v["utilidad_nuevos"]), "$",
                  "Utilidad cotizada de las propuestas aprobadas a clientes que antes del trimestre no tenían propuestas "
                  "aprobadas, actas ni solicitudes técnicas", anterior=float(a["utilidad_nuevos"]),
                  detalle=f"{v['clientes_nuevos']} clientes nuevos"
                  + (f" · margen {v['margen_nuevos']} %" if v["margen_nuevos"] is not None else "")
                  + (f" · {v['participacion_nuevos']} % de la utilidad" if v["participacion_nuevos"] is not None else "")),
        Indicador("entregados", "Proyectos entregados", v["entregados"], "",
                  "Tickets con propuesta aprobada cerrados o con el informe de entrega aprobado en el trimestre",
                  anterior=a["entregados"], detalle=f"{_pesos(v['venta_entregada'])} en ventas"),
        Indicador("desempeno", "Desempeño del área técnica", v["desempeno"], "%",
                  "Actas solucionadas ÷ (solucionadas + solucionadas parcialmente) del trimestre", metas["desempeno"],
                  anterior=a["desempeno"],
                  detalle=f"{v['solucionadas']} solucionadas · {v['parciales']} parciales de {v['actas']} actas"),
    ]
    return {"indicadores": indicadores, "valores": v, "anteriores": a, "hasta": hasta}


def texto(indicador, valor):
    """El valor listo para mostrar: pesos, porcentaje o número."""
    if valor is None:
        return "—"
    if indicador.unidad == "$":
        return _pesos(valor)
    if indicador.unidad == "%":
        return f"{valor:.1f} %".replace(".", ",")
    return f"{valor:g}"


# ---------------------------------------------------------------------------------------------------------------------
# Pantalla y Excel
# ---------------------------------------------------------------------------------------------------------------------
def _filas(resultado):
    """Cada indicador con sus valores ya escritos, para la plantilla y el Excel."""
    return [{"i": i, "valor": texto(i, i.valor), "anterior": texto(i, i.anterior)} for i in resultado["indicadores"]]


def _excel(trimestre, resultado, tecnicos):
    """El libro con los indicadores, la rentabilidad de cada proyecto y el desempeño por técnico."""
    from django.http import HttpResponse
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    negrita, fondo = Font(name="Arial", bold=True, color="FFFFFF"), PatternFill("solid", fgColor="6B7A2E")
    formatos = {"$": '"$"#,##0;-"$"#,##0;"-"', "%": '0.0" %"', "": "0"}

    def encabezado(hoja, columnas, anchos):
        hoja.append(columnas)
        for celda in hoja[hoja.max_row]:
            celda.font, celda.fill = negrita, fondo
            celda.alignment = Alignment(vertical="center", wrap_text=True)
        for n, ancho in enumerate(anchos):
            hoja.column_dimensions[chr(65 + n)].width = ancho
        hoja.freeze_panes = f"A{hoja.max_row + 1}"  # con la celda se crearía una fila vacía

    libro = Workbook()
    hoja = libro.active
    hoja.title = "Indicadores"
    hoja.append([f"Indicadores del {trimestre.nombre} frente al {trimestre.anterior.nombre}"])
    hoja["A1"].font = Font(name="Arial", bold=True, size=12)
    encabezado(hoja, ["Indicador", "Trimestre", "Trimestre anterior", "Variación", "Tendencia", "Meta", "Detalle", "Fórmula"],
               (40, 16, 18, 14, 12, 10, 50, 70))
    for i in resultado["indicadores"]:
        hoja.append([i.nombre, i.valor, i.anterior, None, {"mejor": "Mejoró", "peor": "Empeoró", "igual": "Igual"}.get(i.tendencia, ""),
                     i.meta, i.detalle, i.formula])
        r = hoja.max_row
        hoja.cell(r, 4).value = f'=IF(OR(B{r}="",C{r}=""),"",B{r}-C{r})'
        for c in (2, 3, 4, 6):
            hoja.cell(r, c).number_format = formatos[i.unidad]

    hoja = libro.create_sheet("Proyectos entregados")
    encabezado(hoja, ["Ticket", "Propuesta", "Cliente", "Sede", "Entregado", "Venta", "Costo cotizado", "Material de bodega",
                      "Equipos", "Servicios subcontratados", "Costo real", "Utilidad real", "Margen cotizado", "Margen real",
                      "Costo incompleto"], (12, 18, 34, 24, 12, 15, 15, 15, 13, 15, 15, 15, 12, 12, 40))
    for p in resultado["valores"]["proyectos"]:
        t = p.ticket
        hoja.append([t.radicado, p.propuesta, str(t.empresa or ""), str(t.sede or ""), timezone.localtime(p.entregado).date(),
                     float(p.venta), float(p.costo_cotizado) if p.costo_cotizado is not None else None,
                     float(p.costo["material"]), float(p.costo["equipos"]), float(p.costo["servicios"]), None, None, None, None,
                     ", ".join(p.costo["sin_costo"])])
        r = hoja.max_row
        hoja.cell(r, 11).value = f"=H{r}+I{r}+J{r}"
        hoja.cell(r, 12).value = f"=F{r}-K{r}"
        hoja.cell(r, 13).value = f'=IF(OR(F{r}=0,G{r}=""),"",(F{r}-G{r})/F{r})'
        hoja.cell(r, 14).value = f'=IF(F{r}=0,"",L{r}/F{r})'
        hoja.cell(r, 5).number_format = "DD/MM/YYYY"
        for c in range(6, 13):
            hoja.cell(r, c).number_format = formatos["$"]
        for c in (13, 14):
            hoja.cell(r, c).number_format = "0.0%"

    hoja = libro.create_sheet("Actas por técnico")
    encabezado(hoja, ["Técnico", "Solucionadas", "Parciales", "Recomendaciones", "Total", "Desempeño"], (34, 14, 12, 16, 10, 12))
    for f in tecnicos:
        hoja.append([f["nombre"], f["solucionadas"], f["parciales"], f["recomendaciones"], None, None])
        r = hoja.max_row
        hoja.cell(r, 5).value = f"=B{r}+C{r}+D{r}"
        hoja.cell(r, 6).value = f'=IF(B{r}+C{r}=0,"",B{r}/(B{r}+C{r}))'
        hoja.cell(r, 6).number_format = "0.0%"

    for h in libro.worksheets:
        for fila in h.iter_rows(min_row=2 if h.title == "Indicadores" else 1):
            for celda in fila:
                if not celda.font.bold:
                    celda.font = Font(name="Arial", size=10)
    respuesta = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    respuesta["Content-Disposition"] = f'attachment; filename="indicadores_trimestre_{trimestre.clave}.xlsx"'
    libro.save(respuesta)
    return respuesta


def vista(request):
    """Indicadores comerciales y de proyectos del trimestre frente al anterior (coordinación técnica y dirección)."""
    from django.core.exceptions import PermissionDenied
    from django.shortcuts import render

    if not request.user.es_supervisor:  # la URL ya exige iniciar sesión
        raise PermissionDenied
    hoy = timezone.localdate()
    trimestre = Trimestre.de_texto(request.GET.get("t"), hoy)
    resultado = calcular(trimestre, hoy)
    tecnicos = por_tecnico(trimestre.desde, resultado["hasta"])
    if request.GET.get("formato") == "excel":
        return _excel(trimestre, resultado, tecnicos)
    return render(request, "tickets/indicadores_trimestre.html", {
        "trimestre": trimestre, "trimestres": ultimos(hoy), "en_curso": resultado["hasta"] < trimestre.hasta,
        "hasta": resultado["hasta"], "filas": _filas(resultado), "v": resultado["valores"], "tecnicos": tecnicos,
        "generado": timezone.localtime(),
    })
