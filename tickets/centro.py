"""Centro de indicadores (17/11/2026): una sola pantalla para «cómo vamos», con una pestaña por área.

Reemplaza a «Reportes» (indicadores de gestión), «Indicadores del trimestre» y los indicadores de la semana del
cronograma, que tenían cada uno su periodo y su formato. Los cálculos son los mismos de siempre
(tickets/indicadores.py, comercial/trimestre.py y cronograma/semanal.py); aquí solo se unifica:

- El periodo: semana, mes, trimestre, semestre, año o un rango, igual en todas las pestañas.
- La comparación: siempre contra el periodo anterior completo (el mes, trimestre o año anterior entero; en una
  semana o un rango, el mismo número de días justo antes). Si el periodo va en curso se compara lo corrido.
- La tarjeta: valor, meta, cumple o no, mejoró o empeoró y la fórmula.
- El Excel: el mismo libro en todas las pestañas («Indicadores» + las tablas de la pestaña).

Pestañas: Resumen (lo clave de cada área), Técnica, Bodega y compras, Comercial y proyectos y Cronograma. Entran
la coordinación técnica y dirección (la coordinación TIC pidió también los indicadores comerciales y de proyectos).
"""
import calendar
from dataclasses import dataclass
from datetime import date, timedelta

from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import render
from django.utils import timezone

from comercial import trimestre as tri

from . import indicadores as ind
from . import metas
from .models import Ticket

MESES = ind.MESES
PERIODOS = [
    ("semana", "Esta semana"), ("semana_anterior", "Semana anterior"),
    ("mes", "Este mes"), ("mes_anterior", "Mes anterior"),
    ("trimestre", "Este trimestre"), ("trimestre_anterior", "Trimestre anterior"),
    ("semestre", "Este semestre"), ("anio", "Este año"), ("anio_anterior", "Año anterior"),
    ("rango", "Rango de fechas"),
]
PESTANAS = [
    ("resumen", "Resumen", "bi-speedometer2"),
    ("tecnica", "Técnica", "bi-tools"),
    ("bodega", "Bodega y compras", "bi-boxes"),
    ("comercial", "Comercial y proyectos", "bi-briefcase"),
    ("cronograma", "Cronograma", "bi-calendar-week"),
]
TECNICA = ["cierre", "recepcion_horas", "solucion_dias", "primera_visita", "cronograma", "satisfaccion", "satisfechos",
           "desempeno", "mantenimiento"]
BODEGA = ["alistamiento_dias", "entregas_confirmadas", "compras_a_tiempo", "material_cuadrado"]
# Lo clave de cada área para dirección (pestaña Resumen)
RESUMEN = [
    ("tecnica", "Área técnica", ["cierre", "solucion_dias", "satisfaccion", "cronograma", "desempeno", "mantenimiento"]),
    ("bodega", "Bodega y compras", ["alistamiento_dias", "compras_a_tiempo", "material_cuadrado"]),
    ("comercial", "Comercial y proyectos", ["aprobacion", "margen_consolidado", "entregados", "utilidad_nuevos"]),
]


