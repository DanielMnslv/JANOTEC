"""Solicitudes de material a bodega: el camino que debe recorrer el material antes de salir hacia un cliente.

Comercial (propuesta aprobada)          Técnica (visita del cronograma)
        │ el asesor lista el material            │ el coordinador planea los insumos de la visita
        └──────────────► Validación de bodega ◄───┘
                           │ ¿a cargo de quién? · ¿hay existencias? · holgura de consumibles
                           ├─ a cargo del cliente y sin propuesta aprobada → área comercial (propuesta)
                           ├─ faltan existencias → bodega cotiza y compra (se libera sola al ingresar la compra)
                           └─ firmas: bodega + autorización (coordinador TIC o dirección, el que esté)
                                       → Aprobada → el técnico lo recoge en bodega → lo que sobre se devuelve
"""
import re
from collections import Counter, defaultdict
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from accounts.models import Usuario
from tickets import notificaciones as notif
from tickets.models import Seguimiento

from . import servicios
from .models import ActaEntrega, Activo, FirmaSolicitud, Insumo, ItemSolicitud, SolicitudMaterial

S = SolicitudMaterial.Estado
R = FirmaSolicitud.Rol
CERO = Decimal("0")
# La autorización de salida la da dirección o, cuando el jefe no está, el coordinador TIC: cualquiera de los dos basta
AUTORIZAN = (R.DIRECCION, R.COORDINADOR)


def _n(valor):
    """Decimal('40.00') → «40», Decimal('2.50') → «2.5»"""
    return format(Decimal(valor).normalize(), "f")


# --- Materiales escritos como en el cronograma («3 Batería … [BAT-12V7A]») -------------------
def leer_materiales(texto):
    """[(insumo o None, referencia, descripción, cantidad)] desde el texto con el formato del cronograma."""
    from cronograma.semanal import parse_insumos

    filas = []
    for item in parse_insumos(texto):
        m = re.search(r"\[([^\]]+)\]\s*$", item["descripcion"])
        codigo = m.group(1).strip().upper() if m else ""
        descripcion = re.sub(r"\s*\[[^\]]+\]\s*$", "", item["descripcion"]).strip()
        cantidad = Decimal(str(item["cantidad"]))
        insumo = Insumo.objects.filter(codigo=codigo).first() if codigo else None
        referencia = codigo if codigo and not insumo and Activo.objects.filter(referencia__iexact=codigo).exists() else ""
        filas.append((insumo, referencia, descripcion or codigo, cantidad))
    return filas


def texto_materiales(solicitud, seccion=ItemSolicitud.Seccion.APROBADO):
    """Al revés: los ítems como texto para editarlos con el selector del inventario."""
    lineas = []
    for i in solicitud.items.filter(seccion=seccion).select_related("insumo"):
        codigo = i.insumo.codigo if i.insumo else i.referencia
        metro = "m " if i.insumo and i.insumo.unidad == Insumo.Unidad.METRO else ""
        lineas.append(f"{i.cantidad.normalize():f} {metro}{i.descripcion}" + (f" [{codigo}]" if codigo else ""))
    return "\n".join(lineas)


def guardar_items(solicitud, texto, seccion=ItemSolicitud.Seccion.APROBADO):
    """Reemplaza los ítems de una sección de la lista (lo aprobado no toca los adicionales, ni al revés)."""
    solicitud.items.filter(seccion=seccion).delete()
    for insumo, referencia, descripcion, cantidad in leer_materiales(texto):
        ItemSolicitud.objects.create(solicitud=solicitud, insumo=insumo, referencia=referencia, seccion=seccion,
                                     descripcion=descripcion[:300], cantidad=cantidad)


# --- Existencias y lo apartado -------------------------------------------------------------
# Lo que bodega ya validó queda apartado para su ticket hasta que sale: otra solicitud no lo puede contar como suyo
ESTADOS_APARTAN = [S.COMPRA, S.APROBACION, S.APROBADA]


def _clave(item):
    return item.insumo.codigo if item.insumo else (item.referencia or "").upper()


def apartados(para=None):
    """{código o referencia: [(solicitud, cantidad apartada)]}: lo validado por bodega que todavía no ha salido.

    Con «para», solo lo que le gana a esa solicitud: lo ya validado tiene prioridad sobre lo que aún no se valida,
    y entre lo validado manda la solicitud más antigua."""
    resultado = defaultdict(list)
    qs = SolicitudMaterial.objects.filter(estado__in=ESTADOS_APARTAN)
    if para is not None:
        qs = qs.exclude(pk=para.pk)
        if para.estado in ESTADOS_APARTAN:
            qs = qs.filter(pk__lt=para.pk)
    # Cada solicitud aparta, en su turno, solo lo que de verdad queda en bodega (lo que espera compra no cuenta)
    saldos = servicios.saldos_insumos()
    restante = Counter({codigo: max(saldos[pk]["bodega"], CERO) if pk in saldos else CERO
                        for pk, codigo in Insumo.objects.values_list("pk", "codigo")})
    restante.update(Counter(r.upper() for r in Activo.objects.filter(estado=Activo.Estado.DISPONIBLE)
                            .exclude(referencia="").values_list("referencia", flat=True)))
    for s in qs.order_by("pk").select_related("ticket").prefetch_related("items__insumo"):
        pedido = Counter()
        for item in s.items.all():
            if _clave(item):
                pedido[_clave(item)] += item.total
        ya = _ya_entregado(s)
        for clave, cantidad in pedido.items():
            asignado = min(cantidad - ya[clave], restante[clave])
            if asignado > 0:
                restante[clave] -= asignado
                resultado[clave].append((s, asignado))
    return resultado


def total_apartado():
    return {clave: sum((c for _, c in lista), CERO) for clave, lista in apartados().items()}


