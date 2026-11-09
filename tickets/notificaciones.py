"""Notificaciones por correo. Nunca interrumpen la operación si el correo falla."""
import logging
import re

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

log = logging.getLogger(__name__)


def _enlace(ticket):
    """Enlace completo a la ficha del ticket en la plataforma (SITIO_URL)."""
    return f"{settings.SITIO_URL.rstrip('/')}{ticket.get_absolute_url()}"


def _consulta(ticket):
    """Enlace con que el cliente consulta su ticket sin usuario ni contraseña."""
    from .consulta import enlace_consulta
    return enlace_consulta(ticket)


def correos_cliente(ticket):
    """A quién le llegan las respuestas: los correos de la solicitud (y el usuario cliente que la radicó)."""
    correos = list(ticket.correos_respuesta)
    if ticket.solicitante.es_cliente and ticket.solicitante.email:
        correos.append(ticket.solicitante.email)
    return correos


_DATO = re.compile(r"^([^:\n]{2,40}):\s+(\S.*)$")
_URL = re.compile(r"https?://\S+$")
_DESPEDIDA = re.compile(r"(?:^|\n)\s*(?:Cordialmente|Atentamente|Saludos)[,.]?\s*\n(.+)\Z", re.S | re.I)
_SALUDO = re.compile(r"^(Señor(es|a)?\b|Cordial saludo|Buen(os|as) (días|tardes|noches)|Hola\b|Estimad[oa]s?\b).*$", re.I)


def _firma_de_quien_actua():
    """«Nombre · área» de quien hizo la acción (si es del personal); si no, la plataforma."""
    from config.correo import _actor, _es_interno

    usuario = _actor.get()
    if _es_interno(usuario):
        return f"{usuario.get_full_name() or usuario.username} · {usuario.rol_corto}"
    return "Plataforma de soporte"


def html_desde_texto(asunto, cuerpo):
    """Los avisos internos se escriben en texto plano; aquí toman la misma presentación de los correos al cliente.

    Bloques separados por línea en blanco: si todas sus líneas son «Etiqueta: valor» van a la tabla de datos,
    el primer «Etiqueta: enlace» se vuelve el botón, y una línea final «— Área …» es la firma.
    """
    radicado = (re.search(r"JNT-\d{4}-\d{6}", asunto) or [""])[0]
    titulo = re.sub(r"^(\[[^\]]*\]\s*)+", "", asunto).strip() or asunto
    parrafos, datos, boton, firma = [], [], None, _firma_de_quien_actua()
    saludo = "Cordial saludo."
    cuerpo = cuerpo.strip()
    # Si el texto ya trae su despedida («Cordialmente,» y debajo quien firma), esa es la firma.
    despedida = _DESPEDIDA.search(cuerpo)
    quien_firma = [l.strip() for l in despedida[1].splitlines() if l.strip()] if despedida else []
    if 0 < len(quien_firma) <= 4:
        cuerpo, firma = cuerpo[:despedida.start()].strip(), " · ".join(quien_firma)
    # Si empieza con su propio saludo («Señores …:», «Buenos días»), ese reemplaza al de siempre.
    primera, _, resto = cuerpo.partition("\n")
    if _SALUDO.match(primera.strip()):
        saludo, cuerpo = primera.strip(), resto.strip()
    for bloque in re.split(r"\n\s*\n", cuerpo):
        lineas = [l for l in bloque.splitlines() if l.strip()]
        sueltas = []
        for linea in lineas:
            texto = linea.strip()
            dato = _DATO.match(texto)
            if dato and _URL.match(dato[2]) and not boton:
                boton = (dato[1], dato[2])
            elif dato and not texto.startswith(("-", "•", "■")):
                partes = [p for p in texto.split(" · ")]
                pares = [_DATO.match(p) for p in partes]
                datos += [(m[1], m[2]) for m in pares] if all(pares) else [(dato[1], dato[2])]
            elif texto.startswith("—") and linea is lineas[-1]:
                firma = texto.lstrip("— ").strip()
            else:
                sueltas.append(linea.rstrip())
        if sueltas:
            parrafos.append("\n".join(sueltas))
    contexto = {"titulo": titulo, "saludo": saludo, "parrafos": parrafos, "datos": datos, "radicado": radicado,
                "firma": firma, "boton_texto": boton[0] if boton else "", "boton_url": boton[1] if boton else "",
                "resumen": (parrafos[0] if parrafos else titulo)[:140], "estrellas": [], "acciones": []}
    return render_to_string("correos/cliente.html", contexto)


