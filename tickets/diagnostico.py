"""Salud de los procesos: la plataforma revisa sola cómo se está usando y detecta lo que no fluye.

Cada regla mira los datos reales (tickets, historial, cronograma, actas, material, comercial) y devuelve los
casos que hoy no cumplen la lógica del proceso. Un caso nuevo queda como «hallazgo» abierto; cuando se corrige,
se cierra solo. Así se ve qué se repite, con quién y qué conviene mejorar (en la plataforma o en la capacitación).

Se ejecuta sola (como máximo cada 30 minutos, al usar la plataforma) o con ``python manage.py diagnosticar``.
Solo el superusuario ve los hallazgos; le llega un correo con lo nuevo (como máximo cada 4 horas).

Retirada el 06/11/2026 (``settings.SALUD_PROCESOS`` en falso): no revisa sola, no avisa, no sale en el menú ni en las
bandejas de las áreas. Los hallazgos ya guardados se conservan; con ``SALUD_PROCESOS=1`` en el .env vuelve todo.
"""
import logging
from dataclasses import dataclass, field
from datetime import time, timedelta

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count
from django.urls import reverse
from django.utils import timezone

from .models import ActaSoporte, Hallazgo, Seguimiento, Ticket

log = logging.getLogger(__name__)
E = Ticket.Estado
INTERVALO_REVISION = 30 * 60
INTERVALO_CORREO = 4 * 3600

# Pasos que el flujo permite desde cada estado (lo demás es un «salto» que se saltó parte del proceso)
SIGUIENTES = {
    E.GENERADO: {E.RECEPCIONADO, E.EN_PROCESO, E.CANCELADO},
    E.RECEPCIONADO: {E.EN_CRONOGRAMA, E.EN_PROCESO, E.CANCELADO, E.GENERADO},
    E.EN_CRONOGRAMA: {E.AUTORIZADO, E.VISITA_EJECUTADA, E.INFORME, E.PENDIENTE_VISITA, E.RECEPCIONADO, E.CANCELADO},
    E.AUTORIZADO: {E.VISITA_EJECUTADA, E.INFORME, E.PENDIENTE_VISITA, E.RECEPCIONADO, E.CANCELADO},
    E.VISITA_EJECUTADA: {E.CERRADO, E.PENDIENTE_VISITA, E.INFORME},
    E.PENDIENTE_VISITA: {E.EN_CRONOGRAMA, E.CERRADO, E.CANCELADO},
    E.INFORME: {E.VISITA_EJECUTADA, E.EN_PROCESO, E.CERRADO},
    E.EN_PROCESO: {E.PROPUESTA_ENVIADA, E.EN_INVENTARIO, E.RECEPCIONADO, E.GENERADO, E.CERRADO, E.CANCELADO},
    E.PROPUESTA_ENVIADA: {E.EN_PROCESO, E.EN_INVENTARIO, E.RECEPCIONADO, E.GENERADO, E.CERRADO, E.CANCELADO},
    E.EN_INVENTARIO: {E.RECEPCIONADO, E.EN_PROCESO, E.CANCELADO},
    E.CERRADO: {E.PENDIENTE_VISITA},
    E.CANCELADO: set(),
}
# Estados que dicen que hubo una visita: deben venir con su acta
CON_VISITA = {E.VISITA_EJECUTADA, E.INFORME}


@dataclass
class Regla:
    """Una regla de salud de procesos: qué revisa, de qué área es, qué tan grave es, qué conviene mejorar y los casos encontrados."""
    clave: str
    nombre: str
    area: str
    gravedad: str  # alta | media | baja
    mejora: str  # qué conviene mejorar (plataforma o capacitación)
    casos: list = field(default_factory=list)


def _url(ticket):
    """Enlace a la ficha del ticket."""
    return reverse("ticket_detalle", args=[ticket.radicado])


def _caso(clave, titulo, detalle="", ticket=None, usuario=None, url=""):
    """Un caso encontrado por una regla, con título y detalle recortados al tamaño de la base de datos."""
    return {"clave": clave, "titulo": titulo[:250], "detalle": detalle[:1000], "ticket": ticket, "usuario": usuario,
            "url": url or (_url(ticket) if ticket else "")}


