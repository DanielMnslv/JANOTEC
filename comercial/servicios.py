"""Reglas del flujo comercial y los traspasos del mismo ticket entre áreas.

    Comercial ──solicitar_levantamiento──▶ Técnica ──(informe) devolver_a_comercial──▶ Comercial
    Comercial ──registrar_informe (sin visita)──▶ Administrativa arma la propuesta ──▶ dirección la aprueba
    Propuesta v1 ──enviar_version──▶ cliente: aprueba · no aprueba · solicitar_cambios ──▶ v2 …
    Propuesta aprobada ──a_inventario──▶ «En inventario» (bodega alista lo aprobado y los adicionales)
        ──coordinación decide──▶ «Recepcionado» en técnica (cronograma, visita, cierre)
"""
import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone

from tickets import notificaciones as notif
from tickets.models import Adjunto, Categoria, Seguimiento, Ticket, TipoFalla

from .models import FichaComercial, InformeRequerimiento, PropuestaVersion
from .pdf import ficha_pdf

log = logging.getLogger(__name__)
F = FichaComercial
T = Ticket


def _seguimiento(ticket, autor, mensaje, interno=False, tipo=Seguimiento.Tipo.COMENTARIO, **extra):
    """Crea una entrada en el historial del ticket (por defecto un comentario visible para el cliente)."""
    return Seguimiento.objects.create(ticket=ticket, autor=autor, tipo=tipo, mensaje=mensaje, interno=interno, **extra)


def guardar_adjuntos(ticket, archivos, autor, seguimiento, es_propuesta=False, es_informe=False):
    """Guarda los archivos subidos como adjuntos del ticket, colgados del seguimiento que los trajo.

        es_propuesta / es_informe marcan el tipo de documento para mostrarlo en su sección.
    """
    for a in archivos:
        Adjunto.objects.create(ticket=ticket, seguimiento=seguimiento, archivo=a, nombre_original=a.name[:255],
                               tamano=a.size, subido_por=autor, es_propuesta=es_propuesta, es_informe=es_informe)


def registrar_informe(ficha, autor, datos, archivos=(), con_visita=False):
    """Informe del requerimiento (obligatorio para la propuesta). Crea o actualiza; queda en el historial."""
    t = ficha.ticket
    with transaction.atomic():
        informe, nuevo = InformeRequerimiento.objects.get_or_create(
            ficha=ficha, defaults={"necesidad": datos["necesidad"], "elaborado_por": autor})
        for campo in ("necesidad", "solucion", "servicios", "materiales", "observaciones"):
            setattr(informe, campo, (datos.get(campo) or "").strip())
        informe.con_visita = con_visita or informe.con_visita
        informe.elaborado_por = autor
        informe.save()
        seg = _seguimiento(t, autor, ("Informe del requerimiento registrado" if nuevo else "Informe del requerimiento actualizado")
                           + (" (con visita de levantamiento)." if informe.con_visita else " (sin visita)."), interno=True)
        guardar_adjuntos(t, archivos, autor, seg, es_informe=True)
        if ficha.etapa == F.Etapa.GESTION:
            ficha.etapa = F.Etapa.ELABORANDO  # con el informe, administrativa ya puede armar la propuesta
            ficha.save(update_fields=["etapa", "actualizado"])
            from tickets.devoluciones import T as tramo, cerrar_por_reenvio
            cerrar_por_reenvio(t, tramo.A_REQUERIMIENTO, autor, "Comercial completó el requerimiento y su informe.")
    return informe


def categoria_instalacion():
    """Categoría con la que se radican los requerimientos comerciales (Instalación…, o la primera que exista)."""
    categoria = Categoria.objects.filter(nombre__startswith="Instalación").first() or Categoria.objects.first()
    return categoria


