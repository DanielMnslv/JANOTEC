"""Cotizador: arma la propuesta económica con el catálogo de precios, calcula descuento e IVA y genera el PDF.

Cada versión de la propuesta tiene su cotización. Mientras no se envía es un borrador que el asesor ajusta;
al enviarla queda fija (con su PDF y su código PE-AAAA-MM-DD-Vn) y, si el cliente pide cambios, la siguiente
versión arranca como copia de la anterior.
"""
import re
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from tickets.models import Ticket

from . import servicios
from .models import Cotizacion, FichaComercial as F, ItemCatalogo, LineaCotizacion, PropuestaVersion

ETAPAS_COTIZABLES = (F.Etapa.GESTION, F.Etapa.ELABORANDO, F.Etapa.AJUSTES)
MARGEN_OBLIGATORIO = "Digite el margen de utilidad de esta propuesta, de 1 % a 99 %."


def borrador(ficha):
    """La cotización que se está armando. Si no hay, arranca de la última enviada (cambios del cliente) o vacía."""
    actual = ficha.cotizaciones.filter(version__isnull=True).order_by("-creado").first()
    if actual:
        return actual
    anterior = ficha.cotizaciones.filter(version__isnull=False).order_by("-version__numero").first()
    with transaction.atomic():
        if anterior:
            nueva = Cotizacion.objects.create(
                ficha=ficha, alcance=anterior.alcance, condiciones=anterior.condiciones, modalidad=anterior.modalidad,
                margen=anterior.margen,
                aiu_utilidad=anterior.aiu_utilidad, tasa_mensual=anterior.tasa_mensual, plazos=anterior.plazos,
                recargo_alquiler=anterior.recargo_alquiler,
                opcion_compra=anterior.opcion_compra)
            for linea in anterior.lineas.all():
                linea.pk, linea.cotizacion = None, nueva
                linea.save()
            return nueva
        informe = getattr(ficha, "informe", None) if ficha.informe_listo else None
        alcance = "\n".join(x for x in ((informe.solucion or informe.necesidad) if informe else ficha.ticket.asunto,) if x)
        return Cotizacion.objects.create(ficha=ficha, alcance=alcance)


def _palabras(texto):
    # Las medidas cuentan («15 m» no es «10 m»); las palabras cortas («de», «en») no
    return {p for p in re.findall(r"[a-záéíóúñ0-9]+", texto.lower()) if len(p) > 2 or p.isdigit()}


def _buscar_en_catalogo(texto):
    """El ítem del catálogo que mejor coincide con lo escrito en el informe (por las palabras que comparten)."""
    buscadas = _palabras(texto)
    mejor, puntaje = None, (0, 0)
    for item in ItemCatalogo.objects.filter(activo=True):
        propias = _palabras(item.descripcion)
        comunes = len(buscadas & propias)
        # A igual coincidencia gana el ítem con menos palabras de sobra
        candidato = (comunes, -len(propias - buscadas))
        if comunes and candidato > puntaje:
            mejor, puntaje = item, candidato
    return mejor if puntaje[0] >= 2 else None


def traer_del_informe(cotizacion):
    """Agrega como ítems los servicios y los materiales y equipos del informe (con precio si están en el catálogo)."""
    ficha = cotizacion.ficha
    if not ficha.informe_listo:
        return 0
    agregadas = 0
    orden = cotizacion.lineas.count()
    informe = ficha.informe
    for linea in (informe.servicios + "\n" + informe.materiales).splitlines():
        linea = linea.strip(" -•*\t")
        if not linea:
            continue
        m = re.match(r"^(\d+(?:[.,]\d+)?)\s*(?:m\b|und\b|unidades?\b)?\s*(.+)$", linea, re.I)
        cantidad, descripcion = (Decimal(m.group(1).replace(",", ".")), m.group(2).strip()) if m else (Decimal(1), linea)
        item = _buscar_en_catalogo(descripcion)
        orden += 1
        LineaCotizacion.objects.create(
            cotizacion=cotizacion, item=item, descripcion=(item.descripcion if item else descripcion)[:250], cantidad=cantidad,
            valor_unitario=item.valor_unitario if item else Decimal(0), iva=item.iva if item else Decimal(19), orden=orden)
        agregadas += 1
    return agregadas


