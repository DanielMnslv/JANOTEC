"""«Mi día»: la pantalla de inicio de cada área con solo lo que le toca resolver.

Cada área ve sus pendientes agrupados («Por recepcionar», «Acta por hacer», «Por pagar»…) y cada pendiente trae el
botón del siguiente paso. Dirección ve un solo grupo, «Por aprobar»: informes, propuestas, órdenes de compra y
salidas de material. Aquí quedaron también las reglas útiles de la antigua «Salud de los procesos» (visitas pasadas
sin acta, material detenido, requerimientos sin movimiento y autorizaciones sin respuesta), cada una en la lista del
área que la resuelve.

Mientras el superusuario actúa como un usuario de prueba, solo se listan tickets y órdenes de PRUEBA.
"""
from datetime import timedelta

from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from accounts.prueba import es_de_prueba

from .models import ActaSoporte, InformeBorrador, Ticket

E = Ticket.Estado


# --- Piezas ------------------------------------------------------------------------------
def _item(titulo, detalle="", url="", boton="Abrir", urgente=False, ticket=None):
    """Un pendiente: qué es, un detalle corto y el botón del siguiente paso."""
    return {"titulo": titulo, "detalle": detalle, "url": url, "boton": boton, "urgente": urgente, "ticket": ticket}


def _grupo(clave, titulo, icono, items, ayuda="", ver_todo=""):
    """Un grupo de pendientes con su icono, la explicación de qué hacer y el enlace a la bandeja completa."""
    return {"clave": clave, "titulo": titulo, "icono": icono, "items": items, "ayuda": ayuda, "ver_todo": ver_todo,
            "urgentes": sum(1 for i in items if i["urgente"])}


def _ficha(ticket):
    """Enlace a la ficha del ticket."""
    return reverse("ticket_detalle", args=[ticket.radicado])


def _cliente(ticket):
    """Nombre corto del cliente del ticket."""
    return ticket.empresa.razon_social if ticket.empresa_id else "Sin cliente"


def _dias(desde):
    """Días transcurridos desde una fecha u hora."""
    if desde is None:
        return 0
    if hasattr(desde, "date"):
        desde = timezone.localtime(desde).date()
    return (timezone.localdate() - desde).days


def _hace(desde):
    """«hoy», «hace 1 día», «hace 5 días»."""
    d = _dias(desde)
    return "hoy" if d <= 0 else ("hace 1 día" if d == 1 else f"hace {d} días")


class _Filtro:
    """Restringe las consultas a lo de PRUEBA cuando quien mira es un usuario de prueba."""

    def __init__(self, usuario):
        self.prueba = es_de_prueba(usuario)

    def tickets(self, qs, campo=""):
        """Tickets (o lo que cuelga de un ticket, con ``campo`` como «ticket__»)."""
        return qs.filter(**{f"{campo}empresa__razon_social__istartswith": "PRUEBA"}) if self.prueba else qs

    def ordenes(self, qs):
        """Órdenes de compra: las de un proveedor PRUEBA."""
        return qs.filter(proveedor__razon_social__istartswith="PRUEBA") if self.prueba else qs