def crear_ticket_comercial(*, empresa, sede, solicitante, autor, asunto, descripcion, impacto, contacto_nombre,
                           contacto_telefono, contacto_email, no_disponible, restricciones, ficha_datos, origen,
                           fotos=(), anexos=(), oportunidad=None, jornada=""):
    """Un solo ticket para toda la vida de la solicitud: nace en el área comercial.

    Con urgencia alta el ticket nace con prioridad alta. La jornada es la que pidió el cliente para la primera visita.
    """
    categoria = categoria_instalacion()
    falla = TipoFalla.objects.filter(categoria=categoria, nombre__istartswith="Cotiz").first() if categoria else None
    with transaction.atomic():
        ticket = Ticket(
            empresa=empresa, sede=sede, solicitante=solicitante, tipo=T.Tipo.COTIZACION, area=T.Area.COMERCIAL,
            categoria=categoria, falla=falla, asunto=asunto[:200], descripcion=descripcion or asunto, impacto=impacto or "",
            contacto_nombre=contacto_nombre, contacto_telefono=contacto_telefono, contacto_email=contacto_email or "",
            no_disponible=no_disponible or {}, restricciones=restricciones or "", jornada=jornada or "",
            prioridad=T.Prioridad.ALTA if impacto == T.Impacto.ALTA else T.Prioridad.MEDIA,
        )
        ticket.save()
        ficha = F.objects.create(ticket=ticket, origen=origen, oportunidad=oportunidad, **ficha_datos)
        seg = _seguimiento(ticket, autor, f"Solicitud registrada ({ficha.get_origen_display().lower()}). "
                                          "El área comercial está a cargo.", tipo=Seguimiento.Tipo.CREACION,
                           estado_nuevo=ticket.estado)
        guardar_adjuntos(ticket, list(fotos) + list(anexos), autor, seg)
    requerimiento_creado(ficha)
    return ficha


def solicitar_levantamiento(ficha, autor, nota, jornada=""):
    """Comercial se apoya en el área técnica para entender lo que necesita el cliente: el ticket entra a técnica
    ya recepcionado y el coordinador asigna el técnico y lo programa en el cronograma."""
    t = ficha.ticket
    with transaction.atomic():
        t.area = T.Area.TECNICA
        t.asignado_a = None
        t.visita_programada = None
        t.jornada = jornada or t.jornada
        t.save(update_fields=["area", "asignado_a", "visita_programada", "jornada", "actualizado"])
        t.reingresar(autor, "Llega del área comercial para la visita de levantamiento: queda por programar.")
        ficha.etapa = F.Etapa.LEVANTAMIENTO
        ficha.save(update_fields=["etapa", "actualizado"])
        seg = _seguimiento(t, autor, "Programaremos una visita técnica para revisar el lugar y definir lo que necesita."
                                     + (f"\n{nota}" if nota else ""))
        _seguimiento(t, autor, f"Asignado al área técnica para levantamiento ({t.get_jornada_display() or 'sin jornada'}). "
                               f"Qué revisar: {nota}", interno=True)
        from tickets.devoluciones import T as tramo, cerrar_por_reenvio
        cerrar_por_reenvio(t, tramo.A_COMERCIAL, autor, f"Comercial completó los datos y volvió a pedir el levantamiento. {nota}".strip())
    # Al cliente no le llega correo por los traspasos entre áreas: lo ve al consultar su solicitud
    notif._enviar(
        f"[{t.radicado}] Levantamiento técnico solicitado por comercial · {t.empresa}",
        "\n".join([f"El área comercial solicita una visita de levantamiento: «{t.asunto}».", f"Qué revisar: {nota}",
                   f"Primera visita que pidió el cliente: {ficha.visita_display}",
                   f"Contacto: {t.contacto_nombre} · {t.contacto_telefono}",
                   f"NO puede atender: {t.resumen_no_disponible}" if t.resumen_no_disponible else "",
                   "", f"Asignar técnico: {settings.SITIO_URL.rstrip('/')}{t.get_absolute_url()}"]),
        [settings.NOTIFICAR_SOPORTE_EMAIL],
    )


