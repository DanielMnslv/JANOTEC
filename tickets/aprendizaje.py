"""Aprendizaje de la redacción a partir de lo que JANOTEC ya escribió y aprobó (23/11/2026).

No se reentrena ningún modelo. Antes de cada «Mejorar redacción», la plataforma busca en su propio historial los
documentos más parecidos al texto que se está mejorando y se los muestra a Claude como referencia de lenguaje técnico
y comercial, junto con los nombres de equipos y materiales tal como están en bodega. Entre más actas, informes y
propuestas tenga la plataforma, mejor escribe.

Fuentes (solo lo que ya pasó revisión):
- actas de soporte registradas: diagnóstico y observaciones del resultado;
- informes aprobados por dirección (pasados a limpio);
- alcances de propuestas aprobadas por dirección.

Privacidad: antes de salir hacia Anthropic, cada documento se anonimiza. Los nombres de clientes, sedes y personas,
los NIT, los correos y los celulares se cambian por marcadores ([CLIENTE], [SEDE], [PERSONA], [NIT], [CORREO],
[TELÉFONO]). Lo que importa es el estilo, no los datos.

La búsqueda de parecidos se hace aquí, comparando palabras: no cuesta nada. El historial se arma una vez cada media
hora y queda en caché.
"""

import math
import re
import unicodedata

from django.core.cache import cache

DOCUMENTOS_MAXIMOS = 2  # documentos aprendidos por mejora (además de los ejemplos enseñados a mano)
LARGO_DOCUMENTO = 3500  # caracteres por documento aprendido
PARECIDO_MINIMO = 0.08  # por debajo de esto el documento no se parece lo suficiente
PARECIDO_MAXIMO = 0.9  # por encima es el mismo texto que se está editando: no sirve de ejemplo
TERMINOS_MAXIMOS = 30  # nombres de equipos y materiales del catálogo por mejora
CACHE_SEGUNDOS = 30 * 60
CLAVE_CACHE = "redaccion:aprendizaje:v1"

PALABRAS_VACIAS = set("""
para como pero porque cuando donde desde hasta entre sobre bajo tras ante este esta estos estas ese esa esos esas
aquel aquella cada todo toda todos todas otro otra otros otras mismo misma muy mas menos tambien ademas asi solo
segun parte area areas debe deben puede pueden tiene tienen habia hace hacer hizo sido esta estan fueron fue ser
realizo realizar realizado realiza encontro encontraron cual cuales cuyo cuya sus suyo nuestra nuestro ellos ellas
dicho dicha dichos dichas """.split())

GENERICOS_SEDE = {"principal", "sede", "oficina", "oficinas", "bodega", "planta", "general", "central", "norte", "sur"}


# --- Palabras y parecido --------------------------------------------------------------------------------------------
def _sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def palabras(texto):
    """Las palabras con contenido del texto (sin tildes, en minúscula, de cuatro letras o más)."""
    return {p for p in re.findall(r"[a-z0-9]{4,}", _sin_tildes((texto or "").lower())) if p not in PALABRAS_VACIAS}


def parecido(a, b):
    """Qué tanto comparten dos conjuntos de palabras (0 = nada, 1 = las mismas)."""
    if not a or not b:
        return 0.0
    return len(a & b) / math.sqrt(len(a) * len(b))


# --- Anonimizar -----------------------------------------------------------------------------------------------------
VOCALES = {"a": "[aá]", "e": "[eé]", "i": "[ií]", "o": "[oó]", "u": "[uúü]", "n": "[nñ]"}
CORREO = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CELULAR = re.compile(r"(?<!\d)(?:\+?57[\s-]?)?3\d{2}[\s-]?\d{3}[\s-]?\d{4}(?!\d)")
NIT = re.compile(r"(?<![\w.])\d{3}\.?\d{3}\.?\d{3}(?:\s?-\s?\d)?(?![\w.])")


