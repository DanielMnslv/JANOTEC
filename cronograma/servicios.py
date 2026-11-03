"""Reglas del cronograma: programar tickets, autorización de ingreso del cliente y aviso a SST."""
import logging
import os

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import formats, timezone

from tickets import notificaciones as notif
from tickets.models import Seguimiento, Ticket

from .models import Actividad, ConfiguracionEnvio, Contacto, DocumentoPersonal

log = logging.getLogger(__name__)
E = Ticket.Estado
A = Actividad.Autorizacion
SAL_AUTORIZACION = "cronograma.autorizacion"
DIAS_VALIDEZ_ENLACE = 30


def cuando(actividad):
    """«viernes, 24 de octubre de 2026, de 8:00 a. m. a 12:00 m.»"""
    from .semanal import fecha_larga, hora_corta

    return f"{fecha_larga(actividad.fecha)}, de {hora_corta(actividad.hora_inicio)} a {hora_corta(actividad.hora_fin)}"


def _seguimiento(ticket, autor, mensaje, interno=False):
    return Seguimiento.objects.create(ticket=ticket, autor=autor, mensaje=mensaje, interno=interno,
                                      tipo=Seguimiento.Tipo.NOTA_INTERNA if interno else Seguimiento.Tipo.COMENTARIO)


def _avisar_comercial(ticket, asunto, lineas):
    """Si el ticket es un levantamiento del área comercial, el asesor se entera de lo que pasa con la visita."""
    ficha = getattr(ticket, "ficha", None) if ticket.pk else None
    if ficha is None or ficha.etapa != "levantamiento":
        return
    from comercial.servicios import _correos_comerciales

    notif._enviar(f"[{ticket.radicado}] {asunto} · {ticket.empresa}",
                  "\n".join(lineas + ["", f"Ver el requerimiento: {settings.SITIO_URL.rstrip('/')}{ficha.get_absolute_url()}"]),
                  _correos_comerciales(ficha))


# --- Programar y reprogramar -----------------------------------------------------
def avisar_tecnicos(actividad, tecnicos_antes, reprogramada, quedan=None):
    """Correo a cada técnico: visita nueva para él, cambio de horario o que ya no va (`quedan=[]` al quitarla)."""
    ahora = {u.pk: u for u in (actividad.tecnicos() if quedan is None else quedan)}
    antes = {u.pk: u for u in tecnicos_antes}
    t = actividad.ticket
    detalle = [
        f"Cliente: {actividad.cliente}" + (f" · Sede: {actividad.sede}" if actividad.sede else ""),
        f"Cuándo: {cuando(actividad)}",
        f"Actividad: {actividad.actividad}",
        f"Tipo: {actividad.get_tipo_display()}",
    ]
    if t:
        if t.ubicacion:
            detalle.append(f"Dirección: {t.ubicacion}")
        if t.contacto_nombre or t.contacto_telefono:
            detalle.append(f"Contacto en sitio: {t.contacto_nombre} {t.contacto_telefono}".strip())
    companeros = ", ".join(u.get_full_name() or u.username for u in ahora.values())
    if companeros:
        detalle.append(f"Técnicos: {companeros}")
    if actividad.insumos:
        detalle.append(f"Insumos y equipos: {actividad.insumos}")
    if actividad.trabajo_alturas:
        detalle.append("TRABAJO EN ALTURAS: lleve el equipo y los certificados.")
    if actividad.sst:
        detalle.append("Con acompañamiento SST (HSE).")
    enlace = f"{settings.SITIO_URL.rstrip('/')}{t.get_absolute_url()}" if t else f"{settings.SITIO_URL.rstrip('/')}{reverse('cronograma:semana')}"
    referencia = f"[{t.radicado}] " if t else ""

    def enviar(usuarios, asunto, intro):
        for u in usuarios:
            if u.email:
                notif._enviar(f"{referencia}{asunto} · {actividad.cliente}",
                              "\n".join([f"Hola {u.first_name or u.username},", "", intro, "", *detalle, "",
                                          f"Ver el detalle: {enlace}", "", "— Coordinación TIC · JANOTEC"]),
                              [u.email])

    enviar([u for pk, u in ahora.items() if pk not in antes], "Se le programó una visita",
           "El coordinador TIC lo agendó en el cronograma para esta visita:")
    if reprogramada:
        enviar([u for pk, u in ahora.items() if pk in antes], "Visita reprogramada",
               "Cambió el horario de su visita. Quedó así:")
    enviar([u for pk, u in antes.items() if pk not in ahora], "Ya no tiene esta visita",
           "Lo retiraron de esta visita del cronograma (ya no debe asistir):")