def devolver_a_comercial(ficha, autor, resultado, servicios="", materiales=None):
    """El área técnica terminó el levantamiento: el ticket vuelve a comercial con el resultado.
    Si el informe técnico separa servicios y materiales, el informe del requerimiento nace con esas listas."""
    t = ficha.ticket
    with transaction.atomic():
        tecnico = t.asignado_a
        t.area = T.Area.COMERCIAL
        t.asignado_a = None
        t.estado = T.Estado.EN_PROCESO
        t.save(update_fields=["area", "asignado_a", "estado", "actualizado"])
        ficha.levantamiento = (ficha.levantamiento + "\n\n" if ficha.levantamiento else "") + \
            f"[{timezone.localtime():%d/%m/%Y} · {tecnico or autor}] {resultado}"
        ficha.etapa = F.Etapa.ELABORANDO
        ficha.save(update_fields=["levantamiento", "etapa", "actualizado"])
        if not ficha.informe_listo:
            hubo_visita = t.actas.exists() or t.actividades.exists()
            InformeRequerimiento.objects.create(
                ficha=ficha, con_visita=hubo_visita, necesidad=t.asunto, solucion=resultado, servicios=servicios.strip(),
                materiales=resultado if materiales is None else materiales.strip(),
                elaborado_por=(tecnico if hubo_visita else None) or autor)
        _seguimiento(t, autor, f"Resultado del levantamiento técnico:\n{resultado}", interno=True)
        from tickets.devoluciones import T as tramo, cerrar_por_reenvio
        cerrar_por_reenvio(t, tramo.A_TECNICA, autor, "El área técnica completó el informe y lo volvió a enviar.")
        seg = _seguimiento(t, autor, "Terminamos la visita técnica. Estamos preparando su propuesta económica.")
    # Al cliente no le llega correo por los traspasos entre áreas: lo ve al consultar su solicitud
    notif._enviar(f"[{t.radicado}] Levantamiento listo · {t.empresa}",
                  "\n".join([f"{tecnico or autor} terminó el levantamiento de «{t.asunto}».", "", resultado, "",
                             f"Elaborar la propuesta: {settings.SITIO_URL.rstrip('/')}{ficha.get_absolute_url()}"]),
                  _correos_propuesta(ficha))


def pasar_a_propuesta(ticket, autor, informe, servicios="", materiales=None):
    """Toma de requerimientos o diagnóstico con su informe técnico: el mismo ticket pasa a comercial para la propuesta."""
    ficha = getattr(ticket, "ficha", None)
    if ficha is None:
        origen = F.Origen.CLIENTE if ticket.solicitante.es_cliente else F.Origen.COMERCIAL
        ficha = F.objects.create(ticket=ticket, origen=origen, etapa=F.Etapa.LEVANTAMIENTO,
                                 municipio=ticket.municipio, direccion=ticket.direccion)
    devolver_a_comercial(ficha, autor, informe, servicios, materiales)
    return ficha


def _a_instalacion(ficha):
    """El mismo ticket queda en el área técnica como instalación (sin responsable todavía)."""
    t = ficha.ticket
    categoria = categoria_instalacion()
    t.area = T.Area.TECNICA
    t.asignado_a = None
    t.categoria = categoria
    t.falla = TipoFalla.objects.filter(categoria=categoria, nombre__startswith="Instalar").first() or t.falla
    t.save(update_fields=["area", "asignado_a", "categoria", "falla", "actualizado"])
    ficha.etapa = F.Etapa.INSTALACION
    ficha.save(update_fields=["etapa", "actualizado"])
    if t.empresa.es_prospecto:
        t.empresa.es_prospecto = False  # ganó una propuesta: ya es cliente
        t.empresa.save(update_fields=["es_prospecto"])


def a_inventario(ficha, autor):
    """Propuesta aprobada: lo siguiente es alistar los materiales y equipos. El ticket queda «En inventario» y bodega
    recibe la lista con lo aprobado (de la propuesta, que sale del informe y este del acta). Coordinación ve el avance
    y decide cuándo pasa a «Recepcionado». Si la propuesta solo tiene servicios, no hay nada que alistar."""
    from inventario.solicitudes import desde_propuesta

    t = ficha.ticket
    with transaction.atomic():
        _a_instalacion(ficha)
        t.cambiar_estado(T.Estado.EN_INVENTARIO, autor, "Propuesta aprobada: bodega alista los materiales y equipos "
                                                        "aprobados por el cliente y los adicionales para la instalación.",
                         interno=True)
        desde_propuesta(ficha, autor)
        alistar = t.solicitudes_material.filter(estado__in=["borrador", "validacion", "propuesta", "compra", "aprobacion",
                                                            "aprobada"]).exists()
        if not alistar:
            t.reingresar(autor, "La propuesta aprobada solo tiene servicios: no hay material que alistar; "
                                "la instalación queda por programar.")
    destino = [settings.NOTIFICAR_SOPORTE_EMAIL]
    if alistar:
        notif._enviar(f"[{t.radicado}] Propuesta aprobada · {t.empresa} — en inventario",
                      "\n".join([f"El cliente aprobó la propuesta de «{t.asunto}».",
                                  "Bodega está alistando los materiales y equipos. Vea el avance de la lista en el ticket y, "
                                  "cuando lo considere, páselo a «Recepcionado» para programar la instalación.", "",
                                  f"{settings.SITIO_URL.rstrip('/')}{t.get_absolute_url()}#alistamiento"]), destino)
    else:
        notif._enviar(f"[{t.radicado}] Instalación aprobada · {t.empresa} — por programar",
                      f"Propuesta aprobada solo con servicios.\n\n{settings.SITIO_URL.rstrip('/')}{t.get_absolute_url()}", destino)