def _hora(h):
    """Hora en formato de 12 horas en español: «8:30 a. m.»."""
    return h.strftime("%I:%M %p").lstrip("0").replace("AM", "a. m.").replace("PM", "p. m.")


def _nombre(u):
    """El nombre de la persona; si no hay, «el sistema»."""
    return (u.get_full_name() or u.username) if u else "el sistema"


# --- Reglas ------------------------------------------------------------------------
def contacto_interno():
    """El contacto del cliente es alguien de JANOTEC: los correos «al cliente» y la autorización no le llegan a él."""
    from config.correo import correos_internos
    internos = correos_internos()
    casos = []
    for t in Ticket.objects.exclude(estado__in=Ticket.ESTADOS_FINALES).exclude(empresa__es_del_grupo=True).select_related("radicado_por"):
        correo = (t.contacto_email or "").strip().lower()
        if correo and (correo in internos or correo.endswith("@janotec.app")):
            casos.append(_caso(f"t{t.pk}", f"{t.radicado}: el contacto del cliente es «{t.contacto_email}»",
                               f"Contacto registrado: {t.contacto_nombre}. Radicó: {_nombre(t.radicado_por)}.", t, t.radicado_por))
    return casos


def saltos_de_estado():
    """Cambios de estado hechos a mano que se saltan pasos del proceso (en los últimos 30 días)."""
    desde = timezone.now() - timedelta(days=30)
    casos = []
    segs = (Seguimiento.objects.filter(creado__gte=desde).exclude(estado_nuevo="").exclude(estado_anterior="")
            .select_related("ticket", "autor"))
    for s in segs:
        if s.estado_nuevo not in SIGUIENTES.get(s.estado_anterior, {s.estado_nuevo}):
            casos.append(_caso(f"s{s.pk}", f"{s.ticket.radicado}: pasó de «{s.estado_anterior_display}» a «{s.estado_nuevo_display}»",
                               f"Lo hizo {_nombre(s.autor)} el {timezone.localtime(s.creado):%d/%m %I:%M %p}.", s.ticket, s.autor))
    return casos


def visita_sin_acta():
    """El ticket dice que hubo visita, pero no tiene acta."""
    casos = []
    for t in Ticket.objects.filter(estado__in=CON_VISITA).annotate(n=Count("actas")).filter(n=0).select_related("asignado_a"):
        casos.append(_caso(f"t{t.pk}", f"{t.radicado}: está en «{t.get_estado_display()}» y no tiene acta",
                           f"Responsable: {_nombre(t.asignado_a) if t.asignado_a else 'sin responsable'}.", t, t.asignado_a))
    return casos


def visita_pasada_sin_acta():
    """Ya pasó la fecha de la visita, el ticket sigue abierto y nadie diligenció el acta."""
    from cronograma.models import Actividad
    hoy = timezone.localdate()
    casos = []
    for a in (Actividad.objects.filter(fecha__lt=hoy, fecha__gte=hoy - timedelta(days=30), ticket__estado__in=[E.EN_CRONOGRAMA, E.AUTORIZADO])
              .select_related("ticket").prefetch_related("encargados")):
        if not ActaSoporte.objects.filter(ticket=a.ticket, fecha__date__gte=a.fecha).exists():
            tecnico = a.encargados.first()
            casos.append(_caso(f"a{a.pk}", f"{a.ticket.radicado}: la visita del {a.fecha:%d/%m} no tiene acta",
                               f"Técnico: {_nombre(tecnico)}. Si no se hizo, aplácela o reprográmela.", a.ticket, tecnico))
    return casos


