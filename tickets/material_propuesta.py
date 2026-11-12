"""El material de la propuesta aprobada dentro del acta de instalación.

Si el ticket tiene una propuesta aprobada por el cliente, el acta trae sus materiales y equipos (sin la mano de obra) y
el técnico indica, de cada uno, cuánto usó realmente y cuánto sobró. Queda en el acta (`MaterialPropuestaActa`), en el
historial del cliente y en el PDF. Lo que sobra sigue en poder del técnico: bodega recibe el aviso para recibirle la
devolución.

Esto no mueve el inventario por sí solo: lo que se descuenta del poder del técnico es lo que marque en «Del inventario
que bodega le entregó».
"""
import re
from decimal import Decimal, InvalidOperation

from .models import MaterialPropuestaActa, Seguimiento

CERO = Decimal(0)


def de_la_propuesta(ticket):
    """[{descripcion, marca, referencia, cantidad}] de la propuesta aprobada del ticket; [] si no tiene."""
    ficha = getattr(ticket, "ficha", None) if ticket else None
    if not ficha:
        return []
    from inventario.proyecto import propuesta_del_proyecto

    cotizacion, _, aprobada = propuesta_del_proyecto(ficha)
    if not (cotizacion and aprobada):
        return []
    filas = []
    for linea in cotizacion.lineas.select_related("item"):
        if linea.es_servicio:
            continue
        filas.append({"descripcion": re.sub(r"\s*\[[^\]]+\]\s*$", "", linea.descripcion)[:300],
                      "marca": (linea.marca or (linea.item.marca if linea.item else ""))[:80],
                      "referencia": (linea.referencia or (linea.item.referencia if linea.item else ""))[:80],
                      "cantidad": linea.cantidad})
    return filas


def _numero(texto):
    texto = (texto or "").replace(",", ".").strip()
    if not texto:
        return CERO
    try:
        valor = Decimal(texto)
    except InvalidOperation:
        return None
    return valor if valor >= 0 else None


def leer(post, lista):
    """Lee «usado» y «sobró» de cada fila del formulario. Devuelve (filas, errores); cada fila trae lo escrito."""
    errores = []
    for i, fila in enumerate(lista):
        fila["usado_texto"] = post.get(f"prop_usado_{i}", "")
        fila["sobra_texto"] = post.get(f"prop_sobra_{i}", "")
        fila["usado"], fila["sobrante"] = _numero(fila["usado_texto"]), _numero(fila["sobra_texto"])
        if fila["usado"] is None or fila["sobrante"] is None:
            errores.append(f"{fila['descripcion']}: escriba cantidades en números (0 o más).")
    return lista, errores


def iniciales(lista):
    """Para el formulario nuevo: por defecto se usó todo lo de la propuesta y no sobró nada."""
    for fila in lista:
        fila["usado_texto"], fila["sobra_texto"] = format(fila["cantidad"].normalize(), "f"), "0"
    return lista


def guardar(acta, lista, usuario):
    """Guarda las filas en el acta y, si sobró algo, avisa a bodega y lo deja en el historial del ticket."""
    sobrantes = []
    for fila in lista:
        m = MaterialPropuestaActa.objects.create(
            acta=acta, descripcion=fila["descripcion"], marca=fila["marca"], referencia=fila["referencia"],
            propuesta=fila["cantidad"], usado=fila["usado"] or CERO, sobrante=fila["sobrante"] or CERO)
        if m.sobrante > 0:
            sobrantes.append(m)
    if sobrantes:
        _avisar_sobrante(acta, sobrantes, usuario)
    return sobrantes


def _avisar_sobrante(acta, sobrantes, usuario):
    from inventario.financiera import _correo, correos_bodega

    tecnico = usuario.get_full_name() or usuario.username
    lineas = [f"- {m.sobrante.normalize():f} × {m.descripcion}" + (f" ({m.referencia})" if m.referencia else "") for m in sobrantes]
    radicado = acta.ticket.radicado if acta.ticket_id else ""
    if acta.ticket_id:
        Seguimiento.objects.create(ticket=acta.ticket, autor=usuario, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                                   mensaje=f"Sobró material de la propuesta (acta N° {acta.numero}); queda en poder de "
                                           f"{tecnico} para devolverlo a bodega:\n" + "\n".join(lineas))
    asunto = f"Material sobrante por recibir · Acta N° {acta.numero}" + (f" · {radicado}" if radicado else "") + f" · {acta.empresa}"
    cuerpo = "\n".join([f"En la instalación de {acta.empresa}" + (f" · {acta.sede}" if acta.sede_id else "")
                        + f" sobró material de la propuesta. Lo tiene {tecnico}:", "", *lineas, "",
                        "Recíbaselo en Inventario → «Recibir devolución» del técnico."])
    _correo(asunto, cuerpo, correos_bodega())