def existencias(solicitud):
    """Cada ítem con lo que hay libre en bodega (descontando lo apartado para otros tickets) y lo que falta."""
    saldos = servicios.saldos_insumos()
    disponibles = Counter(r.upper() for r in Activo.objects.filter(estado=Activo.Estado.DISPONIBLE)
                          .exclude(referencia="").values_list("referencia", flat=True))
    de_otros = apartados(para=solicitud)
    filas = []
    for item in solicitud.items.select_related("insumo"):
        clave = _clave(item)
        if item.insumo:
            en_bodega = max(saldos[item.insumo.pk]["bodega"], CERO) if item.insumo.pk in saldos else CERO
        elif item.referencia:
            en_bodega = Decimal(disponibles[item.referencia.upper()])
        else:
            en_bodega = None  # escrito a mano: no está en el inventario
        otros = sum((c for _, c in de_otros.get(clave, [])), CERO) if clave else CERO
        hay = max(en_bodega - otros, CERO) if en_bodega is not None else None
        # Lo escrito a mano falta (hay que comprarlo) hasta que bodega confirme que lo tiene
        falta = max(item.total - hay, CERO) if hay is not None else (CERO if item.a_mano_listo else item.total)
        filas.append({"item": item, "hay": hay, "falta": falta, "en_inventario": hay is not None, "en_bodega": en_bodega,
                      "apartado_otros": otros, "para_quien": [s.numero for s, _ in de_otros.get(clave, [])]})
    return filas


def faltantes(solicitud):
    return [f for f in existencias(solicitud) if f["falta"] > 0]


# --- Avisos ------------------------------------------------------------------------------
def _correos(roles):
    """Correos de quienes firman cada rol (si no hay, el buzón administrativo o de soporte)."""
    por_rol = {R.BODEGA: "bodega", R.COORDINADOR: "supervisor", R.DIRECCION: "direccion"}
    correos = list(Usuario.objects.filter(is_active=True, rol__in=[por_rol[r] for r in roles]).exclude(email="")
                   .values_list("email", flat=True))
    return correos or [c for c in (settings.NOTIFICAR_ADMINISTRATIVA_EMAIL, settings.NOTIFICAR_SOPORTE_EMAIL) if c]


def _avisar(solicitud, asunto, lineas, destinatarios):
    t = solicitud.ticket
    cuerpo = "\n".join([f"Solicitud de material {solicitud.numero} · {t.radicado} · {t.empresa}", "", *lineas, "",
                        f"Ver la solicitud: {settings.SITIO_URL.rstrip('/')}{solicitud.get_absolute_url()}"])
    notif._enviar(f"[{solicitud.numero}] {asunto} · {t.empresa}", cuerpo, destinatarios)


def _nota(solicitud, autor, mensaje):
    Seguimiento.objects.create(ticket=solicitud.ticket, autor=autor, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                               mensaje=f"Solicitud de material {solicitud.numero}: {mensaje}")


def _resumen(solicitud):
    return [f"- {_n(i.cantidad)}{' + ' + _n(i.adicional) + ' adicional' if i.adicional else ''} {i.descripcion}"
            for i in solicitud.items.all()]


# --- Creación ----------------------------------------------------------------------------
@transaction.atomic
def desde_actividad(actividad, autor):
    """El coordinador planeó material en la visita: se pide a bodega (o se actualiza si aún no está aprobada)."""
    if not actividad.ticket_id or not actividad.insumos.strip():
        return None
    solicitud = (SolicitudMaterial.objects.filter(actividad=actividad, estado__in=SolicitudMaterial.ESTADOS_ABIERTOS)
                 .order_by("-creado").first())
    nuevo = solicitud is None
    if nuevo:
        solicitud = SolicitudMaterial.objects.create(
            ticket=actividad.ticket, actividad=actividad, origen=SolicitudMaterial.Origen.TECNICA, estado=S.VALIDACION,
            tecnico=actividad.encargados.first(), solicitado_por=autor,
            nota=f"Visita del {actividad.fecha:%d/%m/%Y} · {actividad.cliente}{' · ' + actividad.sede if actividad.sede else ''}")
    elif _mismo_material(solicitud, actividad.insumos):
        return solicitud  # se guardó la visita pero el material no cambió
    else:
        if solicitud.estado in (S.APROBACION, S.APROBADA):
            solicitud.ronda += 1  # cambió lo pedido: las firmas anteriores ya no valen
        solicitud.estado = S.VALIDACION
        solicitud.tecnico = actividad.encargados.first() or solicitud.tecnico
        solicitud.save()
    guardar_items(solicitud, actividad.insumos)
    _nota(solicitud, autor, "pedida a bodega desde el cronograma." if nuevo else "el material planeado cambió; vuelve a validación.")
    _avisar(solicitud, "Material solicitado por el área técnica" if nuevo else "Cambió el material solicitado",
            [f"{autor.get_full_name() or autor.username} pidió material para la visita ({solicitud.nota}).", *_resumen(solicitud), "",
             "Bodega: valide existencias y si el material está a cargo de JANOTEC o del cliente. Luego autoriza el coordinador TIC o dirección."],
            _correos([R.BODEGA]))
    return solicitud


def _mismo_material(solicitud, texto):
    actuales = sorted((i.insumo_id, i.referencia, i.cantidad) for i in solicitud.items.filter(seccion=ItemSolicitud.Seccion.APROBADO))
    nuevos = sorted((i.pk if i else None, r, c) for i, r, _, c in leer_materiales(texto))
    return actuales == nuevos


@transaction.atomic
def material_de_la_propuesta(ficha):
    """El material (sin servicios ni mano de obra) de la versión aprobada: del cotizador o, si no, del informe."""
    def es_servicio(texto):
        return texto.strip().lower().lstrip("0123456789., ").startswith(("servicio", "mano de obra", "instalación y", "configuración"))

    def sin_cantidad(texto):
        return re.sub(r"^[\s\d.,]*(?:m\b|und\b|unidades?\b)?\s*", "", texto.strip(" -•*\t"), flags=re.I).lower()

    # Lo que el informe declaró como servicio no pasa por bodega aunque no esté en el catálogo
    servicios_informe = {sin_cantidad(x) for x in ficha.informe.servicios.splitlines() if x.strip()} if ficha.informe_listo else set()
    version = ficha.versiones.filter(estado="aprobada").order_by("-numero").first()
    cotizacion = getattr(version, "cotizacion", None) if version else None
    if cotizacion is not None:
        return "\n".join(f"{l.cantidad.normalize():f} {l.descripcion}" for l in cotizacion.lineas.select_related("item")
                         if not (l.item and l.item.tipo == "servicio") and not es_servicio(l.descripcion)
                         and sin_cantidad(l.descripcion) not in servicios_informe)
    if ficha.informe_listo:
        return "\n".join(x for x in ficha.informe.materiales.splitlines() if x.strip() and not es_servicio(x))
    return ""