def guardar(actividad, autor, horario_anterior=None, tecnicos_antes=None):
    """Después de crear o editar una actividad: cruce con la disponibilidad y estado del ticket.

    `horario_anterior` = (fecha, inicio, fin) antes de editar; si cambió y ya se había enviado la
    autorización, hay que volver a pedirla (los enlaces anteriores dejan de valer).
    """
    actividad.calcular_choque()
    material = actividad.material_de_bodega
    if material is not None:
        actividad.insumos = material["texto"]  # lo que bodega entregó; lo que falte va como adicional en su lista
    reprogramada = horario_anterior is not None and horario_anterior != (
        actividad.fecha, actividad.hora_inicio, actividad.hora_fin)
    if reprogramada:
        actividad.sst_enviada = None  # la solicitud SST debe volver a salir con la nueva fecha
    if reprogramada and actividad.autorizacion != A.SIN_ENVIAR:
        actividad.autorizacion = A.SIN_ENVIAR
        actividad.version_enlace += 1
        actividad.motivo_rechazo = ""
    actividad.save()
    if tecnicos_antes is not None or reprogramada:
        antes = list(actividad.tecnicos()) if tecnicos_antes is None else tecnicos_antes
        transaction.on_commit(lambda: avisar_tecnicos(actividad, antes, reprogramada))
    ticket = actividad.ticket
    if not ticket:
        return actividad
    with transaction.atomic():
        principal = actividad.encargados.first()
        ticket.asignado_a = principal or ticket.asignado_a
        ticket.visita_programada = actividad.inicio
        ticket.save(update_fields=["asignado_a", "visita_programada", "actualizado"])
        if ticket.estado in Ticket.ESTADOS_PROGRAMABLES or ticket.estado == E.AUTORIZADO and reprogramada:
            tecnicos = ", ".join(u.get_full_name() or u.username for u in actividad.tecnicos())
            seg = ticket.cambiar_estado(
                E.EN_CRONOGRAMA, autor,
                f"Programamos la visita técnica para el {cuando(actividad)}"
                + (f" con {tecnicos}" if tecnicos else "") + ". Le enviaremos la autorización de ingreso para que la acepte.",
            )
            notif.actualizacion_para_cliente(ticket, seg)
            _avisar_comercial(ticket, "Levantamiento programado", [
                f"El área técnica programó la visita de levantamiento de «{ticket.asunto}» para el {cuando(actividad)}"
                + (f" con {tecnicos}." if tecnicos else "."), "Al terminar, el técnico le devuelve el resultado."])
        elif reprogramada:
            _seguimiento(ticket, autor, f"Visita reprogramada para el {cuando(actividad)}.")
            _avisar_comercial(ticket, "Levantamiento reprogramado",
                              [f"La visita de levantamiento de «{ticket.asunto}» se movió para el {cuando(actividad)}."])
        if actividad.choque:
            _seguimiento(ticket, autor, f"Programada pese a la restricción del cliente: {actividad.choque}", interno=True)
        # El material planeado se pide a bodega (validación, compra si falta y firmas). No en la 1.ª visita
        # (levantamiento: aún no se sabe qué se instalará) ni en la instalación que ya alistó bodega.
        if actividad.tipo != actividad.Tipo.VISITA and material is None:
            from inventario.solicitudes import desde_actividad
            desde_actividad(actividad, autor)
    return actividad


def quitar(actividad, autor):
    """Elimina la actividad; si el ticket ya no tiene otra, vuelve a «Recepcionado» para reprogramarlo."""
    ticket = actividad.ticket
    avisar_tecnicos(actividad, list(actividad.tecnicos()), False, quedan=[])
    actividad.delete()
    if ticket and not ticket.actividades.exists() and ticket.estado in (E.EN_CRONOGRAMA, E.AUTORIZADO):
        ticket.visita_programada = None
        ticket.save(update_fields=["visita_programada", "actualizado"])
        ticket.cambiar_estado(E.RECEPCIONADO, autor, "Se retiró del cronograma: la reprogramaremos.", interno=True)