def _enviar(asunto, cuerpo, destinatarios):
    """Envía un correo interno en texto y HTML; si falla, solo queda en el registro y la operación sigue."""
    destinatarios = [d for d in set(destinatarios) if d]
    if not destinatarios:
        return
    try:
        correo = EmailMultiAlternatives(asunto, cuerpo, settings.DEFAULT_FROM_EMAIL, destinatarios)
        correo.attach_alternative(html_desde_texto(asunto, cuerpo), "text/html")
        correo.send(fail_silently=False)
    except Exception:  # noqa: BLE001 — el correo nunca debe tumbar la solicitud
        log.exception("No se pudo enviar la notificación «%s»", asunto)


def enviar_al_cliente(asunto, destinatarios, titulo, parrafos, saludo="", datos=(), destacado="", aviso="",
                      boton=None, radicado="", estado="", firma="Área de Soporte Técnico", adjuntos=(), calificar=None,
                      acciones=()):
    """Correo formal para el cliente: HTML con la marca de JANOTEC y la misma información en texto plano.

    `destinatarios`: el primero es el principal («Para»); los demás van en copia («CC»).
    `boton` = (texto, url); `adjuntos` = [(nombre, contenido, tipo)]; `calificar` = ticket cuyo correo lleva
    las 10 estrellas para calificar la atención. Nunca interrumpe la operación si falla.
    """
    destinatarios = [d for d in dict.fromkeys(destinatarios) if d]
    if not destinatarios:
        return
    saludo = saludo or "Cordial saludo."
    contexto = {"titulo": titulo, "saludo": saludo, "parrafos": [p for p in parrafos if p], "datos": [d for d in datos if d[1]],
                "destacado": destacado, "aviso": aviso, "radicado": radicado, "estado": estado, "firma": firma,
                "boton_texto": boton[0] if boton else "", "boton_url": boton[1] if boton else "",
                "resumen": next((p for p in parrafos if p), titulo)[:140], "estrellas": [], "acciones": list(acciones)}
    if calificar is not None:
        from .calificacion import enlace
        contexto["estrellas"] = [(n, enlace(calificar, n)) for n in range(1, calificar.ESCALA_CALIFICACION + 1)]
    texto = [titulo + (f" · {radicado}" if radicado else ""), "", saludo, ""]
    texto += [p + "\n" for p in contexto["parrafos"]]
    if destacado:
        texto += [destacado, ""]
    texto += [f"{e}: {v}" for e, v in contexto["datos"]]
    if aviso:
        texto += ["", aviso]
    if boton:
        texto += ["", f"{boton[0]}: {boton[1]}"]
    if contexto["estrellas"]:
        texto += ["", "Califique nuestra atención de 1 a 10:", contexto["estrellas"][-1][1].split("?")[0]]
    for nombre, url, _ in contexto["acciones"]:
        texto += [f"{nombre}: {url}"]
    texto += ["", "Cordialmente,", firma, "JANOTEC S.A.S.", "",
              "Cra 40A N.º 16-21, Barrio Villa María · Villavicencio, Meta · Tel: 310 229 8160 · janotec@janotec.app"]
    try:
        # Como en un correo normal: el primero (quien gestiona) en «Para» y los demás en copia
        correo = EmailMultiAlternatives(asunto, "\n".join(texto), settings.DEFAULT_FROM_EMAIL, destinatarios[:1],
                                        cc=destinatarios[1:])
        correo.attach_alternative(render_to_string("correos/cliente.html", contexto), "text/html")
        for nombre, contenido, tipo in adjuntos:
            correo.attach(nombre, contenido, tipo)
        correo.send(fail_silently=False)
    except Exception:  # noqa: BLE001 — el correo nunca debe tumbar la solicitud
        log.exception("No se pudo enviar la notificación «%s»", asunto)


