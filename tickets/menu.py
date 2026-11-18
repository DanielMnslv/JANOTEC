"""Menú lateral según el rol (14/11/2026).

Cada persona ve desplegadas las áreas que trabaja; las demás a las que tiene permiso quedan dentro de «Más áreas»,
para abrirlas cuando las necesite. Así Miguel, Guana o Yeiber, aunque tengan acceso a todo, ven primero lo suyo, y
Yuri o Sindy ven bodega y administrativa sin todo lo demás encima.

- Los permisos no cambian: un enlace sale si antes salía. Solo cambia dónde queda y si arranca abierto.
- «Lo suyo» sale del campo rol y de las marcas «también …» del usuario, también para los superusuarios
  (el superusuario sin área, el programador, lo tiene todo desplegado).
- Cada grupo se abre o se cierra con un toque y el navegador recuerda cómo lo dejó la persona (base.html).
- El grupo de la página abierta siempre se muestra desplegado.
- Paso a paso (18/11/2026): en cada área, las opciones van en el orden en que se hace el trabajo y llevan su número
  (recepcionar → asignar → programar…); las herramientas de apoyo quedan debajo, sin número.
"""
from dataclasses import dataclass, field

from django.urls import reverse

from .context_processors import MODULO_ADMINISTRATIVO

# Áreas que trabaja cada rol (las marcas «también …» suman otras)
AREAS_DEL_ROL = {
    "agente": {"tecnica"},
    "supervisor": {"tecnica", "gestion"},
    "direccion": {"gestion", "administrativa", "comercial"},  # aprueba propuestas y cubre comercial
    "comercial": {"comercial"},
    "bodega": {"bodega"},
    "administrativo": {"administrativa"},
    "financiera": {"financiera"},
    "central": {"central"},
    "cliente": {"cliente"},
}


@dataclass
class Enlace:
    """Una opción del menú."""
    nombre: str
    url: str
    icono: str
    activo: bool = False
    contador: int = 0
    contador_clase: str = "warning"
    contador_ayuda: str = ""
    paso: int = 0  # su número en el paso a paso del área (0 = herramienta de apoyo, sin número)


@dataclass
class Grupo:
    """Un grupo del menú con su título; ``abierto`` es cómo arranca si la persona no lo ha cambiado."""
    clave: str
    titulo: str
    area: str
    enlaces: list = field(default_factory=list)
    abierto: bool = True

    @property
    def activo(self):
        return any(e.activo for e in self.enlaces)


def areas_propias(usuario):
    """Las áreas que la persona trabaja; ``None`` = todas (superusuario sin área asignada)."""
    if usuario.is_superuser and usuario.rol in ("", "cliente"):
        return None
    areas = set(AREAS_DEL_ROL.get(usuario.rol, set()))
    # Dirección que también va a visitas: lo técnico queda en «Más áreas» (su trabajo diario es aprobar y gestionar)
    if usuario.tambien_tecnico and usuario.rol != "direccion":
        areas.add("tecnica")
    for marca, area in (("tambien_comercial", "comercial"),
                        ("tambien_administrativo", "administrativa"), ("tambien_financiera", "financiera")):
        if getattr(usuario, marca, False):
            areas.add(area)
    if getattr(usuario, "lleva_horas_extras", False):
        areas.add("horas")
    return areas


def _conteo_tecnica(usuario):
    """{estado: n} de la bandeja técnica y cuántos abiertos siguen sin técnico (los números del paso a paso)."""
    from django.db.models import Count

    from .models import Ticket
    from .permisos import tickets_visibles

    tecnica = tickets_visibles(usuario).filter(area=Ticket.Area.TECNICA)
    conteo = dict(tecnica.values_list("estado").annotate(n=Count("id")))
    pendientes = [*Ticket.ESTADOS_ABIERTOS, Ticket.Estado.VISITA_EJECUTADA]  # lo mismo que «Pendientes» del panel
    conteo["sin_tecnico"] = tecnica.filter(estado__in=pendientes, asignado_a=None).count()
    return conteo