def aplazar_visita(ticket, autor, motivo, detalle, informar_cliente=False):
    """El coordinador aplaza la visita programada: sale del cronograma y el ticket vuelve al inicio del flujo
    («Recepcionado», listo para reprogramar) con el mismo radicado, sus datos, su historial y el porqué.

    Se avisa a los técnicos (y a SST si ya se le había pedido acompañamiento). El enlace de autorización que
    tenía el cliente deja de servir porque la actividad se retira.
    """
    from django.db.models import Q

    from tickets.models import Aplazamiento

    if ticket.estado not in (E.EN_CRONOGRAMA, E.AUTORIZADO):
        raise ValueError("Solo se aplaza una visita que está programada en el cronograma.")
    visita = ticket.visita_programada
    filtro = Q(fecha__gte=timezone.localdate())
    if visita:
        filtro |= Q(fecha=timezone.localtime(visita).date())
    actividades = list(ticket.actividades.filter(filtro).prefetch_related("encargados"))
    tecnicos = {t for a in actividades for t in a.tecnicos()}
    programacion = " · ".join(
        f"{cuando(a)}" + (f" con {', '.join(t.get_full_name() or t.username for t in a.tecnicos())}" if a.tecnicos() else "")
        for a in actividades)
    correos_autorizacion = {c for a in actividades if a.autorizacion != A.SIN_ENVIAR for c in a.correos}
    avisar_sst = any(a.sst_enviada for a in actividades)
    motivo_texto = dict(Aplazamiento.Motivo.choices)[motivo]

    with transaction.atomic():
        numero = ticket.veces_aplazado + 1
        aplazamiento = Aplazamiento.objects.create(
            ticket=ticket, numero=numero, motivo=motivo, detalle=detalle, visita_aplazada=visita,
            programacion=programacion[:500], informado_cliente=informar_cliente, autor=autor)
        for a in actividades:
            a.delete()
        ticket.visita_programada = None
        ticket.veces_aplazado = numero
        ticket.save(update_fields=["visita_programada", "veces_aplazado", "actualizado"])
        seg = ticket.reingresar(
            autor,
            f"VISITA APLAZADA (aplazamiento n.º {numero}) · {motivo_texto}\n{detalle}"
            + (f"\nEstaba programada: {programacion}" if programacion else "")
            + "\nEl ticket volvió a «Recepcionado»: queda por programar de nuevo en el cronograma.")
        seg.tipo = Seguimiento.Tipo.APLAZAMIENTO
        seg.save(update_fields=["tipo"])
        visible = None
        if informar_cliente:
            visible = _seguimiento(
                ticket, autor,
                "Su visita técnica" + (f" del {cuando(actividades[0])}" if actividades else "") + " fue aplazada.\n"
                f"Motivo: {motivo_texto}. {detalle}\n"
                "La reprogramaremos y le enviaremos una nueva autorización de ingreso.")

    asunto = f"[{ticket.radicado}] Visita aplazada · {ticket.empresa}"
    cuerpo = "\n".join([
        f"{autor.get_full_name() or autor.username} aplazó la visita de «{ticket.asunto}».",
        f"Estaba programada: {programacion or 'sin actividad en el cronograma'}", "",
        f"Motivo: {motivo_texto}", detalle, "",
        "No asista a esta visita: el ticket volvió a «Recepcionado» y se reprogramará.",
        f"{settings.SITIO_URL.rstrip('/')}{ticket.get_absolute_url()}"])
    notif._enviar(asunto, cuerpo, [t.email for t in tecnicos if t.email])
    if avisar_sst and correo_sst():
        notif._enviar(f"[{ticket.radicado}] Se cancela la solicitud de acompañamiento SST", cuerpo, [correo_sst()])
    _avisar_comercial(ticket, "Levantamiento aplazado", [
        f"Se aplazó la visita de levantamiento de «{ticket.asunto}».", f"Motivo: {motivo_texto}. {detalle}",
        "El área técnica la recepciona y la programa de nuevo."])
    if visible:
        notif.actualizacion_para_cliente(ticket, visible)
        otros = correos_autorizacion - set(notif.correos_cliente(ticket))
        if otros:
            notif._enviar(f"[{ticket.radicado}] Visita aplazada", visible.mensaje + "\n\n— Área de Soporte JANOTEC", list(otros))
    return aplazamiento