def acta_por_otra_persona():
    """El acta la diligenció alguien que no fue a la visita (p. ej. el coordinador a su nombre)."""
    desde = timezone.now() - timedelta(days=30)
    casos = []
    for acta in ActaSoporte.objects.filter(origen=ActaSoporte.Origen.PLATAFORMA, fecha__gte=desde, ticket__isnull=False).select_related("ticket", "tecnico"):
        fueron = {u.pk for a in acta.ticket.actividades.prefetch_related("encargados") for u in a.tecnicos()}
        if acta.tecnico_id and fueron and acta.tecnico_id not in fueron:
            nombres = ", ".join(sorted({_nombre(u) for a in acta.ticket.actividades.all() for u in a.tecnicos()}))
            casos.append(_caso(f"acta{acta.pk}", f"Acta {acta.numero} ({acta.ticket.radicado}) quedó a nombre de {acta.tecnico_texto}",
                               f"La visita era de: {nombres}. Los reportes por técnico se le atribuyen a quien firmó.",
                               acta.ticket, acta.tecnico, reverse("acta_detalle", args=[acta.numero])))
    return casos


def autorizaciones():
    """Visitas próximas sin autorización enviada, esperando respuesta mucho tiempo o rechazadas sin reprogramar."""
    from cronograma.models import Actividad
    A = Actividad.Autorizacion
    hoy, ahora = timezone.localdate(), timezone.now()
    casos = []
    for a in (Actividad.objects.filter(fecha__gte=hoy, ticket__isnull=False).exclude(ticket__empresa__es_del_grupo=True)
              .select_related("ticket", "creado_por")):
        cuando = f"{a.fecha:%d/%m} {_hora(a.hora_inicio)}"
        if a.autorizacion == A.SIN_ENVIAR and a.fecha <= hoy + timedelta(days=2):
            casos.append(_caso(f"a{a.pk}-sin", f"{a.ticket.radicado}: visita del {cuando} sin autorización enviada",
                               "El cliente no sabe que vamos ni autorizó el ingreso del personal.", a.ticket, a.creado_por))
        elif a.autorizacion == A.ENVIADA and a.autorizacion_enviada and a.autorizacion_enviada < ahora - timedelta(hours=48):
            casos.append(_caso(f"a{a.pk}-esp", f"{a.ticket.radicado}: el cliente no responde la autorización de la visita del {cuando}",
                               f"Enviada el {timezone.localtime(a.autorizacion_enviada):%d/%m}. Conviene llamarlo.", a.ticket, a.creado_por))
        elif a.autorizacion == A.RECHAZADA:
            casos.append(_caso(f"a{a.pk}-rech", f"{a.ticket.radicado}: el cliente rechazó la visita del {cuando} y sigue programada",
                               f"Motivo: {a.motivo_rechazo or 'sin motivo'}. Hay que reprogramarla o aplazarla.", a.ticket, a.creado_por))
    return casos


def sst_sin_correo():
    """Visitas que necesitan acompañamiento SST, pero el área SST no tiene correo: la solicitud no le llega a nadie."""
    from cronograma import servicios as crono
    from cronograma.models import Actividad
    if crono.correo_sst():
        return []
    hoy = timezone.localdate()
    return [_caso(f"a{a.pk}", f"{a.ticket.radicado if a.ticket else a.cliente}: visita del {a.fecha:%d/%m} con SST sin a quién pedirlo",
                  "Agregue el correo del área SST en Cronograma → Directorio.", a.ticket, a.creado_por,
                  "" if a.ticket else f"{reverse('cronograma:semana')}?d={a.fecha:%Y-%m-%d}")
            for a in Actividad.objects.filter(fecha__gte=hoy, sst=True).select_related("ticket", "creado_por")]


def programacion_rara():
    """Visitas programadas en una fecha que ya había pasado o a horas fuera de la jornada (p. ej. 2:00 a. m.)."""
    from cronograma.models import Actividad
    hoy = timezone.localdate()
    casos = []
    for a in Actividad.objects.filter(fecha__gte=hoy - timedelta(days=7)).select_related("ticket", "creado_por"):
        creada = timezone.localtime(a.creado).date()
        motivos = []
        if a.fecha < creada:
            motivos.append(f"se programó el {creada:%d/%m} para una fecha que ya había pasado ({a.fecha:%d/%m})")
        if a.hora_inicio < time(6) or a.hora_fin > time(20):
            motivos.append(f"horario {_hora(a.hora_inicio)}–{_hora(a.hora_fin)}")
        if motivos:
            casos.append(_caso(f"a{a.pk}", f"{a.ticket.radicado if a.ticket else a.cliente}: {' y '.join(motivos)}",
                               "Revise la fecha y la hora de la visita.", a.ticket, a.creado_por,
                               "" if a.ticket else f"{reverse('cronograma:semana')}?d={a.fecha:%Y-%m-%d}"))
    return casos