def _grupos(request, datos):
    """Todos los grupos a los que el usuario tiene permiso, en el orden del menú (mismos permisos que antes).

    Paso a paso (18/11/2026): dentro de cada área van primero los pasos de su trabajo en orden (1, 2, 3…, el número
    sale en el menú) y debajo las herramientas de apoyo, para que la persona vaya bajando a medida que avanza."""
    from inventario.templatetags.inventario_extras import menu_bodega

    from .models import Ticket

    E = Ticket.Estado
    usuario = request.user
    coincidencia = getattr(request, "resolver_match", None)
    u = coincidencia.url_name if coincidencia else ""
    ns = coincidencia.namespace if coincidencia else ""
    ruta = request.get_full_path()
    get = request.GET
    por_pagar = datos.get("compras_por_pagar", 0)
    grupos = []

    def grupo(clave, titulo, area, *enlaces, abierto=True):
        enlaces = [e for e in enlaces if e]
        if enlaces:
            grupos.append(Grupo(clave, titulo, area, enlaces, abierto))

    def pasos(*enlaces):
        """Numera los pasos en el orden dado (los que no salen por permiso no cuentan)."""
        lista = [e for e in enlaces if e]
        for n, e in enumerate(lista, start=1):
            e.paso = n
        return lista

    def en_panel(estado, asignado=""):
        """¿La bandeja técnica abierta es esta vista? (sin filtros = la vista por defecto de cada quien)."""
        if u != "panel" or ns or get.get("aplazados"):
            return False
        defecto = "abiertos" if usuario.es_supervisor else "por_atender"
        return get.get("estado", "" if get else defecto) == estado and get.get("asignado", "") == asignado

    panel = reverse("panel")
    asignados = Enlace("Asignados a mí", reverse("asignados"), "bi-person-check", u == "asignados")
    financiera = Enlace("Pagos y facturas", reverse("inventario:financiera"), "bi-cash-stack", u == "financiera", por_pagar,
                        "danger", "Órdenes de compra por pagar")
    viaticos = Enlace("Viáticos", reverse("cronograma:viaticos"), "bi-wallet2", u == "viaticos")
    from .reportes import disponibles
    descargables = bool(disponibles(usuario)) and Enlace("Reportes en Excel", reverse("reportes_descargables"),
                                                        "bi-file-earmark-spreadsheet", u == "reportes_descargables")
    oportunidades = Enlace("Oportunidades", reverse("oportunidades"), "bi-graph-up-arrow", u == "oportunidades")

    def comercial_pasos():
        """Comercial: radicar → gestión → levantamiento → propuesta (administrativa) → seguimiento → aprobada."""
        bandeja = reverse("comercial:bandeja")
        vista = get.get("vista", "comercial") if (ns == "comercial" and u == "bandeja") else None
        etapas = ("gestion", "levantamiento", "administrativa", "enviada", "aprobada")
        return [*pasos(
            usuario.gestiona_requerimientos and Enlace("Nuevo requerimiento", reverse("comercial:nuevo"), "bi-plus-circle",
                                                       ns == "comercial" and u == "nuevo"),
            Enlace("Gestión comercial", bandeja + "?vista=gestion", "bi-telephone", vista == "gestion"),
            Enlace("Levantamiento técnico", bandeja + "?vista=levantamiento", "bi-rulers", vista == "levantamiento"),
            Enlace("Propuesta en elaboración", bandeja + "?vista=administrativa", "bi-calculator", vista == "administrativa"),
            Enlace("Seguimiento a enviadas", bandeja + "?vista=enviada", "bi-send", vista == "enviada"),
            Enlace("Aprobadas", bandeja + "?vista=aprobada", "bi-check2-circle", vista == "aprobada")),
            Enlace("Todos los requerimientos", bandeja, "bi-briefcase",
                   ns == "comercial" and u not in (*MODULO_ADMINISTRATIVO, "nuevo") and vista not in etapas),
            oportunidades]

    def administrativa_pasos():
        """Administrativa: armar la propuesta → precios → aprobación de dirección → correcciones → enviadas."""
        propuestas = reverse("comercial:propuestas")
        return pasos(
            Enlace("Propuestas por armar", propuestas + "#por-armar", "bi-calculator", u in ("propuestas", "cotizador")),
            Enlace("Catálogo de precios", reverse("comercial:catalogo"), "bi-tags", ns == "comercial" and u == "catalogo"),
            Enlace("Por aprobar (dirección)", propuestas + "#por-aprobar", "bi-patch-check", False,
                   datos.get("propuestas_por_aprobar", 0), "warning", "Propuestas esperando su aprobación"),
            Enlace("Devueltas y cambios del cliente", propuestas + "#devueltas", "bi-arrow-counterclockwise"),
            Enlace("Enviadas al cliente", propuestas + "#enviadas", "bi-send"))

    def bodega_pasos():
        """Bodega: ticket en bodega → validar la lista → comprar → recibir → entregar al técnico → cuadre y devoluciones."""
        ordenes = reverse("inventario:ordenes")
        recibir = ns == "inventario" and u == "ordenes" and get.get("estado") == "enviada"
        return pasos(
            Enlace("Tickets en bodega", reverse("inventario:bandeja"), "bi-inboxes", ns == "inventario" and u == "bandeja"),
            Enlace("Validar solicitudes de material", reverse("inventario:solicitudes"), "bi-clipboard-check",
                   ns == "inventario" and u in ("solicitudes", "solicitud")),
            Enlace("Comprar lo que falta", ordenes, "bi-cart", ns == "inventario" and u in ("ordenes", "orden") and not recibir),
            Enlace("Recibir compras", ordenes + "?estado=enviada", "bi-box-seam", recibir),
            Enlace("Entregar al técnico", reverse("inventario:entrega"), "bi-box-arrow-right", ns == "inventario" and u == "entrega"),
            Enlace("Devoluciones y cuadre", reverse("inventario:panel") + "?ver=tecnicos", "bi-box-arrow-in-left",
                   ns == "inventario" and u == "panel" and get.get("ver") == "tecnicos"))

    if usuario.es_personal:
        if usuario.ve_inventario and not usuario.gestiona_inventario:
            material = Enlace("Mi material", reverse("inventario:mi_material"), "bi-boxes", ns == "inventario")
        elif not usuario.ve_inventario and usuario.es_coordinador:
            material = Enlace("Funcionarios", reverse("inventario:funcionarios"), "bi-people", ns == "inventario")
        else:
            material = None
        cronograma = Enlace("Cronograma", reverse("cronograma:semana"), "bi-calendar-week",
                            ns == "cronograma" and u not in ("horas_extras", "horas_extras_corregir", "viaticos") and "mantenimiento" not in u)
        actas = Enlace("Actas", reverse("actas_lista"), "bi-clipboard-check", u in ("actas_lista", "acta_detalle"))
        horas = Enlace("Horas extras", reverse("cronograma:horas_extras"), "bi-clock-history",
                       u in ("horas_extras", "horas_extras_corregir"))
        if usuario.es_supervisor:
            # El paso a paso del ticket (tickets/pasos.py): recepcionar → asignar → programar → visita y acta → informe → cierre
            c = _conteo_tecnica(usuario)
            n = lambda *estados: sum(c.get(x, 0) for x in estados)  # noqa: E731
            tecnica = [*pasos(
                Enlace("Recepcionar", panel + "?estado=generado", "bi-inbox", en_panel("generado"), n(E.GENERADO),
                       "danger", "Tickets por recepcionar"),
                Enlace("Asignar técnico", panel + "?estado=abiertos&asignado=sin", "bi-person-plus",
                       en_panel("abiertos", "sin"), c["sin_tecnico"], "warning", "Tickets abiertos sin técnico"),
                Enlace("Programar visita", panel + "?estado=por_programar", "bi-calendar-plus", en_panel("por_programar"),
                       n(*Ticket.ESTADOS_PROGRAMABLES), "warning", "Tickets por programar"),
                cronograma,
                Enlace("Visitas por atender", panel + "?estado=por_atender", "bi-geo-alt", en_panel("por_atender"),
                       n(E.EN_CRONOGRAMA, E.AUTORIZADO), "secondary", "Programados que el técnico aún no atiende"),
                actas,
                Enlace("Informe", panel + "?estado=visita_ejecutada", "bi-file-earmark-text", en_panel("visita_ejecutada"),
                       n(E.VISITA_EJECUTADA), "warning", "Visitas ejecutadas por informar"),
                Enlace("Confirmar y cerrar", panel + "?estado=informe", "bi-check2-square", en_panel("informe"),
                       n(E.INFORME), "secondary", "Esperando la confirmación del cliente")),
                Enlace("Todos los pendientes", panel + "?estado=abiertos", "bi-kanban", en_panel("abiertos")),
                Enlace("Mantenimientos", reverse("cronograma:mantenimiento"), "bi-arrow-repeat", "mantenimiento" in u),
                horas, viaticos, material]
        else:
            # El técnico: sus visitas → cronograma → acta → informe; luego sus horas y su material
            tecnica = [*pasos(
                Enlace("Mis visitas por atender", panel, "bi-geo-alt", en_panel("por_atender")),
                cronograma, actas,
                Enlace("Informes pendientes", panel + "?estado=visita_ejecutada", "bi-file-earmark-text",
                       en_panel("visita_ejecutada"))),
                horas, usuario.ve_financiera and viaticos, material,
                Enlace("Todos mis tickets", panel + "?estado=", "bi-kanban", en_panel(""))]
        grupo("tecnica", "Área técnica", "tecnica", *tecnica)
        if usuario.ve_comercial:
            grupo("comercial", "Comercial", "comercial", *comercial_pasos())
        if usuario.ve_comercial and usuario.arma_propuestas:
            grupo("administrativa", "Área administrativa", "administrativa", *administrativa_pasos())
        if usuario.gestiona_inventario:
            grupo("bodega", "Bodega e inventario", "bodega", *bodega_pasos(),
                  Enlace("Todo el inventario", reverse("inventario:productos"), "bi-boxes", ns == "inventario" and u == "productos"),
                  not usuario.es_supervisor and descargables)
        if usuario.ve_comercial and usuario.ve_financiera:
            grupo("financiera", "Área financiera", "financiera", *pasos(financiera, viaticos))
        if usuario.es_supervisor:
            grupo("gestion", "Gestión", "gestion",
                  Enlace("Indicadores", reverse("indicadores_centro"), "bi-graph-up", u in ("indicadores_centro", "indicadores_metas")),
                  descargables,
                  Enlace("Clientes", reverse("clientes"), "bi-building", "cliente" in u),
                  Enlace("Configuración", reverse("admin:index"), "bi-gear"))
    elif usuario.es_bodega:
        # Primero su paso a paso (abierto); el resto de bodega en grupos que se abren con un toque
        propios = bodega_pasos()
        grupo("bodega-0", "Bodega", "bodega", *propios, asignados, descargables)
        en_pasos = {e.url for e in propios}  # lo que ya está en el paso a paso no se repite abajo
        for n, g in enumerate(menu_bodega()[1:], start=1):
            enlaces = [Enlace(o["nombre"], o["url"], o["icono"], ruta == o["url"]) for o in g["opciones"]
                       if o["url"] not in en_pasos]
            grupo(f"bodega-{n}", g["titulo"], "bodega", *enlaces, abierto=False)
        if usuario.es_administrativo:
            grupo("administrativa", "Área administrativa", "administrativa", *administrativa_pasos())
        if usuario.es_financiera:
            grupo("financiera", "Área financiera", "financiera", *pasos(financiera, viaticos))
        if usuario.es_comercial:
            grupo("comercial", "Comercial", "comercial", *comercial_pasos())
    elif usuario.es_comercial:
        grupo("comercial", "Comercial", "comercial", *comercial_pasos(), asignados)
        grupo("soporte", "Soporte técnico", "comercial",
              Enlace("Solicitudes técnicas", reverse("mis_tickets"), "bi-tools", u in ("nuevo_ticket", "mis_tickets")))
    elif usuario.es_administrativo:
        grupo("administrativa", "Área administrativa", "administrativa", *administrativa_pasos(), asignados)
    elif usuario.es_financiera:
        grupo("financiera", "Área financiera", "financiera", *pasos(financiera, viaticos), descargables)
    elif usuario.es_central:
        grupo("central", "Central de atención", "central",
              *pasos(Enlace("Registrar solicitud", reverse("nuevo_ticket"), "bi-plus-circle", u == "nuevo_ticket"),
                     Enlace("Solicitudes registradas", reverse("mis_tickets"), "bi-list-check", u == "mis_tickets")),
              asignados)
    else:
        grupo("cliente", "Mis servicios", "cliente",
              *pasos(Enlace("Radicar solicitud", reverse("nuevo_ticket"), "bi-plus-circle", u == "nuevo_ticket"),
                     Enlace("Mis solicitudes", reverse("mis_tickets"), "bi-list-check", u == "mis_tickets"),
                     Enlace("Historial de servicio", reverse("actas_lista"), "bi-clock-history",
                            u in ("actas_lista", "acta_detalle"))))
    if usuario.lleva_horas_extras and not usuario.es_personal:
        # Quien lleva las horas extras sin ser del área técnica (p. ej. Tatiana): valida, corrige o borra
        grupo("horas", "Horas extras", "horas",
              Enlace("Horas extras", reverse("cronograma:horas_extras"), "bi-clock-history",
                     u in ("horas_extras", "horas_extras_corregir")))
    return grupos


def construir(request, datos):
    """{"propios": [Grupo], "otros": [Grupo], "otros_activo": bool}: lo que la persona trabaja y «Más áreas»."""
    grupos = _grupos(request, datos)
    areas = areas_propias(request.user)
    propios = [g for g in grupos if areas is None or g.area in areas]
    otros = [g for g in grupos if g not in propios]
    if not propios:  # p. ej. un rol sin área en la tabla: no se esconde todo
        propios, otros = grupos, []
    return {"propios": propios, "otros": otros, "otros_activo": any(g.activo for g in otros)}