# --- Dirección: un solo «Por aprobar» ------------------------------------------------------
def por_aprobar(usuario):
    """Lo que espera la firma de dirección: informes, propuestas, órdenes de compra y salidas de material."""
    from comercial.models import Cotizacion
    from inventario.models import FirmaSolicitud, OrdenCompra, SolicitudMaterial

    f = _Filtro(usuario)
    items = []
    for i in f.tickets(InformeBorrador.objects.filter(estado=InformeBorrador.Estado.REVISION), "ticket__").select_related(
            "ticket__empresa").order_by("actualizado"):
        items.append(_item(f"Informe {i.ticket.radicado} · {_cliente(i.ticket)}",
                           f"{i.get_tipo_display().split(' (')[0]}. Enviado {_hace(i.actualizado)}.",
                           reverse("informe_borrador", args=[i.ticket.radicado]), "Revisar informe", _dias(i.actualizado) >= 2, ticket=i.ticket))
    for c in f.tickets(Cotizacion.objects.filter(en_revision=True), "ficha__ticket__").select_related(
            "ficha__ticket__empresa").order_by("actualizado"):
        t = c.ficha.ticket
        items.append(_item(f"Propuesta {t.radicado} · {_cliente(t)}", "En revisión de dirección.",
                           reverse("comercial:cotizador", args=[t.radicado]), "Revisar propuesta", c.ficha.prioritaria, ticket=t))
    for o in f.ordenes(OrdenCompra.objects.filter(estado=OrdenCompra.Estado.REVISION)).select_related("proveedor").order_by("pk"):
        items.append(_item(f"Orden de compra {o.numero} · {o.proveedor or 'sin proveedor'}",
                           f"{o.get_forma_pago_display().split(' (')[0]}.", reverse("inventario:orden", args=[o.pk]),
                           "Revisar orden", False))
    autorizadas = FirmaSolicitud.objects.filter(rol=FirmaSolicitud.Rol.DIRECCION, aprobada=True)
    for s in f.tickets(SolicitudMaterial.objects.filter(estado=SolicitudMaterial.Estado.APROBACION), "ticket__").select_related(
            "ticket__empresa").order_by("actualizado"):
        if autorizadas.filter(solicitud=s, ronda=s.ronda).exists():
            continue  # ya autorizó dirección o el coordinador; falta la firma de bodega
        items.append(_item(f"Salida de material {s.numero} · {s.ticket.radicado}", f"{_cliente(s.ticket)}. Esperando {_hace(s.actualizado)}.",
                           reverse("inventario:solicitud", args=[s.pk]), "Autorizar salida", _dias(s.actualizado) >= 2, ticket=s.ticket))
    return _grupo("por_aprobar", "Por aprobar", "bi-patch-check", items,
                  "Lo que espera su firma. Abra cada uno, revíselo y apruébelo o devuélvalo con sus observaciones.")


# --- Coordinación técnica ------------------------------------------------------------------
def coordinacion(usuario):
    """Por recepcionar, por programar, visitas pasadas sin acta, autorizaciones del cliente y salidas por autorizar."""
    from cronograma.models import Actividad

    f = _Filtro(usuario)
    tecnicos = f.tickets(Ticket.objects.filter(area=Ticket.Area.TECNICA)).select_related("empresa")
    grupos = []
    items = [_item(f"{t.radicado} · {_cliente(t)}", f"Radicado {_hace(t.creado)}.",
                   reverse("recepcionar", args=[t.radicado]), "Recepcionar", t.prioridad in ("alta", "critica") or _dias(t.creado) >= 1, ticket=t)
             for t in tecnicos.filter(estado=E.GENERADO).order_by("creado")]
    grupos.append(_grupo("recepcionar", "Por recepcionar", "bi-inbox", items,
                         "Tickets nuevos: asigne técnico, prioridad e impacto.", reverse("panel") + "?estado=generado"))
    items = [_item(f"{t.radicado} · {_cliente(t)}", f"{t.get_estado_display()} {_hace(t.actualizado)}"
                   f"{' · ' + str(t.asignado_a) if t.asignado_a_id else ' · sin técnico'}.",
                   reverse("cronograma:semana"), "Programar visita", _dias(t.actualizado) >= 3, ticket=t)
             for t in tecnicos.filter(estado__in=Ticket.ESTADOS_PROGRAMABLES).select_related("asignado_a").order_by("actualizado")]
    grupos.append(_grupo("programar", "Por programar", "bi-calendar-plus", items,
                         "Recepcionados o pendientes de nueva visita: póngalos en el cronograma."))
    grupos.append(_grupo("sin_acta", "Visitas pasadas sin acta", "bi-clipboard-x", _visitas_sin_acta(usuario),
                         "La fecha de la visita ya pasó y nadie diligenció el acta. Si no se hizo, aplácela o reprográmela."))
    grupos.append(_grupo("informes", "Informes técnicos", "bi-file-earmark-text", _informes_coordinacion(usuario),
                         "Genere el borrador, revíselo y envíelo a dirección. Cuando dirección lo apruebe, entréguelo."))
    hoy, ahora = timezone.localdate(), timezone.now()
    A = Actividad.Autorizacion
    items = []
    for a in (f.tickets(Actividad.objects.filter(fecha__gte=hoy, ticket__isnull=False), "ticket__")
              .exclude(ticket__empresa__es_del_grupo=True).select_related("ticket__empresa").order_by("fecha", "hora_inicio")):
        cuando = f"{a.fecha:%d/%m} {a.hora_inicio:%H:%M}"
        url = reverse("cronograma:semana") + f"?d={a.fecha:%Y-%m-%d}"
        if a.autorizacion == A.SIN_ENVIAR and a.fecha <= hoy + timedelta(days=2):
            items.append(_item(f"{a.ticket.radicado} · visita del {cuando}", "Sin autorización enviada al cliente.", url,
                               "Enviar autorización", True, ticket=a.ticket))
        elif a.autorizacion == A.ENVIADA and a.autorizacion_enviada and a.autorizacion_enviada < ahora - timedelta(hours=48):
            items.append(_item(f"{a.ticket.radicado} · visita del {cuando}",
                               f"El cliente no responde desde el {timezone.localtime(a.autorizacion_enviada):%d/%m}. Conviene llamarlo.",
                               url, "Ver visita", ticket=a.ticket))
        elif a.autorizacion == A.RECHAZADA:
            items.append(_item(f"{a.ticket.radicado} · visita del {cuando}",
                               f"El cliente la rechazó: {a.motivo_rechazo or 'sin motivo'}. Reprográmela o aplácela.", url,
                               "Reprogramar", True, ticket=a.ticket))
    grupos.append(_grupo("autorizaciones", "Autorizaciones del cliente", "bi-envelope-exclamation", items,
                         "Visitas próximas sin autorización enviada, sin respuesta en 48 horas o rechazadas."))
    if not usuario.es_direccion:  # dirección ya las ve en «Por aprobar»
        salidas = [i for i in por_aprobar(usuario)["items"] if i["titulo"].startswith("Salida de material")]
        grupos.append(_grupo("salidas", "Salidas de material por autorizar", "bi-box-arrow-right", salidas,
                             "Bodega ya validó: falta su firma (o la de dirección) para que el material salga."))
    grupos.append(_grupo("horas", "Horas extras por validar", "bi-shield-exclamation", _horas_por_validar(),
                         "Registradas tarde, sin ubicación, lejos del sitio o por otra persona: apruébelas o recházelas.",
                         ver_todo=reverse("cronograma:horas_extras")))
    return grupos