def material_sin_cuadrar():
    """Tickets cerrados con material entregado que no se instaló (acta) ni se devolvió a bodega."""
    from inventario.models import MovimientoActivo, MovimientoInsumo
    from inventario.solicitudes import cuadre
    ids = {pk for modelo in (MovimientoInsumo, MovimientoActivo)
           for pk in modelo.objects.filter(tipo="entrega", ticket__isnull=False).values_list("ticket_id", flat=True)}
    casos = []
    for t in Ticket.objects.filter(pk__in=ids, estado=E.CERRADO):
        pendientes = cuadre(t)["pendientes"]
        if pendientes:
            detalle = "; ".join(f"{f['pendiente']:g} {f['descripcion']} ({_nombre(f['tecnico'])})" for f in pendientes[:5])
            casos.append(_caso(f"t{t.pk}", f"{t.radicado}: se cerró con {len(pendientes)} material(es) sin cuadrar", detalle, t,
                               pendientes[0]["tecnico"]))
    return casos


def material_estancado():
    """Solicitudes de material sin movimiento, o abiertas de tickets que ya se cerraron."""
    from inventario.models import SolicitudMaterial
    limite = timezone.now() - timedelta(days=2)
    casos = []
    for s in SolicitudMaterial.objects.filter(estado__in=SolicitudMaterial.ESTADOS_ABIERTOS).select_related("ticket"):
        url = reverse("inventario:solicitud", args=[s.pk])
        if s.ticket.estado in Ticket.ESTADOS_FINALES:
            casos.append(_caso(f"sm{s.pk}-cerrado", f"{s.numero}: sigue «{s.get_estado_display()}» y el ticket {s.ticket.radicado} ya se cerró",
                               "Cancele la solicitud o registre lo que se entregó.", s.ticket, None, url))
        elif s.actualizado < limite:
            casos.append(_caso(f"sm{s.pk}", f"{s.numero} ({s.ticket.radicado}): lleva {(timezone.now() - s.actualizado).days} días en «{s.get_estado_display()}»",
                               "Bodega debe validarla (y el coordinador TIC o dirección autorizarla) para que el material salga con trazabilidad.", s.ticket, None, url))
    return casos


def notas_como_material():
    """En «Insumos y equipos» se escribieron notas o material que no está en el inventario."""
    from inventario.models import ItemSolicitud, SolicitudMaterial
    casos = []
    for i in (ItemSolicitud.objects.filter(insumo__isnull=True, referencia="", a_mano_listo=False,
                                           solicitud__estado__in=SolicitudMaterial.ESTADOS_ABIERTOS).select_related("solicitud__ticket")):
        s = i.solicitud
        casos.append(_caso(f"item{i.pk}", f"{s.numero} ({s.ticket.radicado}): «{i.cantidad.normalize():f} {i.descripcion}» no está en el inventario",
                           "Si es una nota (dónde está, quién lo suministra) no debería ir como material; si es material, búsquelo en el inventario.",
                           s.ticket, s.solicitado_por, reverse("inventario:solicitud", args=[s.pk])))
    return casos


def tickets_detenidos():
    """Tickets que llevan días sin avanzar en su etapa."""
    ahora = timezone.now()
    plazos = {E.GENERADO: 1, E.RECEPCIONADO: 3, E.INFORME: 3, E.PENDIENTE_VISITA: 3, E.VISITA_EJECUTADA: 5}
    casos = []
    for t in Ticket.objects.filter(estado__in=plazos, area=Ticket.Area.TECNICA).select_related("asignado_a"):
        dias = (ahora - t.actualizado).days
        if dias >= plazos[t.estado]:
            casos.append(_caso(f"t{t.pk}-{t.estado}", f"{t.radicado}: {dias} días en «{t.get_estado_display()}»",
                               f"Responsable: {_nombre(t.asignado_a) if t.asignado_a else 'sin responsable'}.", t, t.asignado_a))
        elif t.estado != E.GENERADO and not t.asignado_a_id:
            casos.append(_caso(f"t{t.pk}-sin", f"{t.radicado}: «{t.get_estado_display()}» sin responsable", "", t))
    return casos