def _patron(nombre):
    """Expresión que encuentra el nombre sin importar tildes, mayúsculas ni espacios de más."""
    partes = []
    for c in _sin_tildes(nombre.strip().lower()):
        if c.isspace():
            if partes and partes[-1] != r"\s+":
                partes.append(r"\s+")
        else:
            partes.append(VOCALES.get(c, re.escape(c)))
    return "".join(partes)


def anonimizar(texto, clientes=(), sedes=(), personas=()):
    """El texto con nombres de clientes, sedes y personas, NIT, correos y celulares cambiados por marcadores."""
    reemplazos = []
    for nombres, marca in ((clientes, "[CLIENTE]"), (sedes, "[SEDE]"), (personas, "[PERSONA]")):
        for n in nombres:
            n = " ".join(str(n or "").split())
            if len(n) >= 4 and _sin_tildes(n.lower()) not in GENERICOS_SEDE:
                reemplazos.append((n, marca))
    texto = CORREO.sub("[CORREO]", texto or "")
    texto = CELULAR.sub("[TELÉFONO]", texto)
    texto = NIT.sub("[NIT]", texto)
    for nombre, marca in sorted(reemplazos, key=lambda r: -len(r[0])):  # primero los largos: «Poligrow Colombia» antes que «Poligrow»
        texto = re.sub(r"(?<!\w)" + _patron(nombre) + r"(?!\w)", marca, texto, flags=re.IGNORECASE)
    return texto


def _personas_globales():
    """Nombres completos del personal y de los usuarios (nombre y apellido juntos), para no enviarlos nunca."""
    from django.contrib.auth import get_user_model

    nombres = []
    for nombre, apellido in get_user_model().objects.values_list("first_name", "last_name"):
        completo = f"{nombre} {apellido}".strip()
        if nombre and apellido:
            nombres.append(completo)
            nombres.append(f"{nombre.split()[0]} {apellido.split()[0]}")
    return nombres


def _clientes_globales():
    from accounts.models import Empresa

    return [n for n in Empresa.objects.values_list("razon_social", flat=True) if n and len(n) >= 5]


def marcadores():
    """(clientes, personas) de toda la plataforma, para anonimizar cualquier texto; en caché media hora."""
    datos = cache.get(CLAVE_CACHE + ":marcadores")
    if datos is None:
        datos = (_clientes_globales(), _personas_globales())
        cache.set(CLAVE_CACHE + ":marcadores", datos, CACHE_SEGUNDOS)
    return datos


# --- El historial ---------------------------------------------------------------------------------------------------
def _documento(grupo, titulo, texto, clientes, sedes, personas):
    texto = anonimizar(texto.strip()[:LARGO_DOCUMENTO], clientes, sedes, personas)
    return {"grupo": grupo, "titulo": titulo, "texto": texto, "palabras": sorted(palabras(texto))}


def _lineas(texto):
    return "\n".join(f"• {l.strip().lstrip('•-–* ').strip()}" for l in (texto or "").splitlines() if l.strip())