def _horas_por_validar():
    """Los reportes de horas extras cerrados que esperan la validación de la coordinación."""
    from cronograma import horas_validacion as hv
    from cronograma.models import ReporteHorasExtra

    items = []
    url = reverse("cronograma:horas_extras")
    for r in (ReporteHorasExtra.objects.filter(validacion=ReporteHorasExtra.Validacion.POR_VALIDAR, hora_llegada__isnull=False)
              .select_related("colaborador", "registrado_por", "cerrado_por").order_by("fecha")[:20]):
        alertas = hv.alertas(r)
        items.append(_item(f"{r.colaborador.get_full_name() or r.colaborador.username} · {r.fecha:%d/%m} · {r.duracion} h",
                           "; ".join(alertas)[:160], f"{url}?mes={r.fecha:%Y-%m}", "Validar"))
    return items


def _informes_coordinacion(usuario):
    """Cada informe en elaboración con el paso que le toca a coordinación.

    Sin borrador → generarlo; borrador (o devuelto por dirección) → revisarlo y enviarlo a dirección;
    en revisión → solo se informa que está con dirección; aprobado → entregarlo.
    """
    from . import informe as inf

    items = []
    for t in (_Filtro(usuario).tickets(Ticket.objects.filter(estado=E.INFORME)).select_related("empresa")
              .order_by("actualizado")):
        borrador = inf.vigente(t)
        titulo = f"{t.radicado} · {_cliente(t)}"
        url = reverse("informe_borrador", args=[t.radicado])
        if borrador is None:
            items.append(_item(titulo, "Acta lista: falta generar el borrador del informe.",
                               _ficha(t), "Generar borrador", _dias(t.actualizado) >= 2, ticket=t))
        elif borrador.aprobado:
            quien = borrador.aprobado_por.get_full_name() if borrador.aprobado_por else "dirección"
            items.append(_item(titulo, f"Aprobado por {quien} {_hace(borrador.aprobado_en)}: ya puede entregarlo.",
                               _ficha(t), "Entregar informe", True, ticket=t))
        elif borrador.en_revision:
            items.append(_item(titulo, f"En revisión de dirección desde {_hace(borrador.actualizado)}.", url, "Ver informe", ticket=t))
        else:
            devuelto = t.seguimientos.filter(mensaje__startswith="Devolvió el informe técnico para corregir").filter(
                creado__gte=borrador.actualizado).exists()
            items.append(_item(titulo, ("Dirección lo devolvió con observaciones: corríjalo y envíelo de nuevo."
                                        if devuelto else "Borrador listo: revíselo y envíelo a dirección para aprobar."),
                               url, "Revisar y enviar a dirección", devuelto or _dias(borrador.actualizado) >= 2, ticket=t))
    return items


