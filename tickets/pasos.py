"""Paso a paso del área técnica en cada ticket.

Como el acta sin ticket (primero el cliente, luego la sede), el ticket avanza un paso a la vez: el siguiente se
habilita cuando el anterior está completo. Coordinación puede omitir un paso dejando el motivo (queda en el historial).
"""
from django.utils import timezone

from .models import PasoSaltado, Ticket

E = Ticket.Estado
# (clave, nombre, lo que hay que hacer, quién lo hace, se habilita cuando…)
PASOS = [
    ("recepcion", "Recepcionar", "Revisar los datos de la solicitud y marcarla como recepcionada.", "Coordinación", ""),
    ("asignacion", "Asignar técnico", "Elegir el técnico encargado.", "Coordinación", "esté recepcionado"),
    ("programacion", "Programar la visita", "Llevar el ticket al cronograma con fecha, hora y técnicos.", "Coordinación",
     "tenga técnico asignado"),
    ("visita", "Visita y acta", "Ir al sitio y diligenciar el acta de soporte al terminar.", "Técnico",
     "la visita esté programada"),
    ("informe", "Informe", "Generar el borrador del informe desde el acta; coordinación lo aprueba y se entrega.",
     "Técnico y coordinación", "esté el acta de la visita"),
    ("cierre", "Confirmación y cierre", "El cliente confirma que quedó bien; si no responde, se cierra solo.", "Cliente",
     "se haya entregado el trabajo"),
]
# Los que coordinación puede omitir (recepcionar y cerrar tienen su propio botón)
OMITIBLES = {"asignacion", "programacion", "visita", "informe"}
# Mientras está en otra área, el paso a paso técnico espera
EN_OTRA_AREA = {E.EN_PROCESO: "el área comercial", E.PROPUESTA_ENVIADA: "el área comercial", E.EN_INVENTARIO: "bodega"}
DESPUES_DE_LA_VISITA = {E.VISITA_EJECUTADA, E.INFORME, E.CERRADO}


def _hechos(ticket):
    """Qué pasos ya se cumplieron, según lo que de verdad tiene el ticket."""
    from . import etapas

    estado = ticket.estado
    actividades = ticket.actividades.exists() if ticket.pk else False
    nueva_visita = estado == E.PENDIENTE_VISITA  # hay que volver: se programa y se hace otra visita
    hecho = {
        "recepcion": estado != E.GENERADO,
        "asignacion": bool(ticket.asignado_a_id) or actividades,
        "programacion": not nueva_visita and (actividades or estado in (E.EN_CRONOGRAMA, E.AUTORIZADO) or estado in DESPUES_DE_LA_VISITA),
        "visita": not nueva_visita and estado in DESPUES_DE_LA_VISITA,
        "informe": estado == E.CERRADO or (estado == E.VISITA_EJECUTADA and ticket.adjuntos.filter(es_informe=True).exists()),
        "cierre": estado == E.CERRADO,
    }
    if nueva_visita and actividades and ticket.actividades.filter(fecha__gte=timezone.localdate()).exists():
        hecho["programacion"] = True  # ya quedó programada la nueva visita
    return hecho, etapas.lleva_informe(ticket)


def calcular(ticket):
    """Los pasos del ticket con su situación: hecho, actual, omitido o por habilitar."""
    if ticket.area != Ticket.Area.TECNICA or ticket.estado == E.CANCELADO:
        return None
    hecho, lleva_informe = _hechos(ticket)
    ficha = getattr(ticket, "ficha", None)
    levantamiento = bool(ficha and ficha.etapa == "levantamiento")
    omitidos = {p.paso: p for p in (ticket.pasos_saltados.select_related("autor") if ticket.pk else [])}
    # Si ya se cumplió un paso posterior, los anteriores quedaron atrás (tickets que avanzaron antes de existir el paso a paso)
    orden = [p[0] for p in PASOS]
    for i, clave in enumerate(orden):
        if any(hecho[c] for c in orden[i + 1:]):
            hecho[clave] = True
    if not lleva_informe and not levantamiento:
        hecho["informe"] = True
    if ticket.estado in EN_OTRA_AREA:
        return {"pasos": [], "actual": None, "total": 0, "levantamiento": levantamiento, "nueva_visita": False,
                "otra_area": EN_OTRA_AREA[ticket.estado], "paso": None}
    pasos, actual = [], None
    for clave, nombre, hacer, quien, cuando in PASOS:
        if clave == "informe" and not (lleva_informe or levantamiento):
            continue
        if clave == "cierre" and levantamiento:
            nombre, hacer, quien = ("Devolver a comercial", "Enviar el informe del requerimiento y devolver el ticket al "
                                    "área comercial para la propuesta.", "Técnico")
            hecho["cierre"] = False
        omitido = omitidos.get(clave)
        listo = hecho[clave] or omitido is not None
        es_actual = not listo and actual is None
        if es_actual:
            actual = clave
        pasos.append({"clave": clave, "nombre": nombre, "hacer": hacer, "quien": quien, "cuando": cuando,
                      "hecho": hecho[clave], "omitido": omitido, "actual": es_actual,
                      "bloqueado": not listo and not es_actual, "omitible": clave in OMITIBLES})
    for i, p in enumerate(pasos, 1):
        p["numero"] = i
    otra_area = EN_OTRA_AREA.get(ticket.estado)
    return {"pasos": pasos, "actual": actual, "total": len(pasos), "levantamiento": levantamiento,
            "nueva_visita": ticket.estado == E.PENDIENTE_VISITA, "otra_area": otra_area,
            "paso": next((p for p in pasos if p["actual"]), None)}


def listo(ticket, clave):
    """¿Ya se cumplió (u omitió) el paso? Para habilitar lo que sigue."""
    info = calcular(ticket)
    if info is None:
        return True
    p = next((p for p in info["pasos"] if p["clave"] == clave), None)
    return p is None or p["hecho"] or p["omitido"] is not None


def puede_hacer_visita(ticket):
    """El acta se habilita cuando hay técnico y la visita está programada (o coordinación omitió esos pasos)."""
    return listo(ticket, "asignacion") and listo(ticket, "programacion")


def omitir(ticket, clave, motivo, autor):
    from .models import Seguimiento

    nombre = dict((p[0], p[1]) for p in PASOS)[clave]
    PasoSaltado.objects.get_or_create(ticket=ticket, paso=clave, defaults={"motivo": motivo[:300], "autor": autor})
    Seguimiento.objects.create(ticket=ticket, autor=autor, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                               mensaje=f"Omitió el paso «{nombre}». Motivo: {motivo}")
    ticket.save(update_fields=["actualizado"])