# --- Documentos de los técnicos -------------------------------------------------------
def documentos_para(actividad):
    """(documentos a adjuntar, problemas) según los técnicos y si la actividad es en alturas."""
    tipos = list(DocumentoPersonal.TIPOS_SIEMPRE)
    if actividad.trabajo_alturas:
        tipos.append(DocumentoPersonal.Tipo.ALTURAS)
    adjuntos, problemas = [], []
    for tecnico in actividad.tecnicos():
        actuales = DocumentoPersonal.actuales(tecnico)
        nombre = tecnico.get_full_name() or tecnico.username
        for tipo in tipos:
            doc = actuales.get(tipo)
            etiqueta = DocumentoPersonal.Tipo(tipo).label.lower()
            if not doc:
                problemas.append(f"{nombre}: falta {etiqueta}.")
                continue
            if doc.vencido:
                problemas.append(f"{nombre}: {etiqueta} vencido el {doc.vigente_hasta:%d/%m/%Y}.")
            adjuntos.append(doc)
    return adjuntos, problemas


# --- Autorización de ingreso -------------------------------------------------------
def token_autorizacion(actividad):
    return signing.dumps({"a": actividad.pk, "v": actividad.version_enlace}, salt=SAL_AUTORIZACION)


def actividad_de_token(token):
    """La actividad del enlace, o None si el enlace es inválido, venció o la visita se reprogramó."""
    try:
        datos = signing.loads(token, salt=SAL_AUTORIZACION, max_age=DIAS_VALIDEZ_ENLACE * 24 * 3600)
    except signing.BadSignature:
        return None
    actividad = Actividad.objects.filter(pk=datos.get("a")).select_related("ticket").first()
    if not actividad or actividad.version_enlace != datos.get("v"):
        return None
    return actividad


def enviar_autorizacion(actividad, autor):
    """Correo al cliente con día, hora, técnicos y los botones «Aceptar» / «Rechazar».

    Adjunta el documento de identidad y los aportes a seguridad social de cada técnico y,
    si la actividad es en alturas, sus certificados de trabajo seguro en alturas.
    """
    adjuntos, problemas = documentos_para(actividad)
    base = settings.SITIO_URL.rstrip("/")
    enlace = base + reverse("cronograma:autorizacion", args=[token_autorizacion(actividad)])
    contexto = {"a": actividad, "cuando": cuando(actividad), "tecnicos": actividad.tecnicos(),
                "aceptar": enlace + "?r=aceptar", "rechazar": enlace + "?r=rechazar", "ticket": actividad.ticket,
                "adjuntos": adjuntos}
    asunto = f"Autorización de ingreso · {actividad.cliente} · {formats.date_format(actividad.fecha, 'j M Y')}"
    if actividad.ticket:
        asunto = f"[{actividad.ticket.radicado}] " + asunto
    correo = EmailMultiAlternatives(
        asunto, render_to_string("cronograma/correo_autorizacion.txt", contexto), settings.DEFAULT_FROM_EMAIL,
        actividad.correos, cc=[c for c in [settings.NOTIFICAR_SOPORTE_EMAIL] if c],
    )
    correo.attach_alternative(render_to_string("cronograma/correo_autorizacion.html", contexto), "text/html")
    for doc in adjuntos:
        try:
            with doc.archivo.open("rb") as f:
                ext = os.path.splitext(doc.archivo.name)[1]
                nombre = f"{doc.get_tipo_display()} - {doc.usuario.get_full_name() or doc.usuario.username}{ext}"
                correo.attach(nombre, f.read())
        except FileNotFoundError:
            problemas.append(f"No se encontró el archivo de {doc}.")
    correo.send(fail_silently=False)
    actividad.autorizacion = A.ENVIADA
    actividad.autorizacion_enviada = timezone.now()
    actividad.motivo_rechazo = ""
    actividad.save(update_fields=["autorizacion", "autorizacion_enviada", "motivo_rechazo", "actualizado"])
    if actividad.ticket:
        _seguimiento(actividad.ticket, autor,
                     f"Enviamos la autorización de ingreso para el {cuando(actividad)} a: {', '.join(actividad.correos)}.")
    return problemas