def traspasar_a_instalacion(ficha, autor, nota=""):
    """Acuerdo cerrado con el cliente (aprobado antes del alistamiento en inventario): pasa al área técnica para instalar."""
    t = ficha.ticket
    with transaction.atomic():
        _a_instalacion(ficha)
        t.reingresar(autor, "Llega del área comercial con la propuesta aprobada: la instalación queda por programar.")
        resumen = ["Acuerdo comercial cerrado. Pasa al área técnica para la instalación."]
        if ficha.servicios:
            resumen.append("Servicios: " + ", ".join(ficha.servicios_display))
        if ficha.levantamiento:
            resumen += ["", "Levantamiento técnico:", ficha.levantamiento]
        if nota:
            resumen += ["", nota]
        _seguimiento(t, autor, "\n".join(resumen), interno=True)
        seg = _seguimiento(t, autor, "Su propuesta quedó aprobada. Nuestro equipo técnico programará la instalación.")
    # Al cliente no le llega correo por los traspasos entre áreas: lo ve al consultar su solicitud
    notif._enviar(f"[{t.radicado}] Instalación aprobada · {t.empresa} — por asignar",
                  "\n".join(resumen + ["", f"Asignar técnico: {settings.SITIO_URL.rstrip('/')}{t.get_absolute_url()}"]),
                  [settings.NOTIFICAR_SOPORTE_EMAIL])


def enviar_version(ficha, autor, pdf_adjunto):
    """Nueva versión de la propuesta: la anterior queda en el historial (con los cambios que pidió el cliente)."""
    numero = (ficha.versiones.order_by("-numero").values_list("numero", flat=True).first() or 0) + 1
    return PropuestaVersion.objects.create(ficha=ficha, numero=numero, valor=ficha.valor_propuesta, archivo=pdf_adjunto,
                                           enviada_por=autor, enviada=timezone.now())


def solicitar_cambios(ficha, autor, cambios):
    """El cliente acepta en principio pero pide cambios (otra marca, otro alcance): se prepara una nueva versión."""
    t = ficha.ticket
    cambios = cambios.strip()[:2000]
    with transaction.atomic():
        version = ficha.version_actual
        if version:
            version.estado, version.cambios, version.respondida = PropuestaVersion.Estado.CAMBIOS, cambios, timezone.now()
            version.save(update_fields=["estado", "cambios", "respondida"])
        ficha.etapa = F.Etapa.AJUSTES
        ficha.fecha_respuesta = timezone.now()
        ficha.save(update_fields=["etapa", "fecha_respuesta", "actualizado"])
        if t.estado == T.Estado.PROPUESTA_ENVIADA:
            t.estado = T.Estado.EN_PROCESO
            t.save(update_fields=["estado", "actualizado"])
        n = f" v{version.numero}" if version else ""
        _seguimiento(t, autor, f"Cambios solicitados a la propuesta{n}:\n{cambios}")
    notif._enviar(f"[{t.radicado}] El cliente pidió cambios a la propuesta{n} · {t.empresa}",
                  f"{autor or 'El cliente (desde el correo)'} pidió estos cambios:\n\n{cambios}\n\nPrepare la nueva versión: "
                  f"{settings.SITIO_URL.rstrip('/')}{ficha.get_absolute_url()}",
                  _correos_propuesta(ficha))


class PlazoRequerido(ValueError):
    """La propuesta es de alquiler y se aprobó sin elegir uno de sus plazos."""


