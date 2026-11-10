"""Devolver el ticket al área anterior cuando falta algo por añadir, corregir o solucionar.

Tramos (quién devuelve → a quién le toca corregir):

    a_tecnica        Comercial / administrativa → área técnica: el informe o levantamiento llegó incompleto.
    a_comercial      Área técnica → comercial: faltan datos para hacer la visita de levantamiento.
    a_requerimiento  Administrativa → comercial: el requerimiento no alcanza para armar la propuesta.
    a_lista          Bodega → quien lista el material de la propuesta aprobada (comercial / administrativa).
    a_bodega         Área técnica → bodega: la instalación no tiene todo el material.

Cada devolución exige el motivo, avisa por correo interno al área anterior (al cliente no le llega nada), sale en
rojo en el «Mi día» de esa área y queda en el historial. Se cierra sola cuando el área anterior vuelve a enviar el
ticket por su camino normal, o con el botón «Listo, corregido», que lo regresa al punto exacto donde se devolvió.
"""
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from . import notificaciones as notif
from .models import Devolucion, Seguimiento, Ticket

E = Ticket.Estado
T = Devolucion.Tramo
MOTIVO_MINIMO = 10

# Para cada tramo: a quién va, el texto del botón y qué le toca hacer al que recibe la devolución
INFO = {
    T.A_TECNICA: {"de": "Comercial / administrativa", "a": "al área técnica", "boton": "Devolver al área técnica",
                  "ayuda": "El informe o el levantamiento técnico está incompleto o tiene errores (faltan equipos, "
                           "medidas, fotos…). Vuelve al técnico y a coordinación para completarlo.",
                  "corregir": "Complete el informe del requerimiento y vuelva a enviarlo a comercial."},
    T.A_COMERCIAL: {"de": "Área técnica", "a": "al área comercial", "boton": "Devolver a comercial",
                    "ayuda": "Faltan datos para la visita de levantamiento (dirección, contacto, qué revisar…). "
                             "Vuelve a comercial para completarlos.",
                    "corregir": "Complete los datos del requerimiento y vuelva a solicitar el levantamiento."},
    T.A_REQUERIMIENTO: {"de": "Administrativa", "a": "al área comercial", "boton": "Devolver a comercial",
                        "ayuda": "El requerimiento no alcanza para armar la propuesta. Vuelve al asesor comercial.",
                        "corregir": "Complete el requerimiento y su informe; al guardarlo vuelve a administrativa."},
    T.A_LISTA: {"de": "Bodega", "a": "a quien lista el material (comercial / administrativa)", "boton": "Devolver la lista de material",
                "ayuda": "La lista de materiales de la propuesta aprobada está mal o incompleta. La solicitud vuelve "
                         "a «Por completar» para corregirla.",
                "corregir": "Corrija la lista en la solicitud de material y vuelva a enviarla a bodega."},
    T.A_BODEGA: {"de": "Área técnica", "a": "a bodega", "boton": "Devolver a bodega",
                 "ayuda": "Falta material o equipos para la instalación. El ticket vuelve a «En inventario» para que "
                          "bodega lo complete.",
                 "corregir": "Agregue o aliste lo que falta; luego devuelva el ticket a técnica."},
}


def _ficha(ticket):
    return getattr(ticket, "ficha", None)


def _etapas():
    from comercial.models import FichaComercial
    return FichaComercial.Etapa


def _solicitud_en_validacion(ticket):
    from inventario.models import SolicitudMaterial
    return ticket.solicitudes_material.filter(origen=SolicitudMaterial.Origen.COMERCIAL,
                                              estado=SolicitudMaterial.Estado.VALIDACION).order_by("-pk").first()


