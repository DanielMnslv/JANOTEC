"""Etapas del servicio que ve el cliente (las mismas de janotecsas.netlify.app).

Solicitud recibida → Visita programada → Inspección realizada → Informe en elaboración → Propuesta enviada → Servicio cerrado

La etapa actual sale del estado del ticket; las fechas reales, de lo que ya pasó (visita, acta, propuesta, cierre).
El área técnica agrega a cada etapa una fecha estimada y una nota visible al cliente (EtapaCliente).
Informe y propuesta solo aparecen en tomas de requerimiento, diagnósticos y solicitudes comerciales.
"""
from django.utils import timezone

from .models import EtapaCliente, Ticket

E = Ticket.Estado
ETAPAS = [("recibida", "Solicitud recibida"), ("programada", "Visita programada"), ("inspeccion", "Inspección realizada"),
          ("informe", "Informe en elaboración"), ("propuesta", "Propuesta enviada"), ("cierre", "Servicio cerrado")]
SISTEMA_DIAGNOSTICO = "Diagnóstico técnico"


def _ficha(ticket):
    """La ficha comercial del ticket, si la tiene."""
    return getattr(ticket, "ficha", None) if ticket.pk else None


def lleva_informe(ticket):
    """Si el cliente debe ver la etapa «Informe en elaboración» (requerimientos, diagnósticos y comerciales)."""
    return bool(ticket.tipo == Ticket.Tipo.REQUERIMIENTO or _ficha(ticket) or ticket.estado == E.INFORME
                or (ticket.categoria_id and ticket.categoria.nombre == SISTEMA_DIAGNOSTICO))


def lleva_propuesta(ticket):
    """Si el cliente debe ver la etapa «Propuesta enviada» (tickets con ficha comercial o requerimientos)."""
    return bool(_ficha(ticket) or ticket.tipo == Ticket.Tipo.REQUERIMIENTO)


def claves_de(ticket):
    """Las etapas que aplican a este ticket, en orden."""
    claves = ["recibida", "programada", "inspeccion"]
    if lleva_informe(ticket):
        claves.append("informe")
    if lleva_propuesta(ticket):
        claves.append("propuesta")
    return claves + ["cierre"]


def etapa_actual(ticket, claves):
    """En qué etapa está el ticket según su estado y la etapa de la ficha comercial."""
    ficha = _ficha(ticket)
    if ticket.estado in (E.CERRADO, E.CANCELADO):
        return "cierre"
    if ficha and ficha.etapa in ("elaborando", "enviada", "aprobada", "rechazada", "instalacion"):
        return "propuesta"
    if ticket.estado == E.INFORME:
        return "informe" if "informe" in claves else "inspeccion"
    if ticket.estado == E.VISITA_EJECUTADA:
        return "inspeccion"
    if ticket.estado in (E.EN_CRONOGRAMA, E.AUTORIZADO):
        return "programada"
    return "recibida"


def _fecha_real(ticket, clave):
    """Cuándo se cumplió de verdad esa etapa (radicación, visita, acta, envío de la propuesta o cierre)."""
    ficha = _ficha(ticket)
    if clave == "recibida":
        return timezone.localtime(ticket.creado).date() if ticket.creado else None
    if clave == "programada" and ticket.visita_programada:
        return timezone.localtime(ticket.visita_programada).date()
    if clave == "inspeccion":
        acta = ticket.actas.order_by("-fecha").first() if ticket.pk else None
        return timezone.localtime(acta.fecha).date() if acta else None
    if clave == "informe":
        informe = ticket.adjuntos.filter(es_informe=True).order_by("-creado").first() if ticket.pk else None
        return timezone.localtime(informe.creado).date() if informe else None
    if clave == "propuesta" and ficha and ficha.fecha_envio:
        return timezone.localtime(ficha.fecha_envio).date()
    if clave == "cierre" and ticket.fecha_cierre:
        return timezone.localtime(ticket.fecha_cierre).date()
    return None


def etapas_cliente(ticket):
    """[{clave, nombre, estado: hecha|actual|pendiente, fecha, estimada, nota}] para mostrar al cliente."""
    claves = claves_de(ticket)
    actual = etapa_actual(ticket, claves)
    indice = claves.index(actual)
    cerrado = ticket.estado in (E.CERRADO, E.CANCELADO)
    guardadas = {e.clave: e for e in ticket.etapas_cliente.all()} if ticket.pk else {}
    nombres = dict(ETAPAS)
    etapas = []
    for i, clave in enumerate(claves):
        estado = "hecha" if i < indice or (cerrado and i == indice) else ("actual" if i == indice else "pendiente")
        guardada = guardadas.get(clave)
        real = _fecha_real(ticket, clave) if estado != "pendiente" else None
        fecha = real or (guardada.fecha if guardada else None)
        etapas.append({"clave": clave, "nombre": nombres[clave], "estado": estado, "fecha": fecha,
                       "estimada": bool(fecha and not real), "nota": guardada.nota if guardada else ""})
    if ticket.estado == E.CANCELADO:
        etapas[-1]["nombre"] = "Solicitud cancelada"
    return etapas


def guardar_etapas(ticket, datos):
    """Guarda las fechas estimadas y notas del editor (`fecha_<clave>`, `nota_<clave>`). Devuelve cuántas cambiaron."""
    from datetime import date

    cambiadas = 0
    for clave in claves_de(ticket):
        texto = (datos.get(f"fecha_{clave}") or "").strip()
        try:
            fecha = date.fromisoformat(texto) if texto else None
        except ValueError:
            fecha = None
        nota = (datos.get(f"nota_{clave}") or "").strip()[:300]
        actual = EtapaCliente.objects.filter(ticket=ticket, clave=clave).first()
        if actual and (actual.fecha, actual.nota) == (fecha, nota) or not actual and not fecha and not nota:
            continue
        EtapaCliente.objects.update_or_create(ticket=ticket, clave=clave, defaults={"fecha": fecha, "nota": nota})
        cambiadas += 1
    return cambiadas
