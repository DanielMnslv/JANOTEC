"""Metas de los indicadores (18/11/2026): se fijan en el Centro de indicadores → «Metas», no en el código.

Cada indicador tiene una meta por defecto (la que se usaba antes). Dirección o la coordinación la cambian o la dejan
vacía (sin meta) y queda guardada en ``MetaIndicador`` con quién y cuándo. Orden de prioridad:
lo guardado en la plataforma > ``settings.INDICADORES_METAS`` > el defecto de este catálogo.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.conf import settings


@dataclass(frozen=True)
class Definicion:
    """Un indicador con meta: su nombre, área, unidad, si más alto es mejor y la meta por defecto."""
    clave: str
    nombre: str
    area: str
    unidad: str
    mayor_es_mejor: bool = True
    defecto: float | None = None


CATALOGO = [
    Definicion("cierre", "Eficacia en el cierre", "Técnica", "%", True, 90),
    Definicion("recepcion_horas", "Oportunidad en la recepción", "Técnica", "h", False, 24),
    Definicion("solucion_dias", "Tiempo de solución", "Técnica", " días", False, 5),
    Definicion("primera_visita", "Solución en la primera visita", "Técnica", "%", True, 80),
    Definicion("cronograma", "Cumplimiento del cronograma", "Técnica", "%", True, 90),
    Definicion("satisfaccion", "Satisfacción del cliente", "Técnica", "/10", True, 8),
    Definicion("satisfechos", "Clientes satisfechos", "Técnica", "%", True, 85),
    Definicion("desempeno", "Desempeño del área técnica", "Técnica", "%", True, None),
    Definicion("mantenimiento", "Mantenimientos preventivos a tiempo", "Técnica", "%", True, 90),
    Definicion("alistamiento_dias", "Tiempo de alistamiento (bodega)", "Bodega y compras", " días", False, 3),
    Definicion("entregas_confirmadas", "Entregas confirmadas por el técnico", "Bodega y compras", "%", True, 95),
    Definicion("compras_a_tiempo", "Compras a tiempo", "Bodega y compras", "%", True, 90),
    Definicion("material_cuadrado", "Material cuadrado al cierre", "Bodega y compras", "%", True, 100),
    Definicion("entregadas", "Propuestas entregadas", "Comercial y proyectos", "", True, None),
    Definicion("aprobadas", "Propuestas aprobadas", "Comercial y proyectos", "", True, None),
    Definicion("aprobacion", "Tasa de aprobación de propuestas", "Comercial y proyectos", "%", True, 40),
    Definicion("margen_utilidad", "Margen de utilidad (cotizado)", "Comercial y proyectos", "%", True, None),
    Definicion("margen_promedio", "Margen de rentabilidad (promedio por proyecto)", "Comercial y proyectos", "%", True, None),
    Definicion("margen_consolidado", "Margen de rentabilidad consolidado", "Comercial y proyectos", "%", True, None),
    Definicion("utilidad_nuevos", "Rentabilidad de clientes nuevos", "Comercial y proyectos", "$", True, None),
    Definicion("entregados", "Proyectos entregados", "Comercial y proyectos", "", True, None),
]
POR_CLAVE = {d.clave: d for d in CATALOGO}
DEFECTOS = {d.clave: d.defecto for d in CATALOGO}


def valores():
    """{clave: meta o None} con lo guardado en la plataforma encima de los defectos."""
    from .models import MetaIndicador

    m = dict(DEFECTOS)
    m.update({k: v for k, v in getattr(settings, "INDICADORES_METAS", {}).items() if k in m})
    for clave, valor in MetaIndicador.objects.values_list("clave", "valor"):
        if clave in m:
            m[clave] = float(valor) if valor is not None else None
    return m


def aplicar(indicadores, metas=None):
    """Pone a cada indicador la meta vigente (también a los que antes no tenían). Devuelve la misma lista."""
    metas = valores() if metas is None else metas
    for i in indicadores:
        if i.clave in metas:
            i.meta = metas[i.clave]
    return indicadores


def leer(texto, definicion):
    """El número escrito en el formulario (acepta coma decimal y puntos de miles en pesos); None si está vacío.

    Lanza ValueError con el mensaje para la persona si no es válido."""
    texto = (texto or "").strip().replace(" ", "")
    if not texto:
        return None
    if definicion.unidad == "$":
        texto = texto.replace("$", "").replace(".", "")
    try:
        numero = Decimal(texto.replace(",", "."))
    except InvalidOperation:
        raise ValueError(f"{definicion.nombre}: escriba solo el número.")
    if numero < 0:
        raise ValueError(f"{definicion.nombre}: no puede ser negativa.")
    if definicion.unidad == "%" and numero > 100:
        raise ValueError(f"{definicion.nombre}: un porcentaje va de 0 a 100.")
    if definicion.unidad == "/10" and numero > 10:
        raise ValueError(f"{definicion.nombre}: la calificación va de 1 a 10.")
    return numero


def guardar(datos, usuario):
    """Guarda las metas del formulario (``meta_<clave>``). Devuelve (cambiadas, errores)."""
    from .models import MetaIndicador

    actuales, cambios, errores = valores(), [], []
    for d in CATALOGO:
        try:
            nuevo = leer(datos.get(f"meta_{d.clave}"), d)
        except ValueError as e:
            errores.append(str(e))
            continue
        anterior = actuales.get(d.clave)
        if (None if nuevo is None else float(nuevo)) != (None if anterior is None else float(anterior)):
            cambios.append((d, nuevo))
    if errores:
        return [], errores
    for d, nuevo in cambios:
        MetaIndicador.objects.update_or_create(clave=d.clave, defaults={"valor": nuevo, "actualizada_por": usuario})
    return [d for d, _ in cambios], []