def plazo_elegido(version, plazo):
    """Valida el plazo que eligió el cliente: (meses, canon) si la versión es de alquiler, (None, None) si es venta.

    Lanza PlazoRequerido si es alquiler y el plazo no es uno de los ofrecidos."""
    opciones = version.opciones_alquiler() if version else []
    if not opciones:
        return None, None
    try:
        meses = int(plazo)
    except (TypeError, ValueError):
        meses = None
    fila = next((f for f in opciones if f["plazo"] == meses), None)
    if fila is None:
        raise PlazoRequerido("La propuesta es de alquiler: elija el plazo (" +
                             ", ".join(f"{f['plazo']}" for f in opciones) + " meses).")
    return fila["plazo"], fila["canon"]


def registrar_respuesta(ficha, autor, aprobada, motivo="", plazo=None):
    """El cliente (o el asesor en su nombre) aprueba o no la propuesta.

    Si es de alquiler o híbrido, al aprobar se exige el plazo que eligió (12, 24, 36…) y queda con su canon mensual;
    lanza PlazoRequerido si falta o no es uno de los ofrecidos."""
    t = ficha.ticket
    version_previa = ficha.version_actual
    meses = canon = None
    if aprobada and version_previa and version_previa.estado == PropuestaVersion.Estado.ENVIADA:
        meses, canon = plazo_elegido(version_previa, plazo)
    with transaction.atomic():
        ficha.etapa = F.Etapa.APROBADA if aprobada else F.Etapa.RECHAZADA
        ficha.fecha_respuesta = timezone.now()
        if not aprobada:
            ficha.motivo_rechazo = motivo[:300] or "Sin motivo indicado."
        ficha.save()
        version = ficha.version_actual
        if version and version.estado == PropuestaVersion.Estado.ENVIADA:
            version.estado = PropuestaVersion.Estado.APROBADA if aprobada else PropuestaVersion.Estado.RECHAZADA
            version.motivo = "" if aprobada else ficha.motivo_rechazo
            version.respondida = ficha.fecha_respuesta
            version.plazo_meses, version.canon_mensual = meses, canon
            version.save(update_fields=["estado", "motivo", "respondida", "plazo_meses", "canon_mensual"])
            if aprobada and version.valor:
                ficha.valor_propuesta = version.valor
                ficha.save(update_fields=["valor_propuesta"])
        if aprobada:
            eleccion = f" en alquiler a {version.plazo_texto}" if version and version.plazo_meses else ""
            seg = _seguimiento(t, autor, f"Propuesta aprobada{eleccion}. Estamos alistando los materiales y equipos para la instalación.")
        else:
            seg = _seguimiento(t, autor, f"La propuesta no fue aprobada: {ficha.motivo_rechazo}")
            t.cambiar_estado(T.Estado.CERRADO, autor, "Solicitud comercial cerrada sin aprobación.")
    # Al cliente no le llega correo por la respuesta registrada: lo ve al consultar su solicitud
    if aprobada:  # lo siguiente es alistar en inventario lo aprobado
        a_inventario(ficha, autor)
    else:
        for s in t.solicitudes_material.filter(estado="propuesta"):
            from inventario.solicitudes import cancelar
            cancelar(s, autor, "El cliente no aprobó la propuesta.")
    notif._enviar(f"[{t.radicado}] Propuesta {'aprobada' if aprobada else 'no aprobada'} · {t.empresa}",
                  f"{seg.mensaje}\n\n{settings.SITIO_URL.rstrip('/')}{ficha.get_absolute_url()}",
                  _correos_propuesta(ficha))


# --- Avisos ------------------------------------------------------------------------
def _acciones_propuesta(version):
    """Botones del correo de la propuesta: aprobar, aprobar con cambios o no aprobar.

        Cada enlace lleva una firma propia de la versión, así el cliente responde sin iniciar sesión.
    """
    if version is None:
        return []
    from .respuesta import enlace
    opciones = version.opciones_alquiler()
    if opciones:  # alquiler: un botón por plazo, así el clic ya dice cuántos meses eligió
        aprobar = [(f"Apruebo a {f['plazo']} meses (${f['canon']:,.0f}/mes)".replace(",", "."),
                    f"{enlace(version, 'aprobar')}&plazo={f['plazo']}", "aprobar") for f in opciones]
        return aprobar + [("La apruebo con cambios", enlace(version, "cambios"), "cambios"),
                          ("No la apruebo", enlace(version, "rechazar"), "rechazar")]
    return [("Apruebo la propuesta", enlace(version, "aprobar"), "aprobar"),
            ("La apruebo con cambios", enlace(version, "cambios"), "cambios"),
            ("No la apruebo", enlace(version, "rechazar"), "rechazar")]