def comercial_desconectado():
    """Lo comercial que se quedó sin dueño: estado comercial sin ficha, sin asesor o sin movimiento."""
    from comercial.models import FichaComercial as F
    ahora = timezone.now()
    casos = []
    for t in Ticket.objects.filter(estado__in=[E.EN_PROCESO, E.PROPUESTA_ENVIADA], ficha__isnull=True):
        casos.append(_caso(f"t{t.pk}-huerfano", f"{t.radicado}: dice «{t.get_estado_display()}» pero no llegó al área comercial",
                           "Páselo con «En gestión comercial» desde el ticket o con el informe técnico.", t))
    for f in F.objects.filter(etapa__in=F.ETAPAS_ABIERTAS, ticket__area=Ticket.Area.COMERCIAL).select_related("ticket", "comercial"):
        url = reverse("comercial:detalle", args=[f.ticket.radicado])
        if not f.comercial_id and f.actualizado < ahora - timedelta(days=1):
            casos.append(_caso(f"f{f.pk}-asesor", f"{f.ticket.radicado}: «{f.get_etapa_display()}» sin asesor asignado",
                               "Nadie del área comercial lo ha tomado.", f.ticket, None, url))
        elif f.actualizado < ahora - timedelta(days=5):
            casos.append(_caso(f"f{f.pk}-quieto", f"{f.ticket.radicado}: {(ahora - f.actualizado).days} días sin movimiento en «{f.get_etapa_display()}»",
                               f"Asesor: {_nombre(f.comercial)}.", f.ticket, f.comercial, url))
    return casos


def recepcion_repetida():
    """Tickets que se recepcionaron y se devolvieron a «Generado» varias veces: señal de que la pantalla confunde."""
    casos = []
    idas = (Seguimiento.objects.filter(estado_anterior=E.RECEPCIONADO, estado_nuevo=E.GENERADO)
            .values("ticket").annotate(n=Count("id")).filter(n__gte=2))
    for fila in idas:
        t = Ticket.objects.get(pk=fila["ticket"])
        casos.append(_caso(f"t{t.pk}", f"{t.radicado}: se recepcionó y se devolvió a «Generado» {fila['n']} veces", "", t))
    return casos