# --- Qué se puede devolver --------------------------------------------------------------
def _aplica(ticket, tramo):
    """¿El ticket está en el punto en que ese tramo tiene sentido?"""
    ficha, Et = _ficha(ticket), _etapas()
    if ticket.estado in (E.CERRADO, E.CANCELADO):
        return False
    if tramo == T.A_TECNICA:
        return (ticket.area == Ticket.Area.COMERCIAL and ficha is not None and ficha.etapa in (Et.GESTION, Et.ELABORANDO)
                and bool(ficha.levantamiento or ticket.actas.exists()))
    if tramo == T.A_COMERCIAL:
        return (ticket.area == Ticket.Area.TECNICA and ficha is not None and ficha.etapa == Et.LEVANTAMIENTO
                and ticket.estado not in (E.VISITA_EJECUTADA, E.INFORME) and not ticket.actas.exists())
    if tramo == T.A_REQUERIMIENTO:
        return ticket.area == Ticket.Area.COMERCIAL and ficha is not None and ficha.etapa == Et.ELABORANDO
    if tramo == T.A_LISTA:
        return ticket.estado == E.EN_INVENTARIO and _solicitud_en_validacion(ticket) is not None
    if tramo == T.A_BODEGA:
        return (ticket.area == Ticket.Area.TECNICA and ficha is not None and ficha.etapa == Et.INSTALACION
                and ticket.estado in (E.RECEPCIONADO, E.EN_CRONOGRAMA, E.AUTORIZADO, E.PENDIENTE_VISITA)
                and ticket.solicitudes_material.exists())
    return False


def _puede_devolver(usuario, ticket, tramo):
    """¿El usuario es del área que tiene el ticket en ese tramo?"""
    if usuario.es_direccion:
        return True
    tecnica = usuario.es_supervisor or ticket.asignado_a_id == usuario.pk
    return {
        T.A_TECNICA: usuario.es_comercial or usuario.es_administrativo,
        T.A_COMERCIAL: tecnica,
        T.A_REQUERIMIENTO: usuario.es_administrativo,
        T.A_LISTA: usuario.es_bodega,
        T.A_BODEGA: tecnica,
    }[tramo]


def puede_resolver(usuario, devolucion):
    """¿El usuario es del área a la que se devolvió (la que corrige)? La lista de material se corrige en la solicitud."""
    if not devolucion.pendiente or devolucion.tramo == T.A_LISTA:
        return False
    if usuario.es_direccion:
        return True
    t = devolucion.ticket
    return {
        T.A_TECNICA: usuario.es_supervisor or t.asignado_a_id == usuario.pk,
        T.A_COMERCIAL: usuario.es_comercial,
        T.A_REQUERIMIENTO: usuario.es_comercial,
        T.A_BODEGA: usuario.es_bodega or usuario.es_supervisor,
    }[devolucion.tramo]


def opciones(ticket, usuario):
    """Los tramos en que este usuario puede devolver el ticket ahora: [(tramo, info)]."""
    if not usuario.is_authenticated or not usuario.es_interno:
        return []
    abiertos = set(ticket.devoluciones.filter(resuelta__isnull=True).values_list("tramo", flat=True))
    return [(tramo, INFO[tramo]) for tramo in T.values
            if tramo not in abiertos and _aplica(ticket, tramo) and _puede_devolver(usuario, ticket, tramo)]


def pendientes(ticket):
    """Devoluciones del ticket que el área anterior todavía no ha corregido."""
    return list(ticket.devoluciones.filter(resuelta__isnull=True).select_related("autor"))


# --- Devolver ---------------------------------------------------------------------------
def _tecnico_del_ticket(ticket):
    """El técnico que hizo la visita o el informe (para devolverle el trabajo a él)."""
    acta = ticket.actas.exclude(tecnico=None).order_by("-fecha").first()
    if acta:
        return acta.tecnico
    ficha = _ficha(ticket)
    informe = getattr(ficha, "informe", None) if ficha and ficha.informe_listo else None
    autor = informe.elaborado_por if informe else None
    return autor if autor and autor.es_interno and not (autor.es_comercial or autor.es_administrativo) else None


def _nombre(usuario):
    return (usuario.get_full_name() or usuario.username) if usuario else "—"


def _correos_destino(ticket, tramo, solicitud=None):
    """Quiénes deben corregir (correo interno)."""
    from comercial.servicios import _correos_administrativos, _correos_comerciales

    ficha = _ficha(ticket)
    if tramo == T.A_TECNICA:
        tecnico = ticket.asignado_a
        return ([tecnico.email] if tecnico and tecnico.email else []) + [settings.NOTIFICAR_SOPORTE_EMAIL]
    if tramo in (T.A_COMERCIAL, T.A_REQUERIMIENTO):
        return _correos_comerciales(ficha)
    if tramo == T.A_LISTA:
        quien = [solicitud.solicitado_por.email] if solicitud and solicitud.solicitado_por and solicitud.solicitado_por.email else []
        return quien + _correos_comerciales(ficha) + _correos_administrativos()
    if tramo == T.A_BODEGA:
        from inventario.solicitudes import R, _correos
        return _correos([R.BODEGA]) + [settings.NOTIFICAR_SOPORTE_EMAIL]
    return []


