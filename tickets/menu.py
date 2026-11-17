"""Menú lateral según el rol (14/11/2026).

Cada persona ve desplegadas las áreas que trabaja; las demás a las que tiene permiso quedan dentro de «Más áreas»,
para abrirlas cuando las necesite. Así Miguel, Guana o Yeiber, aunque tengan acceso a todo, ven primero lo suyo, y
Yuri o Sindy ven bodega y administrativa sin todo lo demás encima.

- Los permisos no cambian: un enlace sale si antes salía. Solo cambia dónde queda y si arranca abierto.
- «Lo suyo» sale del campo rol y de las marcas «también …» del usuario, también para los superusuarios
  (el superusuario sin área, el programador, lo tiene todo desplegado).
- Cada grupo se abre o se cierra con un toque y el navegador recuerda cómo lo dejó la persona (base.html).
- El grupo de la página abierta siempre se muestra desplegado.
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
    return areas


def _grupos(request, datos):
    """Todos los grupos a los que el usuario tiene permiso, en el orden del menú (mismos permisos que antes)."""
    from inventario.templatetags.inventario_extras import menu_bodega

    usuario = request.user
    coincidencia = getattr(request, "resolver_match", None)
    u = coincidencia.url_name if coincidencia else ""
    ns = coincidencia.namespace if coincidencia else ""
    ruta = request.get_full_path()
    por_pagar = datos.get("compras_por_pagar", 0)
    grupos = []

    def grupo(clave, titulo, area, *enlaces, abierto=True):
        enlaces = [e for e in enlaces if e]
        if enlaces:
            grupos.append(Grupo(clave, titulo, area, enlaces, abierto))

    asignados = Enlace("Asignados a mí", reverse("asignados"), "bi-person-check", u == "asignados")
    financiera = Enlace("Financiera", reverse("inventario:financiera"), "bi-cash-stack", u == "financiera", por_pagar,
                        "danger", "Órdenes de compra por pagar")
    viaticos = Enlace("Viáticos", reverse("cronograma:viaticos"), "bi-wallet2", u == "viaticos")
    oportunidades = Enlace("Oportunidades", reverse("oportunidades"), "bi-graph-up-arrow", u == "oportunidades")

    if usuario.es_personal:
        if usuario.ve_inventario and not usuario.gestiona_inventario:
            material = Enlace("Mi material", reverse("inventario:mi_material"), "bi-boxes", ns == "inventario")
        elif not usuario.ve_inventario and usuario.es_coordinador:
            material = Enlace("Funcionarios", reverse("inventario:funcionarios"), "bi-people", ns == "inventario")
        else:
            material = None
        grupo("tecnica", "Área técnica", "tecnica",
              Enlace("Recepción" if usuario.es_supervisor else "Mis tickets", reverse("panel"), "bi-kanban", u == "panel"),
              Enlace("Cronograma", reverse("cronograma:semana"), "bi-calendar-week",
                     ns == "cronograma" and u not in ("horas_extras", "viaticos") and "mantenimiento" not in u),
              Enlace("Horas extras", reverse("cronograma:horas_extras"), "bi-clock-history", u == "horas_extras"),
              usuario.es_supervisor and Enlace("Mantenimientos", reverse("cronograma:mantenimiento"), "bi-arrow-repeat",
                                               "mantenimiento" in u),
              (usuario.es_supervisor or usuario.ve_financiera) and viaticos,
              Enlace("Actas", reverse("actas_lista"), "bi-clipboard-check", u in ("actas_lista", "acta_detalle")),
              material)
        if usuario.ve_comercial:
            grupo("comercial", "Comercial", "comercial",
                  Enlace("Comercial", reverse("comercial:bandeja"), "bi-briefcase", ns == "comercial" and u not in MODULO_ADMINISTRATIVO),
                  oportunidades)
        if usuario.ve_comercial and usuario.arma_propuestas:
            grupo("administrativa", "Área administrativa", "administrativa",
                  Enlace("Administrativo", reverse("comercial:propuestas"), "bi-calculator", u in MODULO_ADMINISTRATIVO,
                         datos.get("propuestas_por_aprobar", 0), "warning", "Propuestas esperando su aprobación"))
        if usuario.gestiona_inventario:
            grupo("bodega", "Bodega e inventario", "bodega",
                  Enlace("Inventario", reverse("inventario:productos"), "bi-boxes", ns == "inventario" and u != "financiera"))
        if usuario.ve_comercial and usuario.ve_financiera:
            grupo("financiera", "Área financiera", "financiera", financiera)
        if usuario.es_supervisor:
            grupo("gestion", "Gestión", "gestion",
                  Enlace("Clientes", reverse("clientes"), "bi-building", "cliente" in u),
                  Enlace("Indicadores", reverse("indicadores_centro"), "bi-graph-up", u == "indicadores_centro"),
                  Enlace("Configuración", reverse("admin:index"), "bi-gear"))
    elif usuario.es_bodega:
        # Bodega tiene muchas opciones: arranca abierto «Mi trabajo»; los demás grupos se abren con un toque
        for n, g in enumerate(menu_bodega()):
            enlaces = [Enlace(o["nombre"], o["url"], o["icono"], ruta == o["url"]) for o in g["opciones"]]
            if n == 0:
                enlaces.insert(0, asignados)
            grupo(f"bodega-{n}", "Bodega" if n == 0 else g["titulo"], "bodega", *enlaces, abierto=n == 0)
        if usuario.es_administrativo:
            grupo("administrativa", "Área administrativa", "administrativa",
                  Enlace("Administrativo", reverse("comercial:propuestas"), "bi-calculator", ns == "comercial"))
        if usuario.es_financiera:
            grupo("financiera", "Área financiera", "financiera", financiera, viaticos)
        if usuario.es_comercial:
            grupo("comercial", "Comercial", "comercial",
                  Enlace("Comercial", reverse("comercial:bandeja"), "bi-briefcase", ns == "comercial" and u not in MODULO_ADMINISTRATIVO),
                  oportunidades)
    elif usuario.es_comercial:
        grupo("comercial", "Comercial", "comercial", asignados,
              Enlace("Requerimientos", reverse("comercial:bandeja"), "bi-briefcase", u == "bandeja"),
              Enlace("Nuevo requerimiento", reverse("comercial:nuevo"), "bi-plus-circle", u == "nuevo"),
              oportunidades)
        grupo("soporte", "Soporte técnico", "comercial",
              Enlace("Solicitudes técnicas", reverse("mis_tickets"), "bi-tools", u in ("nuevo_ticket", "mis_tickets")))
    elif usuario.es_administrativo:
        grupo("administrativa", "Área administrativa", "administrativa", asignados,
              Enlace("Administrativo", reverse("comercial:propuestas"), "bi-calculator", ns == "comercial"))
    elif usuario.es_financiera:
        grupo("financiera", "Área financiera", "financiera", financiera, viaticos)
    elif usuario.es_central:
        grupo("central", "Central de atención", "central", asignados,
              Enlace("Registrar solicitud", reverse("nuevo_ticket"), "bi-plus-circle", u == "nuevo_ticket"),
              Enlace("Solicitudes registradas", reverse("mis_tickets"), "bi-list-check", u == "mis_tickets"))
    else:
        grupo("cliente", "Mis servicios", "cliente",
              Enlace("Mis solicitudes", reverse("mis_tickets"), "bi-list-check", u == "mis_tickets"),
              Enlace("Radicar solicitud", reverse("nuevo_ticket"), "bi-plus-circle", u == "nuevo_ticket"),
              Enlace("Historial de servicio", reverse("actas_lista"), "bi-clock-history", u in ("actas_lista", "acta_detalle")))
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