def codigo(ficha, numero):
    """PE-AAAA-MM-DD-Vn: la fecha es la de la primera versión, como en las propuestas en papel.
    Si otra propuesta ya usa ese día, se le agrega una letra (PE-2026-07-11-B-V1) para que no se repita."""
    primera = ficha.versiones.exclude(codigo="").order_by("numero").first()
    if primera:
        return f"{primera.codigo.rsplit('-V', 1)[0]}-V{numero}"
    base = f"PE-{timezone.localdate():%Y-%m-%d}"
    usadas = PropuestaVersion.objects.filter(codigo__startswith=base).exclude(ficha=ficha).values("ficha").distinct().count()
    if usadas:
        base = f"{base}-{chr(ord('A') + usadas)}"
    return f"{base}-V{numero}"


def enviar(cotizacion, autor):
    """Genera el PDF, crea la versión de la propuesta y se la envía al cliente (con los botones para responder)."""
    from .pdf import propuesta_pdf

    ficha = cotizacion.ficha
    t = ficha.ticket
    if cotizacion.enviada:
        raise ValidationError("Esta cotización ya se envió.")
    if t.area != Ticket.Area.COMERCIAL or ficha.etapa not in ETAPAS_COTIZABLES:
        raise ValidationError("La propuesta se envía mientras el requerimiento está en el área comercial elaborándola.")
    if not ficha.informe_listo:
        raise ValidationError("Primero registre el informe del requerimiento: es la base de la propuesta.")
    lineas = list(cotizacion.lineas.all())
    if not lineas or any(l.valor_unitario <= 0 or l.cantidad <= 0 for l in lineas):
        raise ValidationError("Cada ítem necesita cantidad y costo unitario.")
    if not cotizacion.margen:
        raise ValidationError(MARGEN_OBLIGATORIO)
    totales = cotizacion.totales()
    numero = (ficha.versiones.order_by("-numero").values_list("numero", flat=True).first() or 0) + 1
    cod = codigo(ficha, numero)
    nombre, contenido = propuesta_pdf(cotizacion, cod, numero)
    with transaction.atomic():
        if not ficha.comercial_id and getattr(autor, "es_comercial", False):
            ficha.comercial = autor
        ficha.valor_propuesta = totales["base"].quantize(Decimal(1))
        ficha.etapa, ficha.fecha_envio = F.Etapa.ENVIADA, timezone.now()
        ficha.save()
        if t.estado not in Ticket.ESTADOS_FINALES:
            t.estado = Ticket.Estado.PROPUESTA_ENVIADA
            t.save(update_fields=["estado", "actualizado"])
        seg = servicios._seguimiento(t, autor, f"Propuesta económica {cod} por ${ficha.valor_propuesta:,.0f} antes de IVA "
                                               f"(${totales['total']:,.0f} con IVA)".replace(",", ".")
                                       + ("" if cotizacion.modalidad == cotizacion.Modalidad.VENTA
                                          else f" · {cotizacion.get_modalidad_display()}") + ". Puede descargarla aquí.")
        servicios.guardar_adjuntos(t, [ContentFile(contenido, name=nombre)], autor, seg, es_propuesta=True)
        pdf = t.adjuntos.filter(es_propuesta=True).order_by("-pk").first()
        version = servicios.enviar_version(ficha, autor, pdf)
        version.codigo = cod
        version.save(update_fields=["codigo"])
        cotizacion.version = version
        cotizacion.save(update_fields=["version", "actualizado"])
    servicios.propuesta_enviada(ficha, pdf)
    return version