@transaction.atomic
def devolver(ticket, usuario, tramo, motivo):
    """Devuelve el ticket al área anterior con el motivo. Lanza ValidationError o PermissionDenied si no se puede."""
    motivo = (motivo or "").strip()
    if tramo not in T.values or not _aplica(ticket, tramo):
        raise ValidationError("El ticket ya no está en ese punto: recargue la página.")
    if not _puede_devolver(usuario, ticket, tramo):
        raise PermissionDenied
    if ticket.devoluciones.filter(tramo=tramo, resuelta__isnull=True).exists():
        raise ValidationError("Ya hay una devolución pendiente en ese tramo.")
    if len(motivo) < MOTIVO_MINIMO:
        raise ValidationError("Explique qué falta o qué hay que corregir (mínimo 10 caracteres).")
    ficha, Et, info = _ficha(ticket), _etapas(), INFO[tramo]
    dev = Devolucion(ticket=ticket, tramo=tramo, motivo=motivo, autor=usuario, estado_previo=ticket.estado,
                     area_previa=ticket.area, etapa_previa=ficha.etapa if ficha else "", asignado_previo=ticket.asignado_a)
    texto = f"Devuelto {info['a']} por {_nombre(usuario)}: {motivo}"
    solicitud = None

    if tramo == T.A_TECNICA:
        ticket.area, ticket.asignado_a = Ticket.Area.TECNICA, _tecnico_del_ticket(ticket)
        ticket.save(update_fields=["area", "asignado_a", "actualizado"])
        ticket.cambiar_estado(E.VISITA_EJECUTADA if ticket.actas.exists() else E.RECEPCIONADO, usuario, texto, interno=True)
        ficha.etapa = Et.LEVANTAMIENTO
        ficha.save(update_fields=["etapa", "actualizado"])
    elif tramo == T.A_COMERCIAL:
        ticket.area, ticket.asignado_a = Ticket.Area.COMERCIAL, None
        ticket.save(update_fields=["area", "asignado_a", "actualizado"])
        ticket.cambiar_estado(E.EN_PROCESO, usuario, texto, interno=True)
        ficha.etapa = Et.GESTION
        ficha.save(update_fields=["etapa", "actualizado"])
    elif tramo == T.A_REQUERIMIENTO:
        ficha.etapa = Et.GESTION
        ficha.save(update_fields=["etapa", "actualizado"])
    elif tramo == T.A_LISTA:
        from inventario.solicitudes import S, _nota
        solicitud = _solicitud_en_validacion(ticket)
        solicitud.estado = S.BORRADOR
        solicitud.motivo_devolucion = f"{_nombre(usuario)} (bodega): {motivo}"[:500]
        solicitud.ronda += 1
        solicitud.save()
        _nota(solicitud, usuario, f"devuelta por bodega para corregir la lista: {motivo}")
        dev.solicitud_id_material = solicitud.pk
    elif tramo == T.A_BODEGA:
        ticket.cambiar_estado(E.EN_INVENTARIO, usuario, texto, interno=True)
    dev.save()
    if tramo in (T.A_REQUERIMIENTO, T.A_LISTA):  # no cambian el estado del ticket: la constancia va aparte
        Seguimiento.objects.create(ticket=ticket, autor=usuario, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                                   mensaje=texto)

    enlace = solicitud.get_absolute_url() if solicitud else (ficha.get_absolute_url() if ficha and tramo in (
        T.A_COMERCIAL, T.A_REQUERIMIENTO) else ticket.get_absolute_url())
    notif._enviar(
        f"[{ticket.radicado}] Devuelto a su área: falta algo por corregir · {ticket.empresa}",
        "\n".join([f"{_nombre(usuario)} ({info['de']}) devolvió el ticket {ticket.radicado} «{ticket.asunto}».", "",
                   f"Qué falta o qué hay que corregir: {motivo}", "", info["corregir"], "",
                   f"Abrir: {settings.SITIO_URL.rstrip('/')}{enlace}"]),
        _correos_destino(ticket, tramo, solicitud))
    return dev