def construir_historial():
    """Los documentos aprobados, ya anonimizados, con sus palabras. Se guarda en caché media hora."""
    from comercial.models import Cotizacion

    from .models import ActaSoporte, InformeBorrador

    clientes, personas = marcadores()
    docs = []

    actas = (ActaSoporte.objects.select_related("empresa", "sede", "tecnico", "ticket")
             .prefetch_related("firmas").order_by("-fecha")[:400])
    for a in actas:
        propios_c = [a.empresa.razon_social] if a.empresa_id else []
        propios_s = [s for s in (a.sede.nombre if a.sede_id else "", a.sede_texto) if s]
        propios_p = [a.tecnico_texto, a.tecnico.get_full_name() if a.tecnico_id else "",
                     a.ticket.contacto_nombre if a.ticket_id else ""] + [f.nombre for f in a.firmas.all()]
        args = (clientes + propios_c, propios_s, personas + [p for p in propios_p if p])
        if len(a.diagnostico or "") >= 80:
            docs.append(_documento("diagnostico", f"Diagnóstico de un acta de {a.sistema or 'soporte'}", a.diagnostico, *args))
        if len(a.observaciones or "") >= 80:
            docs.append(_documento("resultado", f"Resultado de un acta de {a.sistema or 'soporte'}", a.observaciones, *args))

    informes = (InformeBorrador.objects.filter(estado=InformeBorrador.Estado.APROBADO)
                .select_related("ticket__empresa", "ticket__sede").order_by("-aprobado_en")[:150])
    for i in informes:
        partes = [f"Asunto: {i.asunto}", "Introducción", i.introduccion, "Actividades realizadas", _lineas(i.actividades)]
        if i.justificacion.strip():
            partes += ["Justificación", i.justificacion]
        partes += ["Conclusiones", _lineas(i.conclusiones)]
        if i.recomendaciones.strip():
            partes += ["Recomendaciones", _lineas(i.recomendaciones)]
        t = i.ticket
        docs.append(_documento("informe", f"Informe aprobado ({i.get_tipo_display().lower()})", "\n\n".join(partes),
                               clientes + [t.empresa.razon_social], [t.sede.nombre] if t.sede_id else [],
                               personas + [i.destinatario, t.contacto_nombre]))

    propuestas = (Cotizacion.objects.filter(aprobada_por__isnull=False).exclude(alcance="")
                  .select_related("ficha__ticket__empresa", "ficha__ticket__sede").order_by("-actualizado")[:150])
    for c in propuestas:
        if len(c.alcance) < 80:
            continue
        t = c.ficha.ticket
        docs.append(_documento("propuesta", "Alcance de una propuesta aprobada", c.alcance,
                               clientes + [t.empresa.razon_social], [t.sede.nombre] if t.sede_id else [],
                               personas + [t.contacto_nombre]))
    return docs


def historial():
    docs = cache.get(CLAVE_CACHE)
    if docs is None:
        docs = construir_historial()
        cache.set(CLAVE_CACHE, docs, CACHE_SEGUNDOS)
    return docs


def documentos_parecidos(grupos, texto):
    """Los documentos aprobados que más se parecen al texto, de los grupos que le corresponden.

    Si el texto es de un campo cualquiera («general»), se busca en todos los grupos.
    """
    especificos = [g for g in grupos if g != "general"]
    q = palabras(texto)
    puntajes = []
    for d in historial():
        if especificos and d["grupo"] not in especificos:
            continue
        p = parecido(q, set(d["palabras"]))
        if PARECIDO_MINIMO <= p < PARECIDO_MAXIMO:
            puntajes.append((p, d))
    puntajes.sort(key=lambda x: -x[0])
    return [d for _, d in puntajes[:DOCUMENTOS_MAXIMOS]]


# --- Vocabulario del catálogo ---------------------------------------------------------------------------------------
def catalogo():
    """Nombres de equipos y materiales como los registra bodega (activos e insumos), con su marca."""
    terminos = cache.get(CLAVE_CACHE + ":catalogo")
    if terminos is None:
        from inventario.models import CodigoActivo, Insumo

        vistos, terminos = set(), []
        for desc, marca in list(CodigoActivo.objects.values_list("descripcion", "marca")) + \
                list(Insumo.objects.values_list("descripcion", "marca")):
            nombre = " ".join(f"{desc} {marca}".split()) if marca and marca.lower() not in desc.lower() else " ".join(desc.split())
            if nombre and nombre.lower() not in vistos:
                vistos.add(nombre.lower())
                terminos.append(nombre)
        cache.set(CLAVE_CACHE + ":catalogo", terminos, CACHE_SEGUNDOS)
    return terminos


def vocabulario(texto):
    """Los nombres del catálogo que tienen que ver con el texto (comparten palabras), los más cercanos primero."""
    q = palabras(texto)
    if not q:
        return []
    puntajes = []
    for t in catalogo():
        comunes = len(q & palabras(t))
        if comunes:
            puntajes.append((comunes, t))
    puntajes.sort(key=lambda x: -x[0])
    return [t for _, t in puntajes[:TERMINOS_MAXIMOS]]