def _saludo(ticket):
    """Saludo con el nombre del contacto (si viene todo en mayúsculas se pasa a nombre propio)."""
    nombre = (ticket.contacto_nombre or "").strip()
    return f"Cordial saludo, {nombre.title() if nombre.isupper() else nombre}." if nombre else "Cordial saludo."


def _datos_solicitud(ticket):
    """Los datos básicos de la solicitud que van en los correos al cliente."""
    return [("Solicitud", ticket.asunto), ("Cliente", str(ticket.empresa) + (f" · {ticket.sede}" if ticket.sede else "")),
            ("Tipo de servicio", f"{ticket.get_tipo_display()} · {ticket.sistema_display}")]


# Los únicos cambios de estado que se le avisan al cliente (además del registro, que avisa ticket_creado).
# Las demás etapas (cronograma, autorizado, visita, informe…) las ve en «Consultar mi solicitud».
ETAPAS_CLIENTE = {
    "recepcionado": (
        "Su solicitud está en proceso",
        ["Recibimos y validamos su solicitud. Ya se encuentra en proceso de atención por parte de nuestro equipo técnico.",
         "Le informaremos por este medio cuando quede solucionada. Mientras tanto puede consultar el avance en "
         "cualquier momento con su número de radicado."],
    ),
    "cerrado": (
        "Su solicitud fue solucionada y cerrada",
        ["Le informamos que su solicitud fue atendida, solucionada y cerrada.",
         "Gracias por confiar en JANOTEC. Si el inconveniente se vuelve a presentar o necesita algo más, "
         "puede registrar una nueva solicitud o responder a este correo citando el número de radicado."],
    ),
}


def ticket_creado(ticket):
    """Correo al cliente con su radicado cuando registra la solicitud."""
    enviar_al_cliente(
        f"[{ticket.radicado}] Solicitud registrada: {ticket.asunto}",
        correos_cliente(ticket),
        "Su solicitud fue registrada",
        [f"Registramos su solicitud con el número de radicado {ticket.radicado}. Con este número puede consultar "
         "el avance en cualquier momento, sin usuario ni contraseña.",
         "Nuestro equipo técnico la revisará y le avisaremos por este medio cuando esté en proceso de atención "
         "y cuando quede solucionada."],
        saludo=_saludo(ticket), radicado=ticket.radicado, estado="Generado",
        datos=_datos_solicitud(ticket) + [
            ("Descripción", ticket.descripcion),
            ("Días sin disponibilidad", ticket.resumen_no_disponible),
            ("Contacto en sitio", " · ".join(x for x in (ticket.contacto_nombre, ticket.contacto_telefono) if x))],
        boton=("Consultar mi solicitud", _consulta(ticket)),
    )
    _enviar(
        f"[{ticket.radicado}] {ticket.get_tipo_display()} de {ticket.empresa} — por recepcionar",
        (
            f"{ticket.asunto}\n\n{ticket.descripcion}\n\n"
            + (f"Falla: {ticket.falla_display}\n" if ticket.falla_display else "")
            + f"Contacto: {ticket.contacto_nombre} · {ticket.contacto_telefono} · {ticket.contacto_email}\n"
            + (f"Jornada pedida: {ticket.get_jornada_display()}\n" if ticket.jornada else "")
            + (f"NO puede atender: {ticket.resumen_no_disponible}\n" if ticket.resumen_no_disponible else "")
            + (f"Otras restricciones: {ticket.restricciones}\n" if ticket.restricciones else "")
            + ("TRABAJO EN ALTURAS (más de 2 m)\n" if ticket.trabajo_alturas else "")
            + ("Requiere acompañamiento SST\n" if ticket.acompanamiento_sst else "")
            + (f"Registrado por: {ticket.radicado_por} ({ticket.radicado_por.rol_corto})\n" if ticket.radicado_por else "")
            + f"Prioridad sugerida: {ticket.get_prioridad_display()}\n\nRecepcionar: {_enlace(ticket)}"
        ),
        [settings.NOTIFICAR_SOPORTE_EMAIL],
    )