def _visitas_sin_acta(usuario, tecnico=None):
    """Visitas de los últimos 30 días cuya fecha pasó sin acta (de un técnico, si se indica)."""
    from cronograma.models import Actividad

    hoy = timezone.localdate()
    qs = _Filtro(usuario).tickets(Actividad.objects.filter(
        fecha__lt=hoy, fecha__gte=hoy - timedelta(days=30), ticket__estado__in=[E.EN_CRONOGRAMA, E.AUTORIZADO]), "ticket__")
    if tecnico is not None:
        qs = qs.filter(encargados=tecnico)
    items = []
    for a in qs.select_related("ticket__empresa").prefetch_related("encargados").order_by("fecha"):
        if ActaSoporte.objects.filter(ticket=a.ticket, fecha__date__gte=a.fecha).exists():
            continue
        quien = ", ".join(str(u) for u in a.encargados.all()) or "sin técnico"
        items.append(_item(f"{a.ticket.radicado} · {_cliente(a.ticket)}", f"Visita del {a.fecha:%d/%m} · {quien}.",
                           reverse("acta_nueva", args=[a.ticket.radicado]), "Hacer el acta", True, ticket=a.ticket))
    return items


# --- Técnico -------------------------------------------------------------------------------
def tecnico(usuario):
    """Mis visitas de hoy y de la semana, actas por hacer y material por recibir."""
    from cronograma.models import Actividad
    from inventario.models import ActaEntrega

    hoy = timezone.localdate()
    visitas = (Actividad.objects.filter(encargados=usuario, fecha__gte=hoy, fecha__lte=hoy + timedelta(days=6))
               .select_related("ticket__empresa").order_by("fecha", "hora_inicio"))
    items = []
    for a in visitas:
        dia = "Hoy" if a.fecha == hoy else ("Mañana" if a.fecha == hoy + timedelta(days=1) else f"{a.fecha:%d/%m}")
        titulo = f"{dia} {a.hora_inicio:%H:%M} · {a.ticket.radicado if a.ticket_id else a.cliente}"
        detalle = f"{a.cliente}{' · ' + a.sede if a.sede else ''} · {a.actividad[:90]}"
        if a.ticket_id:
            items.append(_item(titulo, detalle, _ficha(a.ticket), "Ver ticket", a.fecha == hoy, ticket=a.ticket))
        else:
            items.append(_item(titulo, detalle, reverse("cronograma:semana") + f"?d={a.fecha:%Y-%m-%d}", "Ver cronograma", a.fecha == hoy, ticket=a.ticket))
    grupos = [_grupo("visitas", "Mis visitas de la semana", "bi-geo-alt", items,
                     "Lo que tiene programado desde hoy hasta dentro de 6 días.", reverse("cronograma:semana")),
              _grupo("actas", "Acta por hacer", "bi-clipboard-check", _visitas_sin_acta(usuario, tecnico=usuario),
                     "Visitas que ya hizo y aún no tienen acta.")]
    actas = ActaEntrega.objects.filter(tecnico=usuario, recibido_en__isnull=True).select_related("solicitud__ticket")
    items = [_item(f"{a.numero} · {a.solicitud.ticket.radicado}", f"Bodega le entregó material {_hace(a.creado)}.",
                   reverse("inventario:mi_material"), "Confirmar recibido", True, ticket=a.solicitud.ticket) for a in actas]
    grupos.append(_grupo("material", "Material por recibir", "bi-box-seam", items,
                         "Confirme en la plataforma que recibió el material que le entregó bodega."))
    return grupos