def pendiente_de_revision(ficha):
    """El material de la propuesta aprobada que bodega todavía no ha revisado (frena el traspaso a técnica)."""
    return ficha.ticket.solicitudes_material.filter(origen=SolicitudMaterial.Origen.COMERCIAL,
                                                    estado__in=[S.BORRADOR, S.VALIDACION]).first()


def desde_propuesta(ficha, autor):
    """Propuesta aprobada: el material aprobado va directo a bodega para revisión (si ya esperaba la aprobación
    del cliente, sigue). Si la propuesta no trae material identificable, comercial lo lista."""
    t = ficha.ticket
    esperando = t.solicitudes_material.filter(estado=S.PROPUESTA).first()
    if esperando:
        esperando.cliente_aprobo = True
        esperando.save(update_fields=["cliente_aprobo", "actualizado"])
        _nota(esperando, autor, "el cliente aprobó la propuesta.")
        return validar(esperando, autor, automatica=True)
    if t.solicitudes_material.filter(origen=SolicitudMaterial.Origen.COMERCIAL, estado__in=SolicitudMaterial.ESTADOS_ABIERTOS).exists():
        return None
    version = ficha.versiones.filter(estado="aprobada").order_by("-numero").first()
    texto = material_de_la_propuesta(ficha)
    informe = ficha.informe if ficha.informe_listo else None
    if not texto and informe and informe.servicios.strip() and not informe.materiales.strip():
        # Solo servicios (p. ej. un desmonte): no hay material que alistar; bodega no recibe una solicitud vacía
        Seguimiento.objects.create(ticket=t, autor=autor, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                                   mensaje="La propuesta aprobada solo tiene servicios: no hay material que alistar en bodega.")
        return None
    solicitud = SolicitudMaterial.objects.create(
        ticket=t, origen=SolicitudMaterial.Origen.COMERCIAL, estado=S.BORRADOR, cargo=SolicitudMaterial.Cargo.CLIENTE,
        modalidad="venta", cliente_aprobo=True, solicitado_por=ficha.comercial or autor,
        nota="Material de la propuesta aprobada" + (f" {version.codigo}" if version and version.codigo else "") + " para la instalación.")
    from comercial.servicios import _correos_comerciales
    if texto:
        guardar_items(solicitud, texto)
    if solicitud.items.exists():
        solicitud.estado = S.VALIDACION
        solicitud.save(update_fields=["estado", "actualizado"])
        _nota(solicitud, autor, "propuesta aprobada: el material pasó a revisión de bodega antes de la instalación.")
        _avisar(solicitud, "Revise el material de la propuesta aprobada",
                ["El cliente aprobó la propuesta. Antes de pasar la instalación al área técnica, bodega revisa el material:",
                 *_resumen(solicitud), "", "Confirme existencias (o cotice y compre lo que falte) y valídelo. Luego autoriza el coordinador TIC o dirección."],
                _correos([R.BODEGA]))
        return solicitud
    _avisar(solicitud, "Liste el material de la propuesta aprobada",
            ["La propuesta quedó aprobada. Escriba en la solicitud el material de la instalación para que bodega lo valide."],
            _correos_comerciales(ficha))
    return solicitud


# --- Pasos -------------------------------------------------------------------------------
@transaction.atomic
def enviar_a_bodega(solicitud, autor, texto):
    if solicitud.estado != S.BORRADOR:
        raise ValidationError("La solicitud ya fue enviada a bodega.")
    guardar_items(solicitud, texto)
    if not solicitud.items.exists():
        raise ValidationError("Escriba al menos un material.")
    solicitud.estado, solicitud.motivo_devolucion = S.VALIDACION, ""
    solicitud.save()
    _nota(solicitud, autor, "enviada a validación de bodega.")
    _avisar(solicitud, "Material por validar", [f"{autor.get_full_name() or autor.username} envió la solicitud.", *_resumen(solicitud)],
            _correos([R.BODEGA]))
    return solicitud


@transaction.atomic
def validar(solicitud, autor, cargo=None, modalidad=None, tecnico=None, adicionales=None, comentario="", automatica=False,
            fecha_devolucion=None, a_mano_listos=None):
    """Bodega revisa: a cargo de quién, existencias y holgura. Avanza a propuesta, compra o firmas."""
    if solicitud.estado not in (S.VALIDACION, S.COMPRA, S.PROPUESTA):
        raise ValidationError("La solicitud no está en validación.")
    if cargo is not None:
        solicitud.cargo = cargo
    if modalidad:
        solicitud.modalidad = modalidad
    if fecha_devolucion is not None or modalidad:
        prestamo = solicitud.modalidad in ("alquiler", "prestamo")
        solicitud.fecha_devolucion = (fecha_devolucion or solicitud.fecha_devolucion) if prestamo else None
    if tecnico:
        solicitud.tecnico = tecnico
    for item in solicitud.items.all():
        if adicionales and item.pk in adicionales:
            item.adicional = max(adicionales[item.pk], CERO)
            item.save(update_fields=["adicional"])
        if a_mano_listos is not None and item.pk in a_mano_listos:
            item.a_mano_listo = a_mano_listos[item.pk]
            item.save(update_fields=["a_mano_listo"])
    if not solicitud.cargo:
        raise ValidationError("Indique si el material está a cargo de JANOTEC o del cliente.")
    if not solicitud.modalidad:
        raise ValidationError("Indique la modalidad (venta, alquiler, reinversión o mantenimiento).")

    if solicitud.cargo == SolicitudMaterial.Cargo.CLIENTE and not solicitud.cliente_aprobo:
        solicitud.estado = S.PROPUESTA
        solicitud.save()
        _a_propuesta(solicitud, autor)
        return solicitud
    if solicitud.origen == SolicitudMaterial.Origen.COMERCIAL and not automatica and solicitud.estado == S.VALIDACION:
        transaction.on_commit(lambda: _avisar_revisado(solicitud))
    falta = faltantes(solicitud)
    if falta:
        nueva_compra = solicitud.estado != S.COMPRA
        solicitud.estado = S.COMPRA
        solicitud.save()
        if nueva_compra:
            _nota(solicitud, autor, "no hay existencias suficientes: se generó la solicitud de compra.")
            _avisar(solicitud, "Solicitud de compra", ["No hay existencias suficientes. Bodega cotiza y compra:",
                                                       *[f"- {_n(f['falta'])} {f['item'].descripcion}"
                                                         + ("" if f["en_inventario"] else " (no está en el inventario)") for f in falta],
                                                       "", "Al registrar la compra en el inventario, la solicitud sigue sola. Lo que no "
                                                       "está en el inventario: bodega marca «ya lo tengo» en la solicitud."],
                    _correos([R.BODEGA]))
        return solicitud
    solicitud.estado = S.APROBACION
    solicitud.save()
    if not automatica and (autor.rol == "bodega" or autor.is_superuser):
        # Validar es la firma de bodega: revisó que hay lo que se pidió y que corresponde a lo solicitado
        _firmar(solicitud, autor, R.BODEGA, True, comentario or "Existencias validadas.")
    _nota(solicitud, autor, "validada por bodega: pasa a firmas de aprobación.")
    return _revisar_firmas(solicitud, autor)