def actualizacion_para_cliente(ticket, seguimiento):
    """Avisa al cliente cuando su solicitud entra en proceso (recepcionada) o se cierra.

    Los demás cambios de estado no le llegan por correo. Una respuesta escrita para el cliente (sin cambio
    de estado) sí le llega, porque alguien del equipo decidió escribirle.
    """
    if seguimiento is None or seguimiento.interno:
        return
    destacado = seguimiento.mensaje
    if seguimiento.estado_nuevo:
        if seguimiento.estado_nuevo not in ETAPAS_CLIENTE or ticket.estado != seguimiento.estado_nuevo:
            return
        titulo, parrafos = ETAPAS_CLIENTE[ticket.estado]
        if ticket.estado == ticket.Estado.RECEPCIONADO:
            destacado = ""  # el mensaje automático de recepción repite lo que ya dicen los párrafos
    else:
        titulo, parrafos = "Tenemos una respuesta a su solicitud", []
    calificar = ticket.estado == ticket.Estado.CERRADO and not ticket.calificacion
    enviar_al_cliente(
        f"[{ticket.radicado}] {titulo}: {ticket.asunto}", correos_cliente(ticket), titulo, parrafos,
        saludo=_saludo(ticket), radicado=ticket.radicado, estado=ticket.get_estado_display(),
        destacado=destacado, datos=_datos_solicitud(ticket),
        # Al cerrar, en lugar del botón de consulta el cliente califica directamente con 10 estrellas
        boton=None if calificar else ("Consultar mi solicitud", _consulta(ticket)),
        calificar=ticket if calificar else None,
    )


def respuesta_del_cliente(ticket, seguimiento):
    """Avisa al técnico encargado (o a soporte, si no tiene) que el cliente respondió en el ticket."""
    destino = ticket.asignado_a.email if ticket.asignado_a else settings.NOTIFICAR_SOPORTE_EMAIL
    _enviar(
        f"[{ticket.radicado}] El cliente respondió: {ticket.asunto}",
        f"{seguimiento.autor} escribió:\n\n{seguimiento.mensaje}\n\nGestionar: {_enlace(ticket)}",
        [destino],
    )


def informe_para_aprobar(borrador, autor):
    """A dirección: coordinación envió un informe técnico para que lo revise y lo apruebe."""
    from accounts.models import Usuario

    ticket = borrador.ticket
    direccion = Usuario.objects.filter(rol=Usuario.Rol.DIRECCION, is_active=True).values_list("email", flat=True)
    _enviar(
        f"[{ticket.radicado}] Informe técnico para aprobar: {ticket.asunto}",
        "\n".join([f"{autor.get_full_name() or autor.username} envió el {borrador.get_tipo_display().split(' (')[0].lower()} "
                    "para su revisión y aprobación.", "",
                    f"Empresa: {ticket.empresa}" + (f" · Sede: {ticket.sede}" if ticket.sede else ""), "",
                    "Puede corregirlo, aprobarlo (queda como informe oficial) o devolverlo con lo que hay que corregir.",
                    "", f"Revisar: {_enlace(ticket).rsplit('/', 1)[0]}/informe/borrador/"]),
        list(direccion),
    )


def informe_urgente(ticket, autor, motivo, destino):
    """A dirección: coordinación entregó el informe técnico sin su aprobación por urgencia; lo revisa después."""
    from accounts.models import Usuario

    direccion = Usuario.objects.filter(rol=Usuario.Rol.DIRECCION, is_active=True).values_list("email", flat=True)
    a_quien = "pasó a comercial para la propuesta" if destino == "comercial" else "se entregó al cliente"
    _enviar(
        f"[{ticket.radicado}] Informe entregado con urgencia, sin su aprobación: {ticket.asunto}",
        "\n".join([f"{autor.get_full_name() or autor.username} entregó el informe técnico sin su aprobación y {a_quien}.",
                    "", f"Motivo de la urgencia: {motivo}", "",
                    f"Empresa: {ticket.empresa}" + (f" · Sede: {ticket.sede}" if ticket.sede else ""), "",
                    "Revíselo cuando pueda; si hay que corregir algo, avise a coordinación.",
                    "", f"Ver el ticket: {_enlace(ticket)}"]),
        list(direccion),
    )