# --- Comercial -----------------------------------------------------------------------------
def comercial(usuario):
    """Propuestas por entregar, requerimientos sin movimiento y propuestas que esperan respuesta del cliente."""
    from comercial.models import FichaComercial as F
    from comercial.models import PropuestaVersion as V

    f = _Filtro(usuario)
    ahora = timezone.now()
    mias = Q(comercial=usuario) | Q(comercial__isnull=True)
    fichas = f.tickets(F.objects.filter(mias), "ticket__").select_related("ticket__empresa")
    items = [_item(f"{v.ficha.ticket.radicado} · {_cliente(v.ficha.ticket)}", f"Propuesta v{v.numero} aprobada por dirección para que usted la entregue.",
                   reverse("comercial:detalle", args=[v.ficha.ticket.radicado]), "Registrar entrega", True, ticket=v.ficha.ticket)
             for v in V.objects.filter(ficha__in=fichas, por_comercial=True, entregada_cliente__isnull=True, estado=V.Estado.ENVIADA)
             .select_related("ficha__ticket__empresa")]
    grupos = [_grupo("entregar", "Propuestas por entregar", "bi-hand-index", items,
                     "Dirección las aprobó para entrega en persona: llévelas y registre la fecha.")]
    items = []
    for fi in fichas.filter(etapa__in=[F.Etapa.GESTION, F.Etapa.LEVANTAMIENTO], ticket__area=Ticket.Area.COMERCIAL).order_by("actualizado"):
        if not fi.comercial_id:
            items.append(_item(f"{fi.ticket.radicado} · {_cliente(fi.ticket)}", "Nadie del área comercial lo ha tomado.",
                               reverse("comercial:detalle", args=[fi.ticket.radicado]), "Tomarlo", True, ticket=fi.ticket))
        elif fi.actualizado < ahora - timedelta(days=5):
            items.append(_item(f"{fi.ticket.radicado} · {_cliente(fi.ticket)}",
                               f"{_hace(fi.actualizado).capitalize()} sin movimiento en «{fi.get_etapa_display()}».",
                               reverse("comercial:detalle", args=[fi.ticket.radicado]), "Darle movimiento", True, ticket=fi.ticket))
    grupos.append(_grupo("mover", "Requerimientos por mover", "bi-arrow-repeat", items,
                         "Sin asesor o con más de 5 días quietos.", reverse("comercial:bandeja")))
    items = []
    for fi in fichas.filter(etapa=F.Etapa.ENVIADA).order_by("actualizado"):
        v = fi.versiones.order_by("-numero").first()
        if v and v.estado == V.Estado.ENVIADA and not v.por_entregar and v.enviada < ahora - timedelta(days=3):
            items.append(_item(f"{fi.ticket.radicado} · {_cliente(fi.ticket)}", f"Propuesta v{v.numero} enviada {_hace(v.enviada)} sin respuesta.",
                               reverse("comercial:detalle", args=[fi.ticket.radicado]), "Registrar respuesta", _dias(v.enviada) >= 8, ticket=fi.ticket))
    grupos.append(_grupo("respuestas", "Esperando respuesta del cliente", "bi-telephone-outbound", items,
                         "Propuestas con más de 3 días sin respuesta: llame al cliente y registre lo que diga."))
    return grupos


# --- Administrativa ------------------------------------------------------------------------
def administrativa(usuario):
    """Propuestas devueltas por dirección, con cambios del cliente y por armar; y las órdenes de compra por revisar."""
    from comercial.models import FichaComercial
    from comercial.views import clasificar_propuestas

    f = _Filtro(usuario)
    base = f.tickets(FichaComercial.objects.all(), "ticket__").select_related("ticket__empresa").order_by("fecha_max_propuesta", "creado")
    g = clasificar_propuestas(base)

    def fila(fi, detalle, boton):
        return _item(f"{fi.ticket.radicado} · {_cliente(fi.ticket)}", detalle,
                     reverse("comercial:cotizador", args=[fi.ticket.radicado]), boton, fi.prioritaria, ticket=fi.ticket)

    grupos = [
        _grupo("devueltas", "Devueltas por dirección", "bi-arrow-return-left",
               [fila(fi, f"Observaciones: {fi.observaciones[:140]}", "Corregir") for fi in g["devueltas"]],
               "Corrija lo que pidió dirección y vuelva a enviarla."),
        _grupo("cambios", "Cambios del cliente", "bi-pencil-square",
               [fila(fi, "El cliente pidió cambios: arme la nueva versión.", "Ajustar propuesta") for fi in g["cambios"]]),
        _grupo("armar", "Propuestas por armar", "bi-calculator",
               [fila(fi, f"Informe listo. {'Para el ' + format(fi.fecha_max_propuesta, '%d/%m') if fi.fecha_max_propuesta else 'Sin fecha límite'}.",
                     "Armar propuesta") for fi in g["por_armar"]],
               "El informe de requerimientos ya está: arme la propuesta en el cotizador.", reverse("comercial:propuestas")),
    ]
    if usuario.revisa_compras:
        grupos.append(revision_compras(usuario))
    return grupos