REGLAS = [
    (contacto_interno, Regla("contacto_interno", "El contacto del cliente es del personal JANOTEC", "Coordinación", "alta",
                             "Registrar el contacto real del cliente (nombre, teléfono y correo de quien recibe). Mejora posible: que la "
                             "plataforma advierta al radicar cuando el correo es de @janotec.app y proponga el contacto de la sede.")),
    (saltos_de_estado, Regla("saltos_de_estado", "Cambios de estado que se saltan pasos", "Coordinación", "media",
                             "Avanzar con las acciones del proceso (cronograma, acta, informe, traspaso) en lugar de la lista «Estado». "
                             "Mejora posible: que la lista solo ofrezca los pasos válidos desde el estado actual.")),
    (visita_sin_acta, Regla("visita_sin_acta", "Dice que hubo visita, pero no hay acta", "Técnicos", "alta",
                            "Toda visita ejecutada debe tener su acta firmada. Mejora posible: exigir el acta para marcar «Visita ejecutada».")),
    (visita_pasada_sin_acta, Regla("visita_pasada_sin_acta", "Visitas que ya pasaron sin acta", "Técnicos", "alta",
                                   "El técnico diligencia el acta al terminar (también desde el celular). Si la visita no se hizo, se aplaza.")),
    (acta_por_otra_persona, Regla("acta_por_otra_persona", "Actas a nombre de quien no fue a la visita", "Técnicos", "media",
                                  "Que cada técnico diligencie su acta. Mejora posible: si el coordinador la diligencia, elegir qué técnico fue.")),
    (autorizaciones, Regla("autorizaciones", "Autorizaciones de ingreso pendientes", "Coordinación", "alta",
                           "Enviar la autorización al programar la visita y llamar al cliente si no responde.")),
    (sst_sin_correo, Regla("sst_sin_correo", "Acompañamiento SST sin a quién pedirlo", "Coordinación", "alta",
                           "Registrar el correo del área SST en el Directorio del cronograma.")),
    (programacion_rara, Regla("programacion_rara", "Visitas con fecha u hora fuera de lo normal", "Coordinación", "media",
                              "Mejora posible: que el cronograma advierta al programar en fecha pasada o fuera de la jornada laboral.")),
    (material_sin_cuadrar, Regla("material_sin_cuadrar", "Tickets cerrados con material sin cuadrar", "Bodega", "alta",
                                 "Lo que salió de bodega para un ticket se descuenta en el acta (instalado) o vuelve a bodega "
                                 "indicando el ticket. Si no, el material queda en poder del técnico sin legalizar.")),
    (material_estancado, Regla("material_estancado", "Solicitudes de material detenidas", "Bodega", "media",
                               "Bodega valida cada solicitud (y el coordinador TIC o dirección la autoriza) antes de la visita; las de tickets cerrados se cancelan.")),
    (notas_como_material, Regla("notas_como_material", "Notas o material que no está en el inventario", "Coordinación", "baja",
                                "Usar el buscador del inventario para el material y dejar las notas en la descripción de la actividad. "
                                "Mejora posible: un campo de «notas del material» separado.")),
    (tickets_detenidos, Regla("tickets_detenidos", "Tickets detenidos en una etapa", "Coordinación", "media",
                              "Revisar cada día los que llevan más de lo normal y dejar claro quién sigue.")),
    (comercial_desconectado, Regla("comercial_desconectado", "Requerimientos comerciales sin dueño o sin movimiento", "Comercial", "media",
                                   "Que el asesor tome el requerimiento (se asigna en «Gestionar propuesta») y lo mueva cada pocos días.")),
    (recepcion_repetida, Regla("recepcion_repetida", "Recepcionar y devolver varias veces", "Coordinación", "baja",
                               "Señal de que la pantalla de recepción confunde: revisar con jguana qué buscaba hacer.")),
]


# --- Ejecución -------------------------------------------------------------------------
def revisar():
    """Corre todas las reglas, abre los hallazgos nuevos y cierra los que ya se corrigieron. Devuelve los nuevos."""
    ahora = timezone.now()
    nuevos = []
    with transaction.atomic():
        for funcion, regla in REGLAS:
            try:
                casos = funcion()
            except Exception:  # noqa: BLE001 — una regla con error no frena las demás
                log.exception("Falló la regla de diagnóstico %s", regla.clave)
                continue
            vigentes = set()
            for c in casos:
                vigentes.add(c["clave"])
                h, creado = Hallazgo.objects.get_or_create(
                    regla=regla.clave, clave=c["clave"],
                    defaults={"titulo": c["titulo"], "detalle": c["detalle"], "url": c["url"], "ticket": c["ticket"],
                              "usuario": c["usuario"], "gravedad": regla.gravedad, "area": regla.area})
                if creado:
                    nuevos.append(h)
                else:
                    if h.resuelto:  # se había corregido y volvió a pasar: es algo nuevo, se vuelve a revisar
                        h.revisado_en, h.revisado_por, h.motivo_revision, h.avisado = None, None, "", False
                    h.titulo, h.detalle, h.url, h.visto, h.resuelto = c["titulo"], c["detalle"], c["url"], ahora, None
                    h.save(update_fields=["titulo", "detalle", "url", "visto", "resuelto", "revisado_en", "revisado_por",
                                          "motivo_revision", "avisado"])
            Hallazgo.objects.filter(regla=regla.clave, resuelto__isnull=True).exclude(clave__in=vigentes).update(resuelto=ahora)
    return nuevos


def reglas():
    """Las reglas por su clave."""
    return {r.clave: r for _, r in REGLAS}