def _correos_comerciales(ficha):
    """Área comercial: el asesor de la ficha y el buzón comercial; sin asesor ni buzón, todo el área comercial.

    Mientras el área comercial no tenga a nadie, le llega a dirección, que cubre su trabajo (o, sin ella, al coordinador).
    """
    asesor = [ficha.comercial.email] if ficha.comercial and ficha.comercial.email else []
    area = [settings.NOTIFICAR_COMERCIAL_EMAIL] if settings.NOTIFICAR_COMERCIAL_EMAIL else ([] if asesor else notif.correos_area_comercial())
    return list(dict.fromkeys(asesor + area)) or _correos_direccion() or [settings.NOTIFICAR_SOPORTE_EMAIL]


def _correos_administrativos():
    """Área administrativa (arma las propuestas): por rol o con «también área administrativa». Sin nadie, dirección."""
    from django.db.models import Q

    from accounts.models import Usuario
    correos = list(Usuario.objects.filter(Q(rol=Usuario.Rol.ADMINISTRATIVO) | Q(tambien_administrativo=True), is_active=True,
                                          is_superuser=False).exclude(email="").values_list("email", flat=True))
    return correos or _correos_direccion() or [settings.NOTIFICAR_SOPORTE_EMAIL]


def _correos_propuesta(ficha):
    """Lo que toca a la propuesta y al cliente a la vez: administrativa (la arma) y comercial (le hace seguimiento)."""
    return list(dict.fromkeys(_correos_administrativos() + _correos_comerciales(ficha)))


# --- Revisión de la propuesta por dirección ------------------------------------------------------
def _correos_direccion():
    """Correos de los usuarios de dirección activos (Miguel)."""
    from accounts.models import Usuario
    return list(Usuario.objects.filter(rol=Usuario.Rol.DIRECCION, is_active=True).exclude(email="")
                .values_list("email", flat=True))


def _enlace_cotizador(ficha):
    """Enlace absoluto al cotizador del requerimiento, para los avisos al área administrativa."""
    from django.urls import reverse
    return f"{settings.SITIO_URL.rstrip('/')}{reverse('comercial:cotizador', args=[ficha.ticket.radicado])}"


def propuesta_para_aprobar(cotizacion, autor):
    """A dirección: comercial envió la propuesta para que la revise y la apruebe."""
    ficha, t = cotizacion.ficha, cotizacion.ficha.ticket
    notif._enviar(
        f"[{t.radicado}] Propuesta para aprobar: {t.asunto}",
        "\n".join([f"{autor.get_full_name() or autor.username} envió la propuesta económica para su revisión y aprobación.",
                   "", f"Empresa: {t.empresa}" + (f" · Sede: {t.sede}" if t.sede else ""), "",
                   "Puede aprobarla (sale al cliente) o devolverla al área administrativa con lo que hay que corregir.",
                   "", f"Revisar: {_enlace_cotizador(ficha)}"]),
        _correos_direccion(),
    )


def propuesta_devuelta(cotizacion, autor, observaciones):
    """Al área administrativa: dirección devolvió la propuesta con observaciones."""
    ficha, t = cotizacion.ficha, cotizacion.ficha.ticket
    notif._enviar(
        f"[{t.radicado}] Propuesta devuelta para corregir: {t.asunto}",
        "\n".join([f"{autor.get_full_name() or autor.username} devolvió la propuesta para corregir:", "", observaciones,
                   "", f"Corregir: {_enlace_cotizador(ficha)}"]),
        _correos_administrativos(),
    )