def revision_compras(usuario):
    """Órdenes de compra en revisión administrativa (antes de pasar a dirección)."""
    from inventario.models import OrdenCompra

    qs = _Filtro(usuario).ordenes(OrdenCompra.objects.filter(estado=OrdenCompra.Estado.POR_REVISAR)).select_related("proveedor")
    items = [_item(f"{o.numero} · {o.proveedor or 'sin proveedor'}", "Revise precios y cantidades antes de que pase a dirección.",
                   reverse("inventario:orden", args=[o.pk]), "Revisar orden") for o in qs.order_by("pk")]
    return _grupo("revisar_oc", "Órdenes de compra por revisar", "bi-search", items)


# --- Bodega --------------------------------------------------------------------------------
def bodega(usuario):
    """Los tickets en los pasos que le tocan a bodega: revisar la lista, comprar y alistar/entregar."""
    from inventario.solicitudes import bandeja

    f = _Filtro(usuario)
    filas = bandeja()
    if f.prueba:
        filas = [x for x in filas if x["ticket"].empresa_id and x["ticket"].empresa.razon_social.upper().startswith("PRUEBA")]
    grupos = []
    for clave, titulo, icono, boton in [("recibido", "Por revisar la lista", "bi-list-check", "Revisar lista"),
                                         ("compra", "Por comprar", "bi-cart", "Comprar lo que falta"),
                                         ("alistar", "Por alistar y entregar", "bi-box-seam", "Entregar al técnico")]:
        items = []
        for x in filas:
            if x["paso"] != clave:
                continue
            t = x["ticket"]
            url = x["solicitudes"][0].get_absolute_url() if x["solicitudes"] else _ficha(t)
            items.append(_item(f"{t.radicado} · {_cliente(t)}", f"{x['sigue']} · {x['items']} ítems · {_hace(timezone.now() - timedelta(days=x['dias']))}.",
                               url, boton, x["atrasado"], ticket=t))
        grupos.append(_grupo(clave, titulo, icono, items, ver_todo=reverse("inventario:bandeja")))
    return grupos


# --- Financiera ----------------------------------------------------------------------------
def financiera(usuario):
    """Órdenes por pagar (de contado o el anticipo), saldos de anticipos con el pedido ya recibido y facturas por validar."""
    from inventario.models import OrdenCompra

    O = OrdenCompra.Estado
    base = _Filtro(usuario).ordenes(OrdenCompra.objects.exclude(estado__in=[O.CANCELADA, O.BORRADOR])).select_related("proveedor")
    url = lambda o: reverse("inventario:orden", args=[o.pk])  # noqa: E731
    hoy = timezone.localdate()
    pagar = [_item(f"{o.numero} · {o.proveedor or 'sin proveedor'}",
                   f"{o.forma_pago_texto}: pague y suba el soporte para que bodega la envíe.",
                   url(o), "Registrar pago", True) for o in base.filter(estado=O.POR_PAGAR).order_by("pk")]
    facturas = [_item(f"{o.numero} · {o.proveedor or 'sin proveedor'}", f"Factura {o.factura or 'sin número'}: no cuadra o falta validarla.",
                      url(o), "Validar factura") for o in base.filter(factura_valor__isnull=False, factura_validada_en__isnull=True).order_by("pk")]
    saldos = [_item(f"{o.numero} · {o.proveedor or 'sin proveedor'}",
                    f"Llegó el pedido: pague el saldo del anticipo ({o.valor_por_pagar:,.0f}).".replace(",", "."),
                    url(o), "Pagar saldo", True)
              for o in base.filter(forma_pago=OrdenCompra.FormaPago.ANTICIPO, pagada_en__isnull=False,
                                   saldo_pagado_en__isnull=True, estado=O.RECIBIDA).order_by("pk")]
    from cronograma.models import SolicitudViaticos

    vqs = SolicitudViaticos.objects.filter(estado=SolicitudViaticos.Estado.PENDIENTE).select_related("ticket")
    if _Filtro(usuario).prueba:
        vqs = vqs.filter(destino__istartswith="PRUEBA")
    viaticos = [_item(f"{x.numero} · {x.destino}", f"{x.fecha:%d/%m/%Y} · {x.para_texto} · {x.conceptos_texto}.",
                      x.get_absolute_url(), "Gestionar", x.fecha <= hoy + timedelta(days=1), ticket=x.ticket)
                for x in vqs.order_by("fecha", "pk")]
    v = reverse("inventario:financiera")
    return [_grupo("viaticos", "Viáticos por gestionar", "bi-wallet2", viaticos,
                   "Alimentación, transporte, gasolina u otros para el personal SST/SGI o técnico.", reverse("cronograma:viaticos")),
            _grupo("pagar", "Por pagar", "bi-cash-coin", pagar, ver_todo=v),
            _grupo("facturas", "Facturas por validar", "bi-receipt", facturas, ver_todo=v),
            _grupo("saldos", "Saldos de anticipos", "bi-hourglass-split", saldos, "Pedidos con anticipo que ya llegaron a bodega.", v)]