def _avisar_revisado(solicitud):
    """Al asesor: bodega ya revisó el material de la propuesta aprobada."""
    from comercial.servicios import _correos_comerciales
    ficha = getattr(solicitud.ticket, "ficha", None)
    if ficha is None:
        return
    solicitud.refresh_from_db()
    _avisar(solicitud, "Bodega revisó el material de la propuesta",
            [f"Resultado: {solicitud.get_estado_display()}.",
             "El avance del alistamiento se ve en el ticket; coordinación decide cuándo pasa a «Recepcionado»."],
            _correos_comerciales(ficha) + [c for c in (settings.NOTIFICAR_SOPORTE_EMAIL,) if c])


def _a_propuesta(solicitud, autor):
    """A cargo del cliente: el ticket pasa al área comercial para la propuesta; al aprobarse, la solicitud sigue."""
    from comercial import servicios as com

    lineas = ["Material a cargo del cliente: hay que enviarle una propuesta y esperar su aprobación antes de entregar.",
              *_resumen(solicitud)]
    com.pasar_a_propuesta(solicitud.ticket, autor, "\n".join(lineas + ([solicitud.nota] if solicitud.nota else [])))
    _nota(solicitud, autor, "a cargo del cliente: pasó al área comercial para la propuesta.")


def revisar_compras(autor=None):
    """Después de ingresar una compra: las solicitudes que ya tienen todo siguen a firmas."""
    liberadas = []
    for solicitud in SolicitudMaterial.objects.filter(estado=S.COMPRA):
        if not faltantes(solicitud):
            solicitud.estado = S.APROBACION
            solicitud.save(update_fields=["estado", "actualizado"])
            _nota(solicitud, autor, "ya ingresó la compra: pasa a firmas de aprobación.")
            _revisar_firmas(solicitud, autor)
            liberadas.append(solicitud)
    return liberadas


def rol_de(usuario):
    if usuario.rol == "bodega":
        return R.BODEGA
    if usuario.rol == "supervisor":
        return R.COORDINADOR
    if usuario.rol == "direccion":
        return R.DIRECCION
    return None


def firmas_pendientes(solicitud):
    firmadas = {R.DIRECCION if f.rol in AUTORIZAN else f.rol for f in solicitud.firmas_ronda if f.aprobada}
    return [r for r in solicitud.roles_requeridos if r not in firmadas]


def roles_que_puede_firmar(solicitud, usuario):
    if solicitud.estado != S.APROBACION:
        return []
    pendientes = firmas_pendientes(solicitud)
    if usuario.is_superuser:
        return pendientes
    rol = rol_de(usuario)
    if rol in AUTORIZAN and R.DIRECCION in pendientes:
        return [rol]
    return [rol] if rol in pendientes else []


def casillas(solicitud):
    """Las firmas de la ronda para mostrar: cada casilla con quién firmó (la autorización la da el coordinador o dirección)."""
    nombres = {R.BODEGA: "Encargado del inventario", R.DIRECCION: "Autorización (coordinador TIC o dirección)"}
    pendientes = firmas_pendientes(solicitud) if solicitud.estado == S.APROBACION else []
    filas = []
    for rol in solicitud.roles_requeridos:
        grupo = AUTORIZAN if rol == R.DIRECCION else (rol,)
        firmas = [f for f in solicitud.firmas_ronda if f.rol in grupo]
        filas.append({"nombre": nombres[rol], "firmas": firmas, "pendiente": rol in pendientes})
    return filas


def _firmar(solicitud, usuario, rol, aprobada, comentario, firma=None):
    f, _ = FirmaSolicitud.objects.update_or_create(solicitud=solicitud, ronda=solicitud.ronda, rol=rol, defaults={
        "usuario": usuario, "aprobada": aprobada, "comentario": comentario[:500]})
    if firma:
        f.imagen.save("firma.png", firma, save=True)


@transaction.atomic
def firmar(solicitud, usuario, aprobada, comentario="", rol=None, firma=None):
    """Firma de aprobación, a mano como en el acta de soporte. Un rechazo devuelve la solicitud para corregirla, con el motivo."""
    posibles = roles_que_puede_firmar(solicitud, usuario)
    rol = rol or (posibles[0] if posibles else None)
    if rol not in posibles:
        raise PermissionDenied
    if not aprobada and len(comentario.strip()) < 10:
        raise ValidationError("Explique por qué no aprueba (qué hay que corregir).")
    if aprobada and not firma:
        raise ValidationError("Firme en el recuadro para aprobar la salida.")
    _firmar(solicitud, usuario, rol, aprobada, comentario, firma)
    nombre = usuario.get_full_name() or usuario.username
    if not aprobada:
        solicitud.estado = S.BORRADOR if solicitud.origen == SolicitudMaterial.Origen.COMERCIAL else S.VALIDACION
        solicitud.motivo_devolucion = f"{nombre} ({dict(R.choices)[rol]}): {comentario}"[:500]
        solicitud.ronda += 1
        solicitud.save()
        _nota(solicitud, usuario, f"no aprobada por {nombre}: {comentario}")
        destinatarios = [solicitud.solicitado_por.email] if solicitud.solicitado_por and solicitud.solicitado_por.email else []
        _avisar(solicitud, "Solicitud no aprobada", [f"{nombre} no la aprobó: {comentario}", "Corríjala y vuelva a enviarla."],
                destinatarios + _correos([R.BODEGA]))
        return solicitud
    _nota(solicitud, usuario, f"firmada por {nombre} ({dict(R.choices)[rol]}).")
    return _revisar_firmas(solicitud, usuario)