# --- Corregido: vuelve al área que lo devolvió -------------------------------------------
def _cerrar(dev, usuario, nota):
    dev.resuelta, dev.resuelta_por, dev.nota_resuelta = timezone.now(), usuario, (nota or "").strip()
    dev.save(update_fields=["resuelta", "resuelta_por", "nota_resuelta"])


def _avisar_corregido(dev, usuario, nota):
    t = dev.ticket
    destino = [dev.autor.email] if dev.autor and dev.autor.email else []
    notif._enviar(f"[{t.radicado}] Corregido: vuelve a su área · {t.empresa}",
                  "\n".join([f"{_nombre(usuario)} corrigió lo que usted devolvió del ticket {t.radicado} «{t.asunto}».",
                             f"Usted pidió: {dev.motivo}", f"Qué se corrigió: {nota or 'sin nota'}", "",
                             f"Abrir: {settings.SITIO_URL.rstrip('/')}{t.get_absolute_url()}"]), destino)


@transaction.atomic
def resolver(dev, usuario, nota):
    """Botón «Listo, corregido»: el ticket vuelve al punto exacto donde estaba cuando lo devolvieron."""
    if not puede_resolver(usuario, dev):
        raise PermissionDenied
    nota = (nota or "").strip()
    if len(nota) < 5:
        raise ValidationError("Cuente brevemente qué se corrigió.")
    t, ficha, Et = dev.ticket, _ficha(dev.ticket), _etapas()
    _cerrar(dev, usuario, nota)  # primero: así el cambio de estado no la vuelve a cerrar
    texto = f"Corregido por {_nombre(usuario)} y devuelto a quien lo devolvió ({INFO[dev.tramo]['de']}): {nota}"
    if dev.tramo in (T.A_TECNICA, T.A_COMERCIAL):
        t.area, t.asignado_a = dev.area_previa or t.area, dev.asignado_previo
        t.save(update_fields=["area", "asignado_a", "actualizado"])
        if dev.estado_previo and dev.estado_previo != t.estado:
            t.cambiar_estado(dev.estado_previo, usuario, texto, interno=True)
        else:
            Seguimiento.objects.create(ticket=t, autor=usuario, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True, mensaje=texto)
        if ficha and dev.etapa_previa:
            ficha.etapa = dev.etapa_previa
            ficha.save(update_fields=["etapa", "actualizado"])
    elif dev.tramo == T.A_REQUERIMIENTO:
        ficha.etapa = dev.etapa_previa or Et.ELABORANDO
        ficha.save(update_fields=["etapa", "actualizado"])
        Seguimiento.objects.create(ticket=t, autor=usuario, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True, mensaje=texto)
    elif dev.tramo == T.A_BODEGA:
        t.cambiar_estado(dev.estado_previo or E.RECEPCIONADO, usuario, texto, interno=True)
    _avisar_corregido(dev, usuario, nota)
    return dev


def cerrar_por_reenvio(ticket, tramo, usuario, nota):
    """El área anterior volvió a enviar el ticket por su camino normal: las devoluciones de ese tramo quedan corregidas."""
    for dev in ticket.devoluciones.filter(tramo=tramo, resuelta__isnull=True):
        _cerrar(dev, usuario, nota)
        _avisar_corregido(dev, usuario, nota)


# --- «Mi día»: lo devuelto a mi área ------------------------------------------------------
def para_corregir(usuario, filtro_tickets=None):
    """Devoluciones pendientes que le toca corregir al usuario (o a su área)."""
    qs = Devolucion.objects.filter(resuelta__isnull=True).select_related("ticket__empresa", "autor", "ticket__asignado_a")
    if filtro_tickets:
        qs = filtro_tickets(qs)
    salida = []
    for dev in qs.order_by("creada"):
        lista = dev.tramo == T.A_LISTA and (usuario.es_comercial or usuario.es_administrativo or usuario.es_direccion)
        if lista or puede_resolver(usuario, dev):
            salida.append(dev)
    return salida