def propuesta_urgente(ficha, autor, motivo, codigo):
    """A dirección: comercial envió la propuesta al cliente sin su aprobación por urgencia; la revisa después."""
    t = ficha.ticket
    notif._enviar(
        f"[{t.radicado}] Propuesta enviada con urgencia, sin su aprobación: {t.asunto}",
        "\n".join([f"{autor.get_full_name() or autor.username} envió la propuesta {codigo} al cliente sin su aprobación.",
                   "", f"Motivo de la urgencia: {motivo}", "",
                   f"Empresa: {t.empresa}" + (f" · Sede: {t.sede}" if t.sede else ""), "",
                   "Revísela cuando pueda; si hay que corregir algo, avise al área administrativa.",
                   "", f"Ver la solicitud: {settings.SITIO_URL.rstrip('/')}{ficha.get_absolute_url()}"]),
        _correos_direccion(),
    )


def propuesta_para_entregar(ficha, pdf_adjunto, version, autor):
    """Al asesor comercial: dirección aprobó la propuesta para que él se la entregue al cliente (con el PDF adjunto)."""
    t = ficha.ticket
    cuerpo = "\n".join([
        f"{autor.get_full_name() or autor.username} aprobó la propuesta {version.codigo or f'v{version.numero}'} para que "
        "usted se la entregue al cliente. Al cliente no le llegó por correo.", "",
        f"Empresa: {t.empresa}" + (f" · Sede: {t.sede}" if t.sede else ""), f"Requerimiento: {t.asunto}",
        f"Valor: {version.valor_texto}" if version.valor_texto else "", "",
        "Cuando la entregue, márquela como «Entregada al cliente» con la fecha; luego registre lo que responda.", "",
        f"Ver el requerimiento: {settings.SITIO_URL.rstrip('/')}{ficha.get_absolute_url()}"])
    asunto = f"[{t.radicado}] Propuesta para entregar al cliente · {t.empresa}"
    correo = EmailMultiAlternatives(asunto, cuerpo, settings.DEFAULT_FROM_EMAIL, _correos_comerciales(ficha))
    correo.attach_alternative(notif.html_desde_texto(asunto, cuerpo), "text/html")
    if pdf_adjunto:
        try:
            with pdf_adjunto.archivo.open("rb") as f:
                correo.attach(pdf_adjunto.nombre_original, f.read(), "application/pdf")
        except Exception:  # noqa: BLE001
            log.exception("No se pudo adjuntar la propuesta de %s", t.radicado)
    try:
        correo.send(fail_silently=False)
    except Exception:  # noqa: BLE001 — el correo nunca debe tumbar la operación
        log.exception("No se pudo enviar «%s»", asunto)


def _enviar_con_ficha(asunto, cuerpo, destinatarios, ficha, interna):
    """Correo con el PDF «Ficha de requerimiento» adjunto (si el PDF falla, sale el correo sin él)."""
    destinatarios = [d for d in set(destinatarios) if d]
    if not destinatarios:
        return
    correo = EmailMultiAlternatives(asunto, cuerpo, settings.DEFAULT_FROM_EMAIL, destinatarios)
    correo.attach_alternative(notif.html_desde_texto(asunto, cuerpo), "text/html")
    try:
        correo.attach(*ficha_pdf(ficha, interna=interna), "application/pdf")
    except Exception:  # noqa: BLE001
        log.exception("No se pudo generar la ficha PDF de %s", ficha.ticket.radicado)
    try:
        correo.send(fail_silently=False)
    except Exception:  # noqa: BLE001 — el correo nunca debe tumbar la operación
        log.exception("No se pudo enviar «%s»", asunto)