def _revisar_firmas(solicitud, autor):
    pendientes = firmas_pendientes(solicitud)
    if pendientes:
        quienes = pendientes + ([R.COORDINADOR] if R.DIRECCION in pendientes else [])
        _avisar(solicitud, "Material por aprobar", [
            "Revise que lo que se va a entregar es lo mismo que el cliente solicitó y firme la aprobación de salida.",
            "Basta una autorización: la del coordinador TIC o la de dirección.", *_resumen(solicitud)], _correos(quienes))
        return solicitud
    solicitud.estado = S.APROBADA
    solicitud.save(update_fields=["estado", "actualizado"])
    _nota(solicitud, autor, "aprobada por todos: bodega puede entregarla al técnico.")
    tecnico = [solicitud.tecnico.email] if solicitud.tecnico and solicitud.tecnico.email else []
    _avisar(solicitud, "Material aprobado: por entregar", ["Todas las firmas están completas. El técnico lo recoge en bodega.",
                                                           *_resumen(solicitud)], _correos([R.BODEGA]) + tecnico)
    return solicitud


@transaction.atomic
def cancelar(solicitud, usuario, motivo):
    if solicitud.estado not in SolicitudMaterial.ESTADOS_ABIERTOS:
        raise ValidationError("La solicitud ya está cerrada.")
    solicitud.estado = S.CANCELADA
    solicitud.motivo_devolucion = motivo[:500]
    solicitud.save()
    _nota(solicitud, usuario, f"cancelada: {motivo}")


def _ya_entregado(solicitud):
    """Counter código/referencia → cantidad que ya salió de bodega con esta solicitud."""
    ya = Counter()
    for codigo, cantidad in servicios.MovimientoInsumo.objects.filter(solicitud=solicitud, tipo=servicios.T.ENTREGA) \
            .values_list("insumo__codigo", "cantidad"):
        ya[codigo] += cantidad
    for ref in servicios.MovimientoActivo.objects.filter(solicitud=solicitud, tipo=servicios.T.ENTREGA) \
            .values_list("activo__referencia", flat=True):
        ya[(ref or "").upper()] += 1
    return ya


def _a_mano_entregados(solicitud):
    """Ítems escritos a mano que ya figuran en un acta de entrega."""
    return {x.get("item") for a in solicitud.actas_entrega.all() for x in a.detalle if x.get("item")}


# --- Cuadre del material del ticket: entregado = instalado (acta) + devuelto ---------------
def cuadre(ticket):
    """Lo que salió de bodega para el ticket y en qué quedó. «Pendiente» es lo que el técnico aún tiene sin legalizar:
    ni lo descontó en el acta de servicio ni lo devolvió a bodega."""
    from .models import MovimientoActivo, MovimientoInsumo

    T = MovimientoInsumo.Tipo
    filas = {}
    for m in MovimientoInsumo.objects.filter(ticket=ticket, tipo__in=[T.ENTREGA, T.INSTALACION, T.DEVOLUCION]) \
            .select_related("insumo", "tecnico"):
        f = filas.setdefault((m.insumo_id, m.tecnico_id), {
            "codigo": m.insumo.codigo, "descripcion": m.insumo.descripcion, "tecnico": m.tecnico,
            "entregado": CERO, "instalado": CERO, "devuelto": CERO, "es_activo": False})
        f[{T.ENTREGA: "entregado", T.INSTALACION: "instalado", T.DEVOLUCION: "devuelto"}[m.tipo]] += m.cantidad
    for m in MovimientoActivo.objects.filter(ticket=ticket, tipo=T.ENTREGA).select_related("activo", "tecnico"):
        a = m.activo
        if ("a", a.pk) in filas:
            continue
        en_tecnico = a.lugar == "tecnico" and a.tecnico_id == m.tecnico_id
        filas[("a", a.pk)] = {"codigo": a.placa, "descripcion": a.descripcion_corta, "tecnico": m.tecnico, "es_activo": True,
                              "entregado": Decimal(1), "instalado": Decimal(0 if en_tecnico or a.lugar == "bodega" else 1),
                              "devuelto": Decimal(1 if a.lugar == "bodega" else 0)}
    lista = []
    for f in filas.values():
        if not f["entregado"]:
            continue  # solo cuenta lo que salió de bodega para este ticket
        f["pendiente"] = max(f["entregado"] - f["instalado"] - f["devuelto"], CERO)
        lista.append(f)
    lista.sort(key=lambda f: (not f["pendiente"], f["descripcion"].lower()))
    pendientes = [f for f in lista if f["pendiente"] > 0]
    return {"filas": lista, "pendientes": pendientes, "cuadrado": bool(lista) and not pendientes,
            "tecnicos": sorted({f["tecnico"] for f in pendientes if f["tecnico"]}, key=lambda u: u.pk)}


def resumen_pendiente(ticket):
    """Texto corto de lo que falta cuadrar (vacío si está cuadrado o no salió material)."""
    return "; ".join(f"{_n(f['pendiente'])} {f['descripcion']} ({f['tecnico'].get_full_name() if f['tecnico'] else 'sin técnico'})"
                     for f in cuadre(ticket)["pendientes"])


# --- Lista de alistamiento (la ve el área técnica; coordinación decide con ella) -----------
ESTADOS_LISTA = {
    "entregado": ("Entregado", "primary", "bi-box-arrow-right"),
    "disponible": ("Disponible", "success", "bi-check2-circle"),
    "comprar": ("Falta: en compra", "danger", "bi-cart"),
    "confirmar": ("Por confirmar en bodega", "warning", "bi-question-circle"),
}


def lista_alistamiento(solicitud):
    """La lista de chequeo: cada ítem con su disponibilidad, en dos secciones (aprobado y adicionales), y el avance."""
    ya = _ya_entregado(solicitud)
    a_mano_ent = _a_mano_entregados(solicitud)
    secciones = {clave: [] for clave, _ in ItemSolicitud.Seccion.choices}
    listos = 0
    for f in existencias(solicitud):
        item = f["item"]
        clave = item.insumo.codigo if item.insumo else (item.referencia or "").upper()
        if (clave and ya[clave] >= item.cantidad) or (not clave and item.pk in a_mano_ent):
            estado = "entregado"
        elif f["falta"] <= 0:
            estado = "disponible"
        elif f["en_inventario"]:
            estado = "comprar"
        else:
            estado = "confirmar"
        listos += estado in ("entregado", "disponible")
        nombre, color, icono = ESTADOS_LISTA[estado]
        secciones[item.seccion].append({**f, "estado": estado, "estado_nombre": nombre, "color": color, "icono": icono})
    total = sum(len(v) for v in secciones.values())
    return {"solicitud": solicitud, "aprobado": secciones["aprobado"], "adicional": secciones["adicional"],
            "listos": listos, "total": total, "avance": round(100 * listos / total) if total else 0,
            "pendientes_firma": firmas_pendientes(solicitud) if solicitud.estado == S.APROBACION else [],
            "compras": list(solicitud.ordenes_compra.exclude(estado="cancelada").select_related("proveedor")),
            "actas": list(solicitud.actas_entrega.select_related("tecnico", "adjunto"))}


