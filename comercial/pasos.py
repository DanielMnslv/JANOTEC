"""Paso a paso del área comercial en cada requerimiento (como el del área técnica).

El siguiente paso se habilita cuando el anterior está completo: primero el asesor, luego el informe del
requerimiento, la propuesta (la arma el área administrativa y la aprueba dirección), la respuesta del cliente y,
con la propuesta aprobada, el traspaso a instalación.
"""
from .models import FichaComercial

F = FichaComercial
Et = F.Etapa
# (clave, nombre, lo que hay que hacer, quién lo hace, se habilita cuando…)
PASOS = [
    ("asesor", "Asesor", "Elegir el asesor comercial que lleva el requerimiento.", "Comercial", ""),
    ("informe", "Informe del requerimiento", "Registrar qué necesita el cliente, o pedir la visita de levantamiento al "
     "área técnica si hay que verlo en sitio.", "Comercial o técnico", "tenga asesor"),
    ("propuesta", "Propuesta", "Armar la propuesta en el cotizador, enviarla a dirección para aprobación y luego "
     "al cliente.", "Administrativa",
     "esté el informe del requerimiento"),
    ("respuesta", "Respuesta del cliente", "El cliente aprueba, no aprueba o pide cambios (si pide cambios, se "
     "ajusta y se envía otra versión).", "Cliente", "se haya enviado la propuesta"),
    ("traspaso", "Material e instalación", "Bodega revisa el material aprobado y el ticket pasa al área técnica "
     "para la instalación.", "Bodega y comercial", "el cliente apruebe"),
]
# A qué etapas se puede pasar desde cada una (la misma etapa siempre, para guardar sin cambiarla)
SIGUIENTES = {
    Et.GESTION: {Et.ELABORANDO, Et.ENVIADA, Et.CANCELADA},
    Et.ELABORANDO: {Et.GESTION, Et.ENVIADA, Et.CANCELADA},
    Et.ENVIADA: {Et.AJUSTES, Et.APROBADA, Et.RECHAZADA, Et.CANCELADA},
    Et.AJUSTES: {Et.ENVIADA, Et.CANCELADA},
    Et.APROBADA: {Et.CANCELADA},
}
DESPUES = {"propuesta": {Et.ENVIADA, Et.AJUSTES, Et.APROBADA, Et.RECHAZADA, Et.INSTALACION},
           "respuesta": {Et.APROBADA, Et.RECHAZADA, Et.INSTALACION}}


def etapas_permitidas(etapa):
    """Etapas a las que se puede pasar desde la actual (incluida ella misma, para guardar sin cambiarla)."""
    return {etapa} | SIGUIENTES.get(etapa, set())


def calcular(ficha):
    """Los pasos del requerimiento: hecho, actual o por habilitar. None si ya terminó o se canceló."""
    etapa = ficha.etapa
    if etapa == Et.CANCELADA:
        return None
    hecho = {
        "asesor": bool(ficha.comercial_id),
        "informe": ficha.informe_listo,
        "propuesta": etapa in DESPUES["propuesta"] and etapa != Et.AJUSTES,
        "respuesta": etapa in DESPUES["respuesta"],
        "traspaso": etapa == Et.INSTALACION,
    }
    orden = [p[0] for p in PASOS]
    for i, clave in enumerate(orden):  # si ya se cumplió uno posterior, los anteriores quedaron atrás
        if any(hecho[c] for c in orden[i + 1:]):
            hecho[clave] = True
    pasos, actual = [], None
    for n, (clave, nombre, hacer, quien, cuando) in enumerate(PASOS, 1):
        if clave == "propuesta" and etapa == Et.AJUSTES:
            nombre, hacer = "Ajustar la propuesta", "Hacer los cambios que pidió el cliente y enviar la nueva versión."
        es_actual = not hecho[clave] and actual is None
        if es_actual:
            actual = clave
        pasos.append({"numero": n, "clave": clave, "nombre": nombre, "hacer": hacer, "quien": quien, "cuando": cuando,
                      "hecho": hecho[clave], "actual": es_actual, "bloqueado": not hecho[clave] and not es_actual})
    return {"pasos": pasos, "total": len(pasos), "paso": next((p for p in pasos if p["actual"]), None),
            "rechazada": etapa == Et.RECHAZADA, "levantamiento": etapa == Et.LEVANTAMIENTO}