def requerimiento_creado(ficha):
    """Aviso al radicar un requerimiento: al cliente con la ficha en PDF y al área comercial."""
    t = ficha.ticket
    adjuntos = []
    try:
        adjuntos.append((*ficha_pdf(ficha, interna=False), "application/pdf"))
    except Exception:  # noqa: BLE001
        log.exception("No se pudo generar la ficha PDF de %s", t.radicado)
    notif.enviar_al_cliente(
        f"[{t.radicado}] Recibimos su solicitud de propuesta", t.correos_respuesta, "Recibimos su requerimiento",
        [f"Registramos su solicitud con el número de radicado {t.radicado}. Adjuntamos la ficha de su "
         "requerimiento como constancia de recepción.",
         "Con este mismo número podrá seguirla de principio a fin: propuesta, aprobación e instalación."],
        saludo=notif._saludo(t), radicado=t.radicado, estado="En gestión comercial",
        datos=[("Requerimiento", t.asunto), ("Cliente", str(t.empresa)), ("Asesor", str(ficha.comercial or ""))],
        boton=("Consultar mi solicitud", notif._consulta(t)), firma="Área Comercial", adjuntos=adjuntos,
    )
    _enviar_con_ficha(
        f"[{t.radicado}] {'PRIORITARIO · ' if ficha.prioritaria else ''}Nuevo requerimiento comercial · {t.empresa}",
        "\n".join([t.asunto, "", t.descripcion, f"Origen: {ficha.get_origen_display()} · Asesor: {ficha.comercial or '—'}",
                   f"Contacto: {t.contacto_nombre} · {t.contacto_telefono} · {t.contacto_email}",
                   f"Primera visita que pidió el cliente: {ficha.visita_display}",
                   *_texto_prioridad(ficha),
                   "", f"{settings.SITIO_URL.rstrip('/')}{ficha.get_absolute_url()}"]),
        # Si es prioritario, administrativa también se entera desde ya para alistar la propuesta
        _correos_propuesta(ficha) if ficha.prioritaria else _correos_comerciales(ficha), ficha, interna=True,
    )


def _texto_prioridad(ficha):
    """Líneas del aviso que explican por qué el requerimiento es prioritario y para cuándo (vacío si es normal)."""
    if not ficha.prioritaria:
        return []
    plazo = (f" Propuesta antes del {timezone.localtime(ficha.fecha_max_propuesta):%d/%m/%Y %I:%M %p} "
             f"({ficha.tiempo_restante})." if ficha.fecha_max_propuesta else "")
    return ["", f"PRIORITARIO ({', '.join(ficha.motivos_prioridad)}): comercial y administrativa deben agilizar el "
                f"informe y la propuesta.{plazo}"]


def propuesta_enviada(ficha, pdf_adjunto):
    """Al cliente, con el PDF de la propuesta adjunto (copia al área comercial)."""
    t = ficha.ticket
    destinatarios = list({*t.correos_respuesta, *([t.solicitante.email] if t.solicitante.es_cliente and t.solicitante.email else [])})
    if not destinatarios:
        return
    adjuntos = []
    if pdf_adjunto:
        try:
            with pdf_adjunto.archivo.open("rb") as f:
                adjuntos.append((pdf_adjunto.nombre_original, f.read(), "application/pdf"))
        except Exception:  # noqa: BLE001
            log.exception("No se pudo adjuntar la propuesta de %s", t.radicado)
    version = ficha.version_actual
    # Al cliente se le muestra el total con IVA incluido (si la propuesta salió del cotizador)
    valor = version.valor_texto if version else (
        f"${ficha.valor_propuesta:,.0f} antes de IVA".replace(",", ".") if ficha.valor_propuesta else "")
    anterior = ficha.versiones.filter(numero__lt=version.numero).order_by("-numero").first() if version else None
    parrafos = [f"Adjuntamos la propuesta económica de su requerimiento «{t.asunto}»."]
    if anterior and anterior.cambios:
        parrafos.append(f"Esta versión incluye los cambios que nos solicitó: {anterior.cambios}")
    parrafos.append("Revise el documento adjunto y díganos con un clic qué decide: si la aprueba, si la aprueba con "
                    "cambios (otra marca, otro alcance) o si no la aprueba.")
    titulo = "Le enviamos su propuesta económica" + (f" (versión {version.numero})" if version and version.numero > 1 else "")
    notif.enviar_al_cliente(
        f"[{t.radicado}] Propuesta económica JANOTEC" + (f" · versión {version.numero}" if version and version.numero > 1 else ""),
        destinatarios + [c for c in _correos_propuesta(ficha) if c != settings.NOTIFICAR_SOPORTE_EMAIL],
        titulo, parrafos,
        saludo=notif._saludo(t), radicado=t.radicado, estado="Propuesta enviada",
        datos=[("Requerimiento", t.asunto), ("Cliente", str(t.empresa)), ("Propuesta", (version.codigo or f"Versión {version.numero}") if version else ""),
               ("Valor", valor), ("Asesor", str(ficha.comercial or ""))],
        firma="Área Comercial", adjuntos=adjuntos,
        # Lo que se necesita del cliente: que responda. Cada botón lleva su respuesta (sin usuario ni contraseña).
        acciones=_acciones_propuesta(version),
    )