@transaction.atomic
def agregar_adicionales(solicitud, autor, texto):
    """Bodega o coordinación agregan a la lista lo adicional que se necesita para poder instalar."""
    if solicitud.estado not in SolicitudMaterial.ESTADOS_ABIERTOS:
        raise ValidationError("La solicitud ya está cerrada: pida el material adicional en una solicitud nueva.")
    filas = leer_materiales(texto)
    if not filas:
        raise ValidationError("Escriba al menos un material adicional, con cantidad (uno por línea).")
    for insumo, referencia, descripcion, cantidad in filas:
        ItemSolicitud.objects.create(solicitud=solicitud, insumo=insumo, referencia=referencia,
                                     seccion=ItemSolicitud.Seccion.ADICIONAL, descripcion=descripcion[:300], cantidad=cantidad)
    if solicitud.estado in (S.APROBACION, S.APROBADA, S.COMPRA):
        if solicitud.estado != S.COMPRA:
            solicitud.ronda += 1  # cambió lo que sale: las firmas anteriores ya no valen
        solicitud.estado = S.VALIDACION
        solicitud.save()
    nombre = autor.get_full_name() or autor.username
    _nota(solicitud, autor, f"{nombre} agregó {len(filas)} adicional(es) para la instalación"
                            + (": vuelve a validación de bodega." if solicitud.estado == S.VALIDACION else "."))
    if autor.rol != "bodega":
        _avisar(solicitud, "Material adicional para la instalación",
                [f"{nombre} agregó a la lista lo adicional que se necesita para instalar:",
                 *[f"- {_n(c)} {d}" for _, _, d, c in filas], "", "Revise existencias y valide la solicitud."],
                _correos([R.BODEGA]))
    return len(filas)


def _pdf_acta_entrega(acta):
    """Genera (o vuelve a generar, con la firma de quien recibe) el PDF del acta de entrega en el ticket."""
    from django.core.files.base import ContentFile

    from .pdf import acta_entrega_pdf

    nombre, contenido = acta_entrega_pdf(acta)
    if acta.adjunto:
        acta.adjunto.archivo.save(nombre, ContentFile(contenido), save=False)
        acta.adjunto.tamano = len(contenido)
        acta.adjunto.save(update_fields=["archivo", "tamano"])
    return nombre, contenido


@transaction.atomic
def registrar_acta_entrega(solicitud, usuario, tecnico, activos, insumos, a_mano=(), firma_entrega=None, firma_recibe=None):
    """Acta de entrega con lo que salió en esta entrega (solo lo disponible), firmada a mano por quien entrega y, si el
    técnico está en bodega, por quien recibe. PDF en el ticket; si el técnico no firmó, se le avisa para que firme."""
    from django.core.files.base import ContentFile
    from django.utils import timezone

    from tickets.models import Adjunto

    por_codigo = {}
    for item in solicitud.items.select_related("insumo"):
        por_codigo.setdefault(item.insumo.codigo if item.insumo else (item.referencia or "").upper(), item.seccion)
    detalle = [{"seccion": por_codigo.get((a.referencia or "").upper(), "aprobado"), "cantidad": "1",
                "descripcion": a.descripcion_corta if hasattr(a, "descripcion_corta") else str(a), "codigo": a.placa}
               for a in activos]
    detalle += [{"seccion": por_codigo.get(i.codigo, "aprobado"), "cantidad": _n(c), "descripcion": i.descripcion,
                 "codigo": i.codigo} for i, c in insumos]
    detalle += [{"seccion": item.seccion, "cantidad": _n(item.cantidad), "descripcion": item.descripcion, "codigo": "",
                 "item": item.pk} for item in a_mano]
    acta = ActaEntrega.objects.create(solicitud=solicitud, tecnico=tecnico, entregado_por=usuario, detalle=detalle)
    if firma_entrega:
        acta.firma_entrega.save("firma.png", firma_entrega, save=False)
    if firma_recibe:  # el técnico firmó ahí mismo en bodega
        acta.firma_recibe.save("firma.png", firma_recibe, save=False)
        acta.recibido_en, acta.recibido_por = timezone.now(), tecnico
    acta.save()
    nombre, contenido = _pdf_acta_entrega(acta)
    t = solicitud.ticket
    quien = tecnico.get_full_name() or tecnico.username
    seg = Seguimiento.objects.create(
        ticket=t, autor=usuario, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
        mensaje=f"Acta de entrega {acta.numero} ({solicitud.numero}): bodega entregó {len(detalle)} ítem(s) a {quien}. "
                + (f"Firmada por quien entrega y por {quien}, que la recibió." if acta.recibido_en
                   else "Falta que el técnico firme que lo recibió."))
    archivo = ContentFile(contenido, name=nombre)
    acta.adjunto = Adjunto.objects.create(ticket=t, seguimiento=seg, archivo=archivo, nombre_original=nombre,
                                          tamano=len(contenido), subido_por=usuario)
    acta.save(update_fields=["adjunto"])
    if tecnico.email and not acta.recibido_en:
        transaction.on_commit(lambda: _avisar(solicitud, f"Acta de entrega {acta.numero}: firme que recibió el material", [
            f"Bodega le entregó {len(detalle)} ítem(s) para {t.radicado} · {t.empresa}:",
            *[f"- {x['cantidad']} {x['descripcion']}" + (f" [{x['codigo']}]" if x["codigo"] else "") for x in detalle], "",
            f"Firme que lo recibió en el ticket: {settings.SITIO_URL.rstrip('/')}{t.get_absolute_url()}#alistamiento"],
            [tecnico.email]))
    return acta


