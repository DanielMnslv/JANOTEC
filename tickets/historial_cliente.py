"""Historial de un cliente y su sede, para que el técnico (sobre todo si es nuevo) entienda qué se ha hecho ahí antes
de diligenciar el acta: las visitas anteriores, los equipos y materiales que tiene el cliente y las intervenciones
(tickets) de esa sede.

Junta lo que ya está ligado al cliente en la plataforma:

* **Actas** de la sede (las de la plataforma y las históricas de SafetyTrack).
* **Equipos de JANOTEC instalados** (inventario: activos que están en el cliente).
* **Material entregado** (inventario: insumos que salieron hacia el cliente con un acta).
* **Equipos registrados en las actas** (intervenidos o instalados, también los que son del cliente), sin repetir.
* **Lo instalado de las propuestas** (lo que el técnico marcó como usado en el acta de instalación).
* **Intervenciones**: los tickets de la sede con su tipo y estado.
"""
from collections import OrderedDict
from decimal import Decimal

from django.db.models import Sum

from .models import ActaSoporte, EquipoActa, MaterialPropuestaActa, Ticket


def _de_sede(qs, sede, campo_sede="sede"):
    """El queryset filtrado por la sede (o entero si no hay sede)."""
    if sede is not None:
        return qs.filter(**{campo_sede: sede})
    return qs


def resumen(empresa, sede=None, excluir_ticket=None, limite=8):
    """Todo lo del cliente en esa sede (o en todas, sin sede). Diccionario listo para la plantilla."""
    from inventario.models import Activo, Lugar, MovimientoInsumo

    actas = _de_sede(ActaSoporte.objects.filter(empresa=empresa), sede).select_related("ticket").order_by("-fecha")
    if excluir_ticket is not None:
        actas = actas.exclude(ticket=excluir_ticket)
    actas = list(actas[:limite])

    instalados = OrderedDict()
    for a in _de_sede(Activo.objects.filter(lugar=Lugar.CLIENTE, empresa=empresa), sede).order_by("referencia"):
        clave = (a.referencia or a.descripcion).strip().upper()
        g = instalados.setdefault(clave, {"descripcion": a.descripcion, "referencia": a.referencia, "marca": a.marca,
                                          "unidades": 0, "placas": []})
        g["unidades"] += 1
        if len(g["placas"]) < 6:
            g["placas"].append(a.placa)

    material = (_de_sede(MovimientoInsumo.objects.filter(empresa=empresa, destino=Lugar.CLIENTE), sede)
                .values("insumo__codigo", "insumo__descripcion", "insumo__unidad").annotate(total=Sum("cantidad"))
                .order_by("insumo__descripcion"))

    registrados = OrderedDict()
    equipos = (EquipoActa.objects.filter(acta__empresa=empresa, clase__in=[EquipoActa.Clase.INTERVENIDO, EquipoActa.Clase.UTILIZADO])
               .select_related("acta").order_by("-acta__fecha"))
    if sede is not None:
        equipos = equipos.filter(acta__sede=sede)
    for e in equipos[:300]:
        clave = (e.equipo.strip().lower(), e.referencia.strip().lower(), e.marca.strip().lower())
        if clave not in registrados:
            registrados[clave] = {"equipo": e.equipo, "referencia": e.referencia, "marca": e.marca,
                                  "propiedad": e.get_propiedad_display() if e.propiedad else "",
                                  "ultima": e.acta.fecha, "acta": e.acta, "veces": 0}
        registrados[clave]["veces"] += 1

    de_propuestas = OrderedDict()
    propuesta = MaterialPropuestaActa.objects.filter(acta__empresa=empresa, usado__gt=0).select_related("acta")
    if sede is not None:
        propuesta = propuesta.filter(acta__sede=sede)
    for m in propuesta.order_by("-acta__fecha"):
        g = de_propuestas.setdefault((m.descripcion.lower(), m.referencia.lower()),
                                     {"descripcion": m.descripcion, "referencia": m.referencia, "marca": m.marca,
                                      "usado": Decimal(0), "acta": m.acta})
        g["usado"] += m.usado

    tickets = _de_sede(Ticket.objects.filter(empresa=empresa), sede).select_related("categoria").order_by("-creado")
    if excluir_ticket is not None:
        tickets = tickets.exclude(pk=excluir_ticket.pk)
    tickets = list(tickets[:limite])

    return {
        "empresa": empresa, "sede": sede, "actas": actas, "instalados": list(instalados.values()),
        "material": list(material), "registrados": list(registrados.values())[:40],
        "de_propuestas": list(de_propuestas.values()), "tickets": tickets,
        "vacio": not (actas or instalados or material or registrados or de_propuestas or tickets),
    }