def informe_revisado(borrador, autor, aprobado, motivo=""):
    """A coordinación (y a quien generó el informe): dirección lo aprobó o lo devolvió con observaciones."""
    from accounts.models import Usuario

    ticket = borrador.ticket
    destinos = list(Usuario.objects.filter(rol=Usuario.Rol.SUPERVISOR, is_active=True).values_list("email", flat=True))
    if borrador.creado_por:
        destinos.append(borrador.creado_por.email)
    quien = autor.get_full_name() or autor.username
    if aprobado:
        asunto = f"[{ticket.radicado}] Informe técnico aprobado: {ticket.asunto}"
        texto = [f"{quien} aprobó el informe técnico. Ya se puede entregar al cliente o pasar a comercial para la propuesta."]
    else:
        asunto = f"[{ticket.radicado}] Informe técnico devuelto para corregir: {ticket.asunto}"
        texto = [f"{quien} devolvió el informe técnico para corregir:", "", motivo, "",
                 "Corríjalo y envíelo de nuevo a dirección."]
    _enviar(asunto, "\n".join([*texto, "", f"Ver el ticket: {_enlace(ticket)}"]), destinos)


def asignacion(ticket, autor=None, mensaje=""):
    """Al nuevo responsable (técnico, comercial, bodega…), con lo que le pidieron al asignárselo."""
    if ticket.asignado_a and ticket.asignado_a.email:
        quien = f"{autor.get_full_name() or autor.username} le asignó" if autor else "Se le asignó"
        _enviar(
            f"[{ticket.radicado}] Se le asignó un ticket: {ticket.asunto}",
            "\n".join([f"{quien} este ticket.", *(["", "Lo que le piden:", mensaje] if mensaje else []), "",
                        f"Empresa: {ticket.empresa}" + (f" · Sede: {ticket.sede}" if ticket.sede else ""),
                        f"Prioridad: {ticket.get_prioridad_display()} · Estado: {ticket.get_estado_display()}", "",
                        ticket.descripcion, "", f"Gestionar: {_enlace(ticket)}"]),
            [ticket.asignado_a.email],
        )


def mensaje_en_ticket(ticket, seguimiento):
    """Lo que alguien escribe en el ticket le llega a quien lo tiene; lo que escribe el responsable, al área técnica."""
    if seguimiento is None or not seguimiento.mensaje:
        return
    autor = seguimiento.autor
    destinatarios = []
    if ticket.asignado_a and ticket.asignado_a != autor and ticket.asignado_a.email:
        destinatarios.append(ticket.asignado_a.email)
    if autor and not autor.es_supervisor and settings.NOTIFICAR_SOPORTE_EMAIL:
        destinatarios.append(settings.NOTIFICAR_SOPORTE_EMAIL)
    tipo = "Nota interna" if seguimiento.interno else "Respuesta al cliente"
    _enviar(
        f"[{ticket.radicado}] {tipo} de {autor.get_full_name() or autor.username}: {ticket.asunto}",
        "\n".join([f"{autor.get_full_name() or autor.username} ({autor.rol_corto}) escribió en el ticket:", "",
                    seguimiento.mensaje, "", f"Estado: {ticket.get_estado_display()}",
                    f"Responsable: {ticket.asignado_a or 'sin asignar'}", "", f"Ver el ticket: {_enlace(ticket)}"]),
        destinatarios,
    )


def correos_area_comercial():
    """El buzón NOTIFICAR_COMERCIAL_EMAIL o, si no está configurado, las personas activas del área comercial."""
    if settings.NOTIFICAR_COMERCIAL_EMAIL:
        return [settings.NOTIFICAR_COMERCIAL_EMAIL]
    from django.db.models import Q

    from accounts.models import Usuario
    return list(Usuario.objects.filter(Q(rol=Usuario.Rol.COMERCIAL) | Q(tambien_comercial=True), is_active=True)
                .exclude(email="").values_list("email", flat=True))