@transaction.atomic
def confirmar_recibido(acta, usuario, firma=None):
    """El técnico firma a mano que recibió lo del acta de entrega (si no firmó en bodega al momento de la entrega)."""
    if acta.recibido_en:
        return acta
    if not (usuario.is_superuser or acta.tecnico_id == usuario.pk):
        raise PermissionDenied
    if not firma:
        raise ValidationError("Firme en el recuadro que recibió el material.")
    from django.utils import timezone

    acta.firma_recibe.save("firma.png", firma, save=False)
    acta.recibido_en, acta.recibido_por = timezone.now(), usuario
    acta.save(update_fields=["firma_recibe", "recibido_en", "recibido_por"])
    _pdf_acta_entrega(acta)  # el PDF del ticket queda con las dos firmas
    _nota(acta.solicitud, usuario, f"{usuario.get_full_name() or usuario.username} firmó que recibió lo del acta de entrega {acta.numero}.")
    _a_instalacion_si_listo(acta, usuario)
    return acta


def material_para_instalacion(ticket):
    """Lo que bodega ya entregó para el ticket (actas de entrega), una línea por ítem, y si falta algo por entregar o
    por confirmar. Es lo que lleva el técnico a la instalación: al programarla no se vuelve a pedir a bodega."""
    solicitudes = ticket.solicitudes_material.exclude(estado=S.CANCELADA)
    actas = ActaEntrega.objects.filter(solicitud__in=solicitudes).order_by("creado")
    lineas = [f"{x['cantidad']} {x['descripcion']}" + (f" [{x['codigo']}]" if x.get("codigo") else "")
              for a in actas for x in a.detalle]
    pendiente = solicitudes.exclude(estado=S.ENTREGADA).exists() or actas.filter(recibido_en__isnull=True).exists()
    return {"texto": "\n".join(lineas), "pendiente": pendiente, "hay": solicitudes.exists()}


def _a_instalacion_si_listo(acta, usuario):
    """Con todo el material entregado y cada acta de entrega confirmada, la instalación pasa sola al área técnica:
    «Recepcionado», asignada al técnico que recibió el material, y coordinación recibe el aviso para programarla."""
    from tickets.models import Ticket

    t = acta.solicitud.ticket
    if t.estado != Ticket.Estado.EN_INVENTARIO:
        return
    solicitudes = t.solicitudes_material.exclude(estado=S.CANCELADA)
    if solicitudes.exclude(estado=S.ENTREGADA).exists() or \
            ActaEntrega.objects.filter(solicitud__in=solicitudes, recibido_en__isnull=True).exists():
        return  # falta material por entregar o actas por confirmar
    tecnico = acta.tecnico or usuario
    t.asignado_a = tecnico
    t.save(update_fields=["asignado_a", "actualizado"])
    nombre = tecnico.get_full_name() or tecnico.username
    t.reingresar(usuario, f"Bodega entregó todo el material y {nombre} confirmó que lo recibió: la instalación pasa al "
                          f"área técnica («Recepcionado»), asignada a {nombre}, para programarla en el cronograma.")
    notif._enviar(f"[{t.radicado}] Material entregado: instalación por programar · {t.empresa}",
                  "\n".join([f"{nombre} recibió todo el material de «{t.asunto}» (actas de entrega confirmadas).",
                             "El ticket pasó a «Recepcionado» y está asignado a él: prográmelo en el cronograma.", "",
                             f"{settings.SITIO_URL.rstrip('/')}{t.get_absolute_url()}"]),
                  [c for c in (settings.NOTIFICAR_SOPORTE_EMAIL,) if c])


def permitido_entregar(solicitud, activos, insumos):
    """Lo que bodega entrega debe ser lo aprobado (con su holgura). Devuelve los errores."""
    errores = []
    if solicitud.estado != S.APROBADA:
        return [f"La solicitud {solicitud.numero} no está aprobada ({solicitud.get_estado_display()})."]
    ya = _ya_entregado(solicitud)
    aprobado = Counter()
    for item in solicitud.items.select_related("insumo"):
        aprobado[item.insumo.codigo if item.insumo else item.referencia.upper()] += item.total
    de_otros = apartados(para=solicitud)
    saldos = servicios.saldos_insumos([i for i, _ in insumos]) if insumos else {}
    for insumo, cantidad in insumos:
        otros = de_otros.get(insumo.codigo, [])
        libre = (saldos[insumo.pk]["bodega"] if insumo.pk in saldos else CERO) - sum((c for _, c in otros), CERO)
        if otros and cantidad > libre:
            errores.append(f"{insumo.codigo}: solo hay {_n(max(libre, CERO))} libres; el resto está apartado para "
                           + ", ".join(f"{s.numero} ({s.ticket.radicado})" for s, _ in otros) + ".")
    disponibles = Counter(r.upper() for r in Activo.objects.filter(estado=Activo.Estado.DISPONIBLE)
                          .exclude(referencia="").values_list("referencia", flat=True))
    for ref, n in Counter((a.referencia or "").upper() for a in activos if a.referencia).items():
        otros = de_otros.get(ref, [])
        if otros and n > disponibles[ref] - sum((c for _, c in otros), CERO):
            errores.append(f"Equipos {ref}: los disponibles están apartados para "
                           + ", ".join(f"{s.numero} ({s.ticket.radicado})" for s, _ in otros) + ".")
    for insumo, cantidad in insumos:
        if ya[insumo.codigo] + cantidad > aprobado[insumo.codigo]:
            errores.append(f"{insumo.codigo}: aprobado {_n(aprobado[insumo.codigo])}, ya entregado {_n(ya[insumo.codigo])}.")
    pedidos = Counter((a.referencia or "").upper() for a in activos)
    for ref, n in pedidos.items():
        if ya[ref] + n > aprobado[ref]:
            errores.append(f"Equipos {ref or 'sin referencia'}: aprobados {_n(aprobado[ref])}, ya entregados {_n(ya[ref])}.")
    return errores


def marcar_entregada(solicitud):
    """Si ya salió todo lo aprobado (también lo escrito a mano), la solicitud queda «Entregada»."""
    ya = _ya_entregado(solicitud)
    a_mano_ent = _a_mano_entregados(solicitud)
    completa = all((ya[i.insumo.codigo if i.insumo else i.referencia.upper()] >= i.cantidad) if (i.insumo or i.referencia)
                   else i.pk in a_mano_ent for i in solicitud.items.select_related("insumo"))
    if completa:
        solicitud.estado = S.ENTREGADA
        solicitud.save(update_fields=["estado", "actualizado"])
    return completa