# --- Lo que ve cada quien ------------------------------------------------------------------
def areas_de(usuario):
    """Las áreas cuyo «Mi día» ve el usuario, en orden. Dirección ve solo «Por aprobar» (salvo el superusuario,
    que además ve coordinación)."""
    from accounts.models import Usuario as U

    areas = []
    if usuario.es_direccion:
        areas.append(("Dirección", lambda: [por_aprobar(usuario)]))
    if usuario.es_coordinador:
        areas.append(("Coordinación técnica", lambda: coordinacion(usuario)))
    if usuario.rol == U.Rol.AGENTE or usuario.tambien_tecnico:
        areas.append(("Mis visitas", lambda: tecnico(usuario)))
    if usuario.es_comercial:
        areas.append(("Comercial", lambda: comercial(usuario)))
    if usuario.es_administrativo:
        areas.append(("Administrativa", lambda: administrativa(usuario)))
    elif usuario.revisa_compras and not usuario.es_direccion:
        areas.append(("Revisión de compras", lambda: [revision_compras(usuario)]))
    if usuario.es_bodega:
        areas.append(("Bodega", lambda: bodega(usuario)))
    if usuario.es_financiera:
        areas.append(("Financiera", lambda: financiera(usuario)))
    return areas


def tiene_mi_dia(usuario):
    """¿El usuario abre en «Mi día»? Todas las áreas internas menos la central de atención."""
    return bool(usuario.is_authenticated and areas_de(usuario))


def devueltos(usuario):
    """Tickets que otra área le devolvió al área del usuario porque falta algo por añadir, corregir o solucionar."""
    from . import devoluciones

    f = _Filtro(usuario)
    items = []
    for d in devoluciones.para_corregir(usuario, lambda qs: f.tickets(qs, "ticket__")):
        t, info = d.ticket, devoluciones.INFO[d.tramo]
        url = (reverse("inventario:solicitud", args=[d.solicitud_id_material]) if d.tramo == "a_lista" and d.solicitud_id_material
               else t.get_absolute_url())
        quien = (d.autor.get_full_name() or d.autor.username) if d.autor else info["de"]
        items.append(_item(f"{t.radicado} · {_cliente(t)}", f"Devuelto por {quien} ({info['de']}) {_hace(d.creada)}: {d.motivo[:160]}",
                           url, "Corregir", True, ticket=t))
    return _grupo("devueltos", "Devueltos a su área", "bi-arrow-return-left", items,
                  "Otra área se lo devolvió porque falta algo. Corríjalo y envíelo de vuelta.")


def pendientes(usuario):
    """Las áreas del usuario con sus grupos ya calculados: [{"nombre", "grupos"}]. Si otra área le devolvió
    tickets, «Devueltos a su área» va de primero."""
    areas = [{"nombre": nombre, "grupos": calcular()} for nombre, calcular in areas_de(usuario)]
    grupo = devueltos(usuario) if areas else None
    if grupo and grupo["items"]:
        areas[0]["grupos"].insert(0, grupo)
    return areas


def total_por_aprobar(usuario):
    """Cuántas cosas esperan la firma de dirección (para la etiqueta del menú)."""
    return len(por_aprobar(usuario)["items"]) if usuario.es_direccion else 0