def responder_autorizacion(actividad, aceptada, motivo=""):
    """El cliente acepta o rechaza desde el correo. Rechazada ⇒ la actividad queda en rojo en el cronograma."""
    actividad.autorizacion = A.ACEPTADA if aceptada else A.RECHAZADA
    actividad.autorizacion_respuesta = timezone.now()
    actividad.motivo_rechazo = "" if aceptada else motivo[:300]
    actividad.save(update_fields=["autorizacion", "autorizacion_respuesta", "motivo_rechazo", "actualizado"])
    ticket = actividad.ticket
    if ticket:
        if aceptada and ticket.estado == E.EN_CRONOGRAMA:
            ticket.cambiar_estado(E.AUTORIZADO, None, f"El cliente autorizó el ingreso para el {cuando(actividad)}.")
        elif not aceptada:
            _seguimiento(ticket, None, f"El cliente rechazó la visita del {cuando(actividad)}"
                                       + (f": {motivo}" if motivo else ".") + " La reprogramaremos.")
    resultado = "ACEPTÓ" if aceptada else "RECHAZÓ"
    destinos = [settings.NOTIFICAR_SOPORTE_EMAIL] + ([actividad.creado_por.email] if actividad.creado_por else [])
    notif._enviar(
        f"[Cronograma] El cliente {resultado} la visita · {actividad.cliente} · {actividad.fecha:%d/%m/%Y}",
        "\n".join([f"{actividad.cliente} · {actividad.sede}", f"Visita: {cuando(actividad)}",
                   f"Respuesta: {resultado}" + (f" — {motivo}" if motivo else ""), "",
                   f"Ver cronograma: {settings.SITIO_URL.rstrip('/')}{reverse('cronograma:semana')}?d={actividad.fecha:%Y-%m-%d}"]),
        destinos,
    )
    if aceptada and actividad.sst and not actividad.sst_enviada:  # si ya salió en el envío de la semana, no se repite
        avisar_sst(actividad)


def texto_sst(actividad):
    """El mismo texto de solicitud HSE que generaba la plataforma anterior."""
    persona = actividad.sst_persona or "[persona HSE]"
    lugar = f", {actividad.sede}" if actividad.sede else ""
    trabajo = actividad.sst_trabajo or ("trabajos en alturas" if actividad.trabajo_alturas else "las labores programadas")
    viaticos = ("Se solicita gestionar los viáticos correspondientes para el desplazamiento del personal."
                if actividad.viaticos else "Se informa que no se requiere solicitud de viáticos para esta actividad.")
    vehiculo = " Así mismo, se requiere la asignación de vehículo para el desplazamiento del personal." if actividad.vehiculo else ""
    tecnicos = ", ".join(u.get_full_name() or u.username for u in actividad.tecnicos())
    return "\n".join([
        "Cordial saludo.", "",
        f"Por medio del presente, y de manera atenta, solicito el acompañamiento del señor {persona} para la actividad "
        f"programada el {cuando(actividad)}, en el cliente {actividad.cliente}{lugar}.", "",
        f"El acompañamiento es requerido en calidad de HSE, debido a que durante la actividad se realizarán {trabajo}, "
        "con el fin de garantizar el cumplimiento de las condiciones y procedimientos de seguridad establecidos para este tipo de labores.",
        "", f"La actividad está contemplada para desarrollarse durante {actividad.sst_duracion}.",
        f"Personal técnico: {tecnicos or '—'}.", f"Actividad: {actividad.actividad}", "",
        f"{viaticos}{vehiculo}", "",
        "El cliente ya autorizó el ingreso.", "", "Agradezco de antemano su apoyo y gestión. Quedo atento a sus comentarios.",
        "", "Cordialmente,", ConfiguracionEnvio.actual().remitente,
    ])


def correo_sst():
    return Contacto.correo_de(Contacto.Tipo.SST, settings.NOTIFICAR_SST_EMAIL)


def avisar_sst(actividad):
    """Envía la solicitud de acompañamiento HSE al área SST del Directorio (una vez por fecha programada)."""
    from . import semanal

    destino = correo_sst()
    if not destino:
        log.warning("Actividad %s requiere SST pero no hay correo SST en el directorio", actividad.pk)
        notif._enviar(
            f"[Cronograma] Falta el correo de SST · {actividad.cliente} · {actividad.fecha:%d/%m/%Y}",
            "\n".join([f"La visita del {cuando(actividad)} a {actividad.cliente} necesita acompañamiento SST (HSE), "
                        "pero no se pudo pedir porque el área SST no tiene correo.",
                        "Agregue el correo en Cronograma → Directorio (contacto «SST») y envíe la solicitud desde la actividad.",
                        "", f"Ver cronograma: {settings.SITIO_URL.rstrip('/')}{reverse('cronograma:semana')}?d={actividad.fecha:%Y-%m-%d}"]),
            [settings.NOTIFICAR_SOPORTE_EMAIL])
        if actividad.ticket:
            _seguimiento(actividad.ticket, None, "No se pudo pedir el acompañamiento SST: falta el correo del área SST "
                                                 "en el Directorio.", interno=True)
        return
    semanal._enviar([semanal.correo_sst_de(actividad, destino)])
    if actividad.ticket:
        _seguimiento(actividad.ticket, None, f"Se envió la solicitud de acompañamiento SST a {destino}.", interno=True)