def por_entregar(solicitud):
    """Lo aprobado que aún no ha salido: insumos con su cantidad (con holgura), placas disponibles sugeridas y lo
    escrito a mano que bodega ya tiene (va en el acta de entrega aunque no se mueva en el inventario)."""
    ya = _ya_entregado(solicitud)
    a_mano_ent = _a_mano_entregados(solicitud)
    insumos, placas, a_mano = [], [], []
    for item in solicitud.items.select_related("insumo"):
        if item.insumo:
            resta = item.total - ya[item.insumo.codigo]
            if resta > 0:
                insumos.append((item.insumo, resta))
        elif item.referencia:
            resta = int(item.total - ya[item.referencia.upper()])
            if resta > 0:
                placas += list(Activo.objects.filter(estado=Activo.Estado.DISPONIBLE, referencia__iexact=item.referencia)
                               .exclude(placa__in=placas).order_by("placa").values_list("placa", flat=True)[:resta])
        elif item.pk not in a_mano_ent:
            a_mano.append(item)
    return {"insumos": insumos, "placas": placas, "a_mano": a_mano}


# --- Paso a paso de bodega por ticket ----------------------------------------------------
# (clave, nombre, lo que sigue, quién lo hace)
PASOS_BODEGA = [
    ("recibido", "Revisar la lista", "Revisar lo que pidieron, validar existencias y agregar adicionales", "Bodega"),
    ("compra", "Comprar lo que falta", "Cotizar y comprar lo que no hay; registrar la orden de compra", "Bodega"),
    ("autorizacion", "Autorización de salida", "Firma de salida: coordinador TIC o dirección (el que esté)", "Coordinación"),
    ("alistar", "Alistar y entregar", "Alistar el material y entregarlo al técnico con el acta de entrega", "Bodega"),
    ("confirmacion", "Confirmación del técnico", "El técnico confirma en la plataforma que recibió el material", "Técnico"),
    ("listo", "Material entregado", "Todo entregado y confirmado: el ticket pasa a técnica para instalar", ""),
]
ORDEN_PASOS = [p[0] for p in PASOS_BODEGA]
PASO_DE_ESTADO = {S.BORRADOR: "recibido", S.VALIDACION: "recibido", S.COMPRA: "compra", S.APROBACION: "autorizacion",
                  S.APROBADA: "alistar"}


def paso_bodega(solicitud):
    """En qué paso va una solicitud (None si espera la propuesta al cliente)."""
    if solicitud.estado == S.PROPUESTA:
        return None
    if solicitud.estado == S.ENTREGADA:
        return "confirmacion" if any(a.recibido_en is None for a in solicitud.actas_entrega.all()) else "listo"
    return PASO_DE_ESTADO.get(solicitud.estado)


def seguimiento_ticket(ticket, solicitudes):
    """Paso del ticket en bodega: el más atrasado de sus solicitudes (lo que falta para que pueda instalarse)."""
    pasos = [(s, paso_bodega(s)) for s in solicitudes]
    activos = [p for _, p in pasos if p]
    clave = min(activos, key=ORDEN_PASOS.index) if activos else ("propuesta" if pasos else "recibido")
    info = dict((p[0], p) for p in PASOS_BODEGA).get(clave, ("propuesta", "Espera al cliente",
                                                             "El cliente debe aprobar la propuesta del material", "Comercial"))
    n = ORDEN_PASOS.index(clave) + 1 if clave in ORDEN_PASOS else 0
    desde = min((s.creado for s in solicitudes), default=ticket.actualizado)
    from django.utils import timezone
    dias = (timezone.now() - desde).days
    items = sum(s.n_items if hasattr(s, "n_items") else s.items.count() for s in solicitudes)
    return {"ticket": ticket, "solicitudes": [s for s, _ in pasos], "paso": clave, "numero": n, "total": len(PASOS_BODEGA),
            "nombre": info[1], "sigue": info[2], "quien": info[3], "dias": dias, "items": items,
            "atrasado": clave not in ("listo", "confirmacion") and dias > 3,
            "pasos": [(c, nombre, (ORDEN_PASOS.index(c) + 1) < n or clave == "listo", c == clave) for c, nombre, _, _ in PASOS_BODEGA]}


def bandeja(ver="abiertos"):
    """Los tickets que bodega tiene en gestión, cada uno con su paso; los terminados de los últimos 30 días si se piden."""
    from django.db.models import Count, Q
    from django.utils import timezone

    from tickets.models import Ticket

    qs = (SolicitudMaterial.objects.exclude(estado=S.CANCELADA).select_related("ticket", "ticket__empresa", "ticket__asignado_a",
                                                                                "tecnico")
          .prefetch_related("actas_entrega").annotate(n_items=Count("items", distinct=True)))
    if ver == "terminados":
        qs = qs.filter(estado=S.ENTREGADA, actualizado__gte=timezone.now() - timezone.timedelta(days=30))
    else:
        qs = qs.filter(Q(estado__in=SolicitudMaterial.ESTADOS_ABIERTOS)
                       | Q(estado=S.ENTREGADA, actas_entrega__recibido_en__isnull=True)
                       | Q(ticket__estado=Ticket.Estado.EN_INVENTARIO))
    por_ticket = defaultdict(list)
    for s in qs.distinct().order_by("creado"):
        por_ticket[s.ticket].append(s)
    filas = [seguimiento_ticket(t, lista) for t, lista in por_ticket.items()]
    if ver == "terminados":
        en_gestion = {f["ticket"].pk for f in bandeja()}  # un ticket con otra solicitud abierta sigue en gestión
        filas = [f for f in filas if f["paso"] == "listo" and f["ticket"].pk not in en_gestion]
    elif ver != "todos":
        filas = [f for f in filas if f["paso"] != "listo" or f["ticket"].estado == Ticket.Estado.EN_INVENTARIO]
    # Sin solicitud todavía pero ya en inventario (la lista se está armando)
    con_sm = {f["ticket"].pk for f in filas}
    for t in ([] if ver == "terminados" else
              Ticket.objects.filter(estado=Ticket.Estado.EN_INVENTARIO).exclude(pk__in=con_sm).select_related("empresa")):
        filas.append(seguimiento_ticket(t, []))
    filas.sort(key=lambda f: (ORDEN_PASOS.index(f["paso"]) if f["paso"] in ORDEN_PASOS else -1, -f["dias"]))
    return filas