def oportunidad_detectada(oportunidad):
    """Aviso al área comercial o, si no hay nadie en ella, al coordinador."""
    destino = correos_area_comercial() or [settings.NOTIFICAR_SOPORTE_EMAIL]
    acta = oportunidad.acta
    _enviar(
        f"[Oportunidad] {oportunidad.empresa} · {', '.join(oportunidad.servicios_display) or 'nueva necesidad'}",
        "\n".join([
            f"Cliente: {oportunidad.empresa}" + (f" · Sede: {oportunidad.sede}" if oportunidad.sede else ""),
            f"Detectada por: {oportunidad.detectada_por}" + (f" en el acta N° {acta.numero}" if acta else ""),
            f"Servicios: {', '.join(oportunidad.servicios_display) or '—'}",
            "", oportunidad.nota,
            "", f"Ver oportunidades: {settings.SITIO_URL.rstrip('/')}/oportunidades/",
        ]),
        destino,
    )


def acta_registrada(acta):
    """Copia del acta a los correos indicados en «Copia correo a» (como hacía SafetyTrack) y al cliente."""
    # Primero quien gestiona la solicitud (va en «Para»); las copias del acta van después, en copia
    destinatarios = correos_cliente(acta.ticket) if acta.ticket else []
    destinatarios += [c.strip() for c in acta.copia_correo.split(";") if c.strip()]
    fecha = timezone.localtime(acta.fecha)
    intervenidos = ", ".join(e.equipo + (f" ({e.get_trabajo_display().lower()})" if e.trabajo else "")
                             for e in acta.equipos_intervenidos if e.equipo)
    utilizados = ", ".join(e.equipo + (f" ({e.cantidad})" if e.cantidad else "") for e in acta.equipos_utilizados if e.equipo)
    retirados = ", ".join(e.equipo + (f" ({e.get_propiedad_display().split(':')[0].lower()})" if e.propiedad else "")
                          for e in acta.equipos_retirados if e.equipo)
    asunto = f"[Acta {acta.numero}]" + (f" [{acta.ticket.radicado}]" if acta.ticket else "") + f" Acta de servicio técnico · {acta.empresa}"
    enviar_al_cliente(
        asunto, destinatarios, f"Acta de servicio técnico N.º {acta.numero}",
        ["Le compartimos el acta del servicio técnico atendido de manera virtual, firmada por nuestro técnico."
         if acta.es_virtual else "Le compartimos el acta del servicio técnico realizado en sus instalaciones, firmada "
         "por nuestro técnico y por la persona que recibió el servicio."],
        saludo=_saludo(acta.ticket) if acta.ticket else "Cordial saludo.",
        radicado=acta.ticket.radicado if acta.ticket else "", estado=acta.get_resultado_display(),
        datos=[("Fecha", f"{fecha:%d/%m/%Y · %I:%M %p}".replace("AM", "a. m.").replace("PM", "p. m.")),
               ("Cliente / sede", f"{acta.empresa} · {acta.lugar}"),
               ("Servicio", f"{acta.actividad} · {acta.get_sistema_display_corto()}"),
               ("Modalidad", acta.get_modalidad_display()),
               ("Diagnóstico", acta.diagnostico), ("Trabajo realizado", acta.observaciones),
               ("Equipos intervenidos", intervenidos), ("Materiales utilizados", utilizados), ("Equipos retirados", retirados),
               ("Resultado", acta.get_resultado_display()),
               ("Técnico", acta.tecnico_texto), ("Recibió", f"{acta.recibio} ({acta.cargo_recibio})" if acta.cargo_recibio else acta.recibio)],
        boton=("Ver el acta completa con fotos y firmas", f"{settings.SITIO_URL.rstrip('/')}{acta.get_absolute_url()}"),
    )
    if not acta.ticket and settings.NOTIFICAR_SOPORTE_EMAIL and settings.NOTIFICAR_SOPORTE_EMAIL not in destinatarios:
        _enviar(
            f"[Acta {acta.numero}] Trabajo no programado · {acta.empresa}",
            "\n".join([f"{acta.tecnico_texto} registró un acta de un trabajo que no estaba en el cronograma.", "",
                        f"Cliente / sede: {acta.empresa} · {acta.lugar}", f"Diagnóstico: {acta.diagnostico}",
                        f"Trabajo realizado: {acta.observaciones}", f"Resultado: {acta.get_resultado_display()}", "",
                        f"Ver el acta: {settings.SITIO_URL.rstrip('/')}{acta.get_absolute_url()}"]),
            [settings.NOTIFICAR_SOPORTE_EMAIL],
        )