def areas_de(usuario):
    """Las áreas cuyos casos ve y puede revisar cada quien (el superusuario, todas)."""
    if usuario.is_superuser:
        return {r.area for _, r in REGLAS}
    areas = set()
    if usuario.es_supervisor:
        areas |= {"Coordinación", "Técnicos"}
    if usuario.gestiona_inventario:
        areas.add("Bodega")
    if usuario.ve_comercial:
        areas.add("Comercial")
    return areas


def pendientes():
    """Los casos abiertos que nadie ha revisado."""
    return Hallazgo.objects.filter(resuelto__isnull=True, revisado_en__isnull=True)


def activa():
    """¿Está encendida la salud de los procesos? (ver ``settings.SALUD_PROCESOS``)."""
    return getattr(settings, "SALUD_PROCESOS", False)


def casos_para(usuario, areas=None):
    """Lo que le toca revisar a cada área en su bandeja: abierto y sin revisar, lo más grave primero (nada si está apagada)."""
    if not activa():
        return []
    areas = areas_de(usuario) if areas is None else set(areas) & areas_de(usuario)
    orden = {"alta": 0, "media": 1, "baja": 2}
    casos = list(pendientes().filter(area__in=areas).select_related("ticket"))
    return sorted(casos, key=lambda h: (orden.get(h.gravedad, 3), h.creado))


def marcar_revisado(hallazgo, usuario, motivo):
    """Marca un hallazgo como revisado sin acción, con su motivo (queda guardado).

    Solo lo hace alguien del área del hallazgo, y el motivo debe tener al menos 5 caracteres.
    """
    from django.core.exceptions import PermissionDenied, ValidationError

    if hallazgo.area not in areas_de(usuario):
        raise PermissionDenied
    if len(motivo.strip()) < 5:
        raise ValidationError("Escriba por qué no requiere acción (queda guardado).")
    hallazgo.revisado_en, hallazgo.revisado_por, hallazgo.motivo_revision = timezone.now(), usuario, motivo.strip()[:300]
    hallazgo.save(update_fields=["revisado_en", "revisado_por", "motivo_revision"])


def avisar(forzar=False):
    """Correo al superusuario con los hallazgos que aún no se le han contado (como máximo cada 4 horas)."""
    from accounts.models import Usuario

    from . import notificaciones
    pendientes = list(Hallazgo.objects.filter(avisado=False, resuelto__isnull=True, revisado_en__isnull=True)
                      .order_by("regla", "creado"))
    if not pendientes or (not forzar and not cache.add("diagnostico:correo", True, INTERVALO_CORREO)):
        return 0
    destinos = list(Usuario.objects.filter(is_superuser=True, is_active=True).exclude(email="").values_list("email", flat=True))
    if not destinos:
        return 0
    por_regla, info = {}, reglas()
    for h in pendientes:
        por_regla.setdefault(h.regla, []).append(h)
    base = settings.SITIO_URL.rstrip("/")
    lineas = [f"La plataforma detectó {len(pendientes)} situación(es) nueva(s) en el uso de los procesos:", ""]
    for clave, lista in por_regla.items():
        r = info.get(clave)
        lineas.append(f"■ {r.nombre if r else clave} ({len(lista)}) · {r.area if r else ''}")
        lineas += [f"   - {h.titulo}" for h in lista[:8]]
        if len(lista) > 8:
            lineas.append(f"   … y {len(lista) - 8} más")
        if r:
            lineas.append(f"   Qué mejorar: {r.mejora}")
        lineas.append("")
    lineas.append(f"Ver todo: {base}{reverse('salud_procesos')}")
    notificaciones._enviar(f"[Salud de los procesos] {len(pendientes)} situación(es) nueva(s)", "\n".join(lineas), destinos)
    Hallazgo.objects.filter(pk__in=[h.pk for h in pendientes]).update(avisado=True)
    return len(pendientes)


def revisar_si_toca():
    """Desde las vistas principales: como máximo cada 30 minutos; nunca interrumpe a quien está trabajando."""
    if not activa():
        return
    if cache.add("diagnostico:revision", True, INTERVALO_REVISION):
        try:
            revisar()
            avisar()
        except Exception:  # noqa: BLE001
            log.exception("No se pudo revisar la salud de los procesos")