# ---------------------------------------------------------------------------------------------------------------------
# Periodo
# ---------------------------------------------------------------------------------------------------------------------
def _sumar_meses(dia, meses):
    """El primer día del mes que queda ``meses`` después (o antes, si es negativo)."""
    n = dia.year * 12 + dia.month - 1 + meses
    return date(n // 12, n % 12 + 1, 1)


def _fin_de_mes(dia):
    return dia.replace(day=calendar.monthrange(dia.year, dia.month)[1])


def describir(desde, fin):
    """Nombre del periodo: «Noviembre de 2026», «Trimestre 4 de 2026», «Año 2026», «Semana del 9 al 15/11/2026»…"""
    if desde.day == 1 and fin == _fin_de_mes(fin):
        meses = (fin.year - desde.year) * 12 + fin.month - desde.month + 1
        if meses == 1:
            return f"{MESES[desde.month - 1].capitalize()} de {desde.year}"
        if meses == 3 and desde.month % 3 == 1:
            return f"Trimestre {(desde.month - 1) // 3 + 1} de {desde.year}"
        if meses == 6 and desde.month in (1, 7):
            return f"Semestre {1 if desde.month == 1 else 2} de {desde.year}"
        if meses == 12 and desde.month == 1:
            return f"Año {desde.year}"
    if desde.weekday() == 0 and (fin - desde).days == 6:
        return f"Semana del {desde:%d/%m} al {fin:%d/%m/%Y}"
    return f"{desde:%d/%m/%Y} al {fin:%d/%m/%Y}"


def anterior(desde, fin):
    """El periodo anterior completo: los mismos meses antes si el periodo es de meses enteros; si no, los mismos días."""
    if desde.day == 1 and fin == _fin_de_mes(fin):
        meses = (fin.year - desde.year) * 12 + fin.month - desde.month + 1
        return _sumar_meses(desde, -meses), desde - timedelta(days=1)
    dias = (fin - desde).days + 1
    return desde - timedelta(days=dias), desde - timedelta(days=1)


@dataclass
class Periodo:
    """El periodo elegido: ``hasta`` es hoy si va en curso; ``fin`` es su último día."""
    clave: str
    desde: date
    hasta: date
    fin: date

    @property
    def en_curso(self):
        return self.hasta < self.fin

    @property
    def nombre(self):
        return describir(self.desde, self.fin)

    @property
    def anterior(self):
        return anterior(self.desde, self.fin)

    @property
    def nombre_anterior(self):
        return describir(*self.anterior)


def periodo(params, hoy=None):
    """El periodo de ?periodo= (y ?desde=&hasta= para un rango). Por omisión, el mes en curso."""
    hoy = hoy or timezone.localdate()
    clave = params.get("periodo") or "mes"
    lunes = hoy - timedelta(days=hoy.weekday())
    trimestre = date(hoy.year, 3 * ((hoy.month - 1) // 3) + 1, 1)
    if clave == "semana":
        desde, fin = lunes, lunes + timedelta(days=6)
    elif clave == "semana_anterior":
        desde, fin = lunes - timedelta(days=7), lunes - timedelta(days=1)
    elif clave == "mes_anterior":
        fin = hoy.replace(day=1) - timedelta(days=1)
        desde = fin.replace(day=1)
    elif clave == "trimestre":
        desde, fin = trimestre, _sumar_meses(trimestre, 3) - timedelta(days=1)
    elif clave == "trimestre_anterior":
        desde, fin = _sumar_meses(trimestre, -3), trimestre - timedelta(days=1)
    elif clave == "semestre":
        desde = date(hoy.year, 1 if hoy.month <= 6 else 7, 1)
        fin = _sumar_meses(desde, 6) - timedelta(days=1)
    elif clave == "anio":
        desde, fin = date(hoy.year, 1, 1), date(hoy.year, 12, 31)
    elif clave == "anio_anterior":
        desde, fin = date(hoy.year - 1, 1, 1), date(hoy.year - 1, 12, 31)
    elif clave == "rango" and ind._fecha(params.get("desde")) and ind._fecha(params.get("hasta")):
        desde, fin = sorted([ind._fecha(params.get("desde")), ind._fecha(params.get("hasta"))])
    else:
        clave, desde, fin = "mes", hoy.replace(day=1), _fin_de_mes(hoy)
    return Periodo(clave, desde, max(desde, min(fin, hoy)), fin)


# ---------------------------------------------------------------------------------------------------------------------
# Valores para mostrar
# ---------------------------------------------------------------------------------------------------------------------
def texto(i, valor):
    """El valor listo para leer: pesos, porcentaje, días, horas o /10, con coma decimal."""
    if valor is None:
        return "—"
    if i.unidad == "$":
        return tri._pesos(valor)
    numero = f"{round(valor, 1):g}".replace(".", ",")
    if i.unidad == "%":
        return f"{numero} %"
    return f"{numero}{i.unidad}"


def tarjeta(i, area=""):
    """Lo que necesita la tarjeta de un indicador (tickets/_tarjeta_indicador.html)."""
    return {"i": i, "area": area, "valor": texto(i, i.valor), "anterior": texto(i, i.anterior),
            "meta": None if i.meta is None else ("≥ " if i.mayor_es_mejor else "≤ ") + texto(i, i.meta)}


# ---------------------------------------------------------------------------------------------------------------------
# Cálculo por pestaña
# ---------------------------------------------------------------------------------------------------------------------
def _gestion(tickets, p):
    """Indicadores de gestión (técnica y bodega) del periodo frente al anterior completo."""
    return ind.calcular(tickets, p.desde, p.hasta, False, p.anterior)


def _tecnicos_actas(p):
    """Desempeño por actas y mantenimientos a tiempo (comercial/trimestre.py)."""
    return tri.indicadores_tecnicos(tri.medir_tecnico(p.desde, p.hasta), tri.medir_tecnico(*p.anterior))


def _por_tecnico(tickets, p):
    """Gestión de cada técnico (actas, cerrados, días, satisfacción) con su desempeño por el resultado de las actas."""
    filas = ind.por_responsable(tickets, p.desde, p.hasta)
    actas = {f["nombre"]: f for f in tri.por_tecnico(p.desde, p.hasta)}
    for f in filas:
        a = actas.pop(f["nombre"], {})
        f.update(solucionadas=a.get("solucionadas", 0), parciales=a.get("parciales", 0), desempeno=a.get("desempeno"))
    for nombre, a in actas.items():  # p. ej. actas de SafetyTrack con el nombre escrito
        filas.append({"nombre": nombre, "actas": a["total"], "cerrados": 0, "abiertos": 0, "solucion_dias": None,
                      "satisfaccion": None, "calificaciones": 0, "solucionadas": a["solucionadas"],
                      "parciales": a["parciales"], "desempeno": a["desempeno"]})
    return filas


def _elegir(lista, claves):
    """Los indicadores de esas claves, en ese orden, con la meta vigente."""
    por_clave = {i.clave: i for i in lista}
    return metas.aplicar([por_clave[c] for c in claves if c in por_clave])


def calcular(pestana, usuario, tickets, p):
    """Las tarjetas y tablas de la pestaña."""
    ctx = {}
    if pestana == "tecnica":
        g = _gestion(tickets, p)
        v = g["valores"]
        mayor = max(v["distribucion"].values()) or 1
        ctx.update(
            tarjetas=[tarjeta(i) for i in _elegir(g["indicadores"] + _tecnicos_actas(p), TECNICA)], v=v,
            distribucion=[(n, c, round(100 * c / mayor)) for n, c in v["distribucion"].items()],
            por_tecnico=_por_tecnico(tickets, p), por_cliente=ind.por_cliente(tickets, p.desde, p.hasta),
            por_tipo=ind.por_tipo(tickets, p.desde, p.hasta),
            por_falla=(tickets.filter(creado__gte=ind._inicio(p.desde), creado__lt=ind._inicio(p.hasta + timedelta(days=1)))
                       .exclude(falla=None).values("falla__nombre", "categoria__nombre").annotate(n=Count("id")).order_by("-n")[:8]),
            por_estado=[e for e in ((label, tickets.filter(estado=valor).count(), valor) for valor, label in Ticket.Estado.choices) if e[1]],
        )
    elif pestana == "bodega":
        g = _gestion(tickets, p)
        ctx.update(tarjetas=[tarjeta(i) for i in _elegir(g["indicadores"], BODEGA)], v=g["valores"])
    elif pestana == "comercial":
        v, a = tri.medir_comercial(p.desde, p.hasta), tri.medir_comercial(*p.anterior)
        ctx.update(tarjetas=[tarjeta(i) for i in metas.aplicar(tri.indicadores_comerciales(v, a))], v=v)
    elif pestana == "cronograma":
        from cronograma import semanal
        from cronograma.models import Actividad

        actividades = (Actividad.objects.filter(fecha__range=(p.desde, p.hasta)).select_related("ticket")
                       .prefetch_related("encargados"))
        ctx.update(crono=semanal.indicadores(actividades))
    else:  # resumen
        g = _gestion(tickets, p)
        todos = (g["indicadores"] + _tecnicos_actas(p)
                 + tri.indicadores_comerciales(tri.medir_comercial(p.desde, p.hasta), tri.medir_comercial(*p.anterior)))
        grupos = []
        for clave, titulo, claves in RESUMEN:
            grupos.append({"clave": clave, "titulo": titulo, "tarjetas": [tarjeta(i, titulo) for i in _elegir(todos, claves)]})
        ctx.update(grupos=grupos, tarjetas=[t for g_ in grupos for t in g_["tarjetas"]])
    return ctx


# ---------------------------------------------------------------------------------------------------------------------
# Excel: el mismo libro en todas las pestañas
# ---------------------------------------------------------------------------------------------------------------------
def _excel(pestana, nombre_pestana, p, ctx):
    from openpyxl import Workbook
    from openpyxl.styles import Font

    libro = Workbook()
    hoja = libro.active
    hoja.title = "Indicadores"
    hoja.append([f"{nombre_pestana} · {p.nombre}{' (al ' + p.hasta.strftime('%d/%m/%Y') + ')' if p.en_curso else ''}"
                 f" frente a {p.nombre_anterior}"])
    hoja["A1"].font = Font(name="Arial", bold=True, size=12)
    tri.encabezado_excel(hoja, ["Área", "Indicador", "Periodo", "Periodo anterior", "Variación", "Tendencia", "Meta",
                                "Cumple", "Unidad", "Detalle", "Fórmula"], (18, 40, 14, 16, 12, 11, 10, 10, 9, 50, 70))
    for t in ctx.get("tarjetas", []):
        i = t["i"]
        hoja.append([t["area"] or nombre_pestana, i.nombre, i.valor, i.anterior, None,
                     {"mejor": "Mejoró", "peor": "Empeoró", "igual": "Igual"}.get(i.tendencia, ""), i.meta,
                     {True: "Sí", False: "No"}.get(i.cumple, ""), i.unidad.strip(), i.detalle, i.formula])
        r = hoja.max_row
        hoja.cell(r, 5).value = f'=IF(OR(C{r}="",D{r}=""),"",C{r}-D{r})'
        for c in (3, 4, 5, 7):
            hoja.cell(r, c).number_format = tri.FORMATOS_EXCEL.get(i.unidad, "0.0")

    if not ctx.get("tarjetas"):
        c = ctx.get("crono", {})
        hoja.append([nombre_pestana, f"Actividades: {c.get('total', 0)} · autorizadas: {c.get('autorizadas', 0)}"
                     f" · en rojo: {c.get('en_rojo', 0)}. El detalle está en las hojas siguientes."])
    if pestana == "tecnica":
        h = libro.create_sheet("Por técnico")
        tri.encabezado_excel(h, ["Técnico", "Actas", "Cerrados", "Abiertos hoy", "Días de solución", "Satisfacción",
                                 "Solucionadas", "Parciales", "Desempeño"], (34, 9, 10, 12, 15, 12, 13, 10, 12))
        for f in ctx["por_tecnico"]:
            h.append([f["nombre"], f["actas"], f["cerrados"], f["abiertos"], f["solucion_dias"], f["satisfaccion"],
                      f["solucionadas"], f["parciales"], None])
            r = h.max_row
            h.cell(r, 9).value = f'=IF(G{r}+H{r}=0,"",G{r}/(G{r}+H{r}))'
            h.cell(r, 9).number_format = "0.0%"
        h = libro.create_sheet("Por cliente")
        tri.encabezado_excel(h, ["Cliente", "Recibidas", "Cerradas", "Satisfacción"], (44, 11, 11, 13))
        for c in ctx["por_cliente"]:
            h.append([c["empresa__razon_social"], c["n"], c["cerrados"], round(c["satisfaccion"], 1) if c["satisfaccion"] else None])
    elif pestana == "comercial":
        tri.hoja_proyectos(libro, ctx["v"]["proyectos"])
    elif pestana == "cronograma":
        for titulo, clave in (("Por tipo", "por_tipo"), ("Carga por técnico", "por_tecnico"), ("Por cliente", "por_cliente"),
                              ("Por sede", "por_sede"), ("Por línea", "por_linea")):
            h = libro.create_sheet(titulo)
            tri.encabezado_excel(h, [titulo.replace("Por ", "").capitalize(), "Actividades"], (50, 13))
            for f in ctx["crono"].get(clave, []):
                h.append([f["etiqueta"], f["n"]])
    tri.fuente_arial(libro)
    return tri.respuesta_excel(libro, f"indicadores_{pestana}_{p.desde:%Y-%m-%d}_{p.fin:%Y-%m-%d}.xlsx")


# ---------------------------------------------------------------------------------------------------------------------
# Pantalla
# ---------------------------------------------------------------------------------------------------------------------
def pestanas_de(usuario):
    """Las pestañas que la persona puede ver (hoy todas para quien entra al Centro)."""
    return list(PESTANAS)


def vista(request):
    """Centro de indicadores: pestañas por área, el mismo periodo y la misma comparación en todas, y su Excel."""
    from .permisos import tickets_visibles

    if not request.user.es_supervisor:  # la URL ya exige iniciar sesión
        raise PermissionDenied
    pestanas = pestanas_de(request.user)
    pestana = request.GET.get("pestana") if request.GET.get("pestana") in {x[0] for x in pestanas} else "resumen"
    nombre_pestana = next(n for c, n, _ in pestanas if c == pestana)
    p = periodo(request.GET)
    ctx = calcular(pestana, request.user, tickets_visibles(request.user), p)
    if request.GET.get("formato") == "excel":
        return _excel(pestana, nombre_pestana, p, ctx)
    consulta = request.GET.copy()
    consulta.pop("pestana", None)
    consulta.pop("formato", None)
    return render(request, "tickets/centro_indicadores.html", {
        **ctx, "pestana": pestana, "nombre_pestana": nombre_pestana, "pestanas": pestanas, "p": p,
        "periodos": PERIODOS, "consulta": consulta.urlencode(), "generado": timezone.localtime(),
    })


def vista_metas(request):
    """Metas de los indicadores: dirección o la coordinación las fijan (vacío = sin meta) y quedan con quién y cuándo."""
    from django.contrib import messages
    from django.shortcuts import redirect
    from django.urls import reverse

    from .models import MetaIndicador

    if not request.user.es_supervisor:
        raise PermissionDenied
    errores = []
    if request.method == "POST":
        cambiadas, errores = metas.guardar(request.POST, request.user)
        if not errores:
            if cambiadas:
                messages.success(request, "Metas guardadas: " + ", ".join(d.nombre for d in cambiadas) + ".")
            else:
                messages.info(request, "No cambió ninguna meta.")
            return redirect("indicadores_metas")
    vigentes = metas.valores()
    guardadas = {m.clave: m for m in MetaIndicador.objects.select_related("actualizada_por")}
    filas = []
    for d in metas.CATALOGO:
        valor = request.POST.get(f"meta_{d.clave}") if errores else vigentes.get(d.clave)
        if isinstance(valor, float):
            valor = f"{valor:g}".replace(".", ",")
        filas.append({"d": d, "valor": "" if valor is None else valor, "guardada": guardadas.get(d.clave),
                      "defecto": "sin meta" if d.defecto is None else f"{d.defecto:g}".replace(".", ",")})
    areas = []
    for f in filas:
        if not areas or areas[-1][0] != f["d"].area:
            areas.append((f["d"].area, []))
        areas[-1][1].append(f)
    return render(request, "tickets/centro_metas.html", {"areas": areas, "errores": errores,
                                                         "volver": reverse("indicadores_centro")})
