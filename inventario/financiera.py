"""Flujo de la orden de compra con dirección y el área financiera.

1. Bodega arma la orden y la envía a dirección, que la aprueba o la devuelve con observaciones. Si es urgente y
   dirección no puede atenderla, bodega la pasa con un motivo: queda en el historial y dirección recibe el aviso.
2. Pago anticipado: financiera paga y sube el soporte de pago. A crédito: queda lista para el proveedor y
   financiera la paga después, contra la factura, al vencer el plazo.
3. Bodega se la envía al proveedor (orden + soporte de pago por correo, o por WhatsApp) y recibe lo que llega.
4. La factura llega a financiera, que la registra y la compara con la orden. Si no cuadra queda marcada, salvo
   que financiera acepte la diferencia con una nota.

Mientras el área financiera no tenga usuario, dirección hace su parte y recibe sus avisos.
"""
import logging
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Usuario
from tickets import notificaciones as notif

from .models import OrdenCompra

log = logging.getLogger(__name__)
E = OrdenCompra.Estado


# --- Permisos ------------------------------------------------------------------------------
def compras_requerido(vista):
    """Las órdenes de compra las ven bodega, la coordinación administrativa que las revisa, dirección y financiera
    (cada uno actúa solo en su paso)."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        """Deja pasar a bodega (gestiona el inventario) y a financiera o dirección; los demás reciben 403."""
        if not (request.user.gestiona_inventario or request.user.ve_financiera or request.user.revisa_compras):
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envoltura


def financiera_requerida(vista):
    """El módulo de financiera: financiera y dirección."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        """Deja pasar solo a financiera o dirección; los demás reciben 403."""
        if not request.user.ve_financiera:
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envoltura


# --- A quién le llega cada aviso -----------------------------------------------------------------
def _activos(filtro):
    """Correos de los usuarios activos que cumplen el filtro (se omiten los que no tienen correo)."""
    return list(Usuario.objects.filter(filtro, is_active=True).exclude(email="").values_list("email", flat=True))


def correos_direccion():
    """Correos de dirección (Miguel); si no hay ninguno, el buzón de soporte, para que el aviso no se pierda."""
    return _activos(Q(rol=Usuario.Rol.DIRECCION)) or [c for c in (settings.NOTIFICAR_SOPORTE_EMAIL,) if c]


def correos_financiera():
    """Área financiera; mientras no tenga a nadie, dirección."""
    return _activos(Q(rol=Usuario.Rol.FINANCIERA) | Q(tambien_financiera=True)) or correos_direccion()


def correos_revision():
    """Correos de quienes revisan las órdenes de compra (coordinación administrativa)."""
    return _activos(Q(revisa_compras=True))


def requiere_revision(orden, autor):
    """La orden pasa por la coordinación administrativa si hay quien revise, quien la envía no es de los que revisan
    y aún no la han revisado."""
    return (not orden.revisada_en and not getattr(autor, "revisa_compras", False)
            and Usuario.objects.filter(is_active=True, revisa_compras=True).exists())


def correos_bodega():
    """Correos de bodega; si no hay usuario de bodega, los buzones administrativo y de soporte."""
    return _activos(Q(rol=Usuario.Rol.BODEGA)) or [c for c in (settings.NOTIFICAR_ADMINISTRATIVA_EMAIL,
                                                                 settings.NOTIFICAR_SOPORTE_EMAIL) if c]


def _enlace(orden):
    """Enlace absoluto a la orden de compra, para ponerlo en los correos."""
    return f"{settings.SITIO_URL.rstrip('/')}{orden.get_absolute_url()}"


def _pesos(valor):
    """Valor en pesos colombianos con punto de miles: 1234567 → «$1.234.567»."""
    return f"${valor:,.0f}".replace(",", ".")


def _resumen(orden):
    """Líneas de resumen de la orden para los correos: proveedor, total con IVA, forma de pago y ticket."""
    t = orden.totales()
    return [f"Proveedor: {orden.proveedor or '—'}", f"Total: {_pesos(t['total'])} (con IVA)",
            f"Forma de pago: {orden.get_forma_pago_display()}" + (f" a {orden.dias_credito} días" if orden.a_credito and orden.dias_credito else "")
            ] + ([f"Para: {orden.solicitud.ticket.radicado} · {orden.solicitud.ticket.empresa}"] if orden.solicitud_id else [])


def _correo(asunto, cuerpo, destinos, adjuntos=()):
    """Correo con adjuntos; si falla, queda en el registro y la operación sigue."""
    destinos = [d for d in dict.fromkeys(destinos) if d]
    if not destinos:
        return False
    correo = EmailMultiAlternatives(asunto, cuerpo, settings.DEFAULT_FROM_EMAIL, destinos)
    correo.attach_alternative(notif.html_desde_texto(asunto, cuerpo), "text/html")
    for nombre, contenido, tipo in adjuntos:
        correo.attach(nombre, contenido, tipo)
    try:
        correo.send(fail_silently=False)
        return True
    except Exception:  # noqa: BLE001
        log.exception("No se pudo enviar «%s»", asunto)
        return False


def _archivo(campo):
    """(nombre, contenido, tipo) de un archivo subido, o None si no hay o no se puede leer."""
    if not campo:
        return None
    try:
        with campo.open("rb") as f:
            nombre = campo.name.rsplit("/", 1)[-1]
            return nombre, f.read(), "application/pdf" if nombre.lower().endswith(".pdf") else "application/octet-stream"
    except Exception:  # noqa: BLE001
        log.exception("No se pudo leer %s", campo.name)
        return None


def _pdf_orden(orden):
    """PDF de la orden de compra como adjunto de correo: (nombre, contenido, tipo)."""
    from .pdf import orden_compra_pdf
    nombre, contenido = orden_compra_pdf(orden)
    return nombre, contenido, "application/pdf"


def _nota(orden, autor, texto):
    """Lo deja en el historial de la solicitud de material del ticket, si la orden es para uno."""
    if orden.solicitud_id:
        from . import solicitudes as sol
        sol._nota(orden.solicitud, autor, texto)


# --- 1. Dirección aprueba -----------------------------------------------------------------------
def validar(orden):
    """Lo mínimo para que una orden pueda salir: proveedor, fecha de llegada, ítems con precio y plazo si es a crédito."""
    faltan = [t for t, v in (("el proveedor", orden.proveedor_id), ("la fecha estimada de llegada", orden.fecha_estimada),
                             ("al menos un ítem", orden.lineas.exists())) if not v]
    if orden.lineas.exists() and orden.totales()["total"] <= 0:
        faltan.append("el precio de los ítems")
    if orden.a_credito and not orden.dias_credito:
        faltan.append("los días de crédito")
    if faltan:
        raise ValidationError("Para enviarla falta " + ", ".join(faltan) + ".")


def enviar_a_direccion(orden, autor, motivo_urgencia=""):
    """Bodega pasa la orden a dirección. Si quien la envía es dirección, queda aprobada de una vez; si es urgente
    (con motivo), sigue sin esperar y dirección recibe el aviso.

    Antes de dirección la revisa la coordinación administrativa (firma «Revisó»): si quien la envía es de los que
    revisan, queda revisada por él; si no, pasa primero a revisión administrativa.
    """
    validar(orden)
    if autor.es_direccion or motivo_urgencia:
        return aprobar(orden, autor, motivo_urgencia)
    if requiere_revision(orden, autor):
        orden.estado, orden.observaciones_revision = E.POR_REVISAR, ""
        orden.save(update_fields=["estado", "observaciones_revision", "actualizado"])
        _nota(orden, autor, f"orden de compra {orden.numero} enviada a revisión administrativa.")
        notif._enviar(f"[{orden.numero}] Orden de compra para revisar · {orden.proveedor}",
                      "\n".join([f"{autor.get_full_name() or autor.username} preparó la orden de compra.", "",
                                 *_resumen(orden), "", "Revísela: puede corregirla y pasarla a dirección, o devolverla.",
                                 "", f"Revisar: {_enlace(orden)}"]),
                      correos_revision())
        return orden
    if getattr(autor, "revisa_compras", False) and not orden.revisada_en:
        orden.revisada_por, orden.revisada_en = autor, timezone.now()
    return _a_direccion(orden, autor)


def revisar(orden, autor):
    """La coordinación administrativa revisó la orden: queda su firma «Revisó» y pasa a dirección."""
    if orden.estado != E.POR_REVISAR:
        raise ValidationError("Solo se revisa una orden que está en revisión administrativa.")
    if not getattr(autor, "revisa_compras", False) and not autor.es_direccion:
        raise ValidationError("La revisa la coordinación administrativa.")
    validar(orden)
    if autor.es_direccion:
        return aprobar(orden, autor)
    orden.revisada_por, orden.revisada_en = autor, timezone.now()
    _nota(orden, autor, f"orden de compra {orden.numero} revisada por la coordinación administrativa.")
    return _a_direccion(orden, autor)


def devolver_revision(orden, autor, observaciones):
    """La coordinación administrativa devuelve la orden a quien la preparó con lo que hay que corregir."""
    observaciones = observaciones.strip()
    if orden.estado != E.POR_REVISAR:
        raise ValidationError("Solo se devuelve una orden que está en revisión administrativa.")
    if len(observaciones) < 5:
        raise ValidationError("Escriba qué hay que corregir.")
    orden.estado, orden.observaciones_revision = E.BORRADOR, observaciones[:2000]
    orden.save(update_fields=["estado", "observaciones_revision", "actualizado"])
    _nota(orden, autor, f"la coordinación administrativa devolvió la orden de compra {orden.numero}: {observaciones}")
    destinos = [orden.creado_por.email] if orden.creado_por and orden.creado_por.email else correos_bodega()
    notif._enviar(f"[{orden.numero}] Orden de compra devuelta para corregir",
                  "\n".join([f"{autor.get_full_name() or autor.username} devolvió la orden:", "", observaciones, "",
                             f"Corregir: {_enlace(orden)}"]),
                  destinos)


def _a_direccion(orden, autor):
    """La deja en revisión de dirección y le avisa."""
    orden.estado, orden.observaciones_direccion = E.REVISION, ""
    orden.save(update_fields=["estado", "observaciones_direccion", "revisada_por", "revisada_en", "actualizado"])
    _nota(orden, autor, f"orden de compra {orden.numero} enviada a dirección para su aprobación.")
    notif._enviar(f"[{orden.numero}] Orden de compra para aprobar · {orden.proveedor}",
                  "\n".join([f"{autor.get_full_name() or autor.username} envió la orden de compra para su aprobación.", "",
                             *_resumen(orden), "", "Puede aprobarla (pasa a financiera para el pago) o devolverla a bodega.",
                             "", f"Revisar: {_enlace(orden)}"]),
                  correos_direccion())
    return orden


def aprobar(orden, autor, motivo_urgencia=""):
    """Aprobada por dirección (o pasada por urgencia): anticipada → financiera la paga; a crédito → lista para el proveedor."""
    validar(orden)
    urgente = not autor.es_direccion
    if urgente and len(motivo_urgencia.strip()) < 10:
        raise ValidationError("La orden la aprueba dirección. Si es urgente, escriba el motivo (mínimo 10 caracteres).")
    with transaction.atomic():
        orden.estado = E.LISTA if orden.a_credito else E.POR_PAGAR
        orden.aprobada_en = timezone.now()
        orden.aprobada_por = None if urgente else autor
        orden.motivo_urgencia = motivo_urgencia.strip()[:500] if urgente else ""
        orden.observaciones_direccion = ""
        orden.save()
        _nota(orden, autor, f"orden de compra {orden.numero} " + (f"pasada sin aprobación de dirección por urgencia: {orden.motivo_urgencia}"
                                                                 if urgente else "aprobada por dirección") + ".")
    if urgente:
        notif._enviar(f"[{orden.numero}] Orden de compra pasada con urgencia, sin su aprobación · {orden.proveedor}",
                      "\n".join([f"{autor.get_full_name() or autor.username} pasó la orden sin su aprobación.", "",
                                 f"Motivo de la urgencia: {orden.motivo_urgencia}", "", *_resumen(orden), "",
                                 f"Ver la orden: {_enlace(orden)}"]),
                      correos_direccion())
    if orden.estado == E.POR_PAGAR:
        adjuntos = [_pdf_orden(orden)] + [a for a in (_archivo(orden.soporte),) if a]
        _correo(f"[{orden.numero}] Orden de compra por pagar · {orden.proveedor}",
                "\n".join(["Esta orden de compra es de pago anticipado: el proveedor despacha cuando reciba el soporte de pago.", "",
                           *_resumen(orden), "", "Adjuntamos la orden y la cotización del proveedor. Cuando pague, suba el "
                           "soporte en la plataforma: le llega a bodega para reenviárselo al proveedor.", "",
                           f"Registrar el pago: {_enlace(orden)}"]),
                correos_financiera(), adjuntos)
    else:
        notif._enviar(f"[{orden.numero}] Orden de compra aprobada: envíela al proveedor",
                      "\n".join(["La orden es a crédito: ya puede enviársela al proveedor.", "", *_resumen(orden), "",
                                 f"Enviar: {_enlace(orden)}"]),
                      correos_bodega())
    return orden


def devolver(orden, autor, observaciones):
    """Dirección devuelve la orden a bodega con lo que hay que corregir."""
    observaciones = observaciones.strip()
    if orden.estado != E.REVISION:
        raise ValidationError("Solo se devuelve una orden que está en revisión de dirección.")
    if len(observaciones) < 5:
        raise ValidationError("Escriba qué hay que corregir.")
    orden.estado, orden.observaciones_direccion = E.BORRADOR, observaciones[:2000]
    orden.revisada_por = orden.revisada_en = None  # corregida, se revisa de nuevo
    orden.save(update_fields=["estado", "observaciones_direccion", "revisada_por", "revisada_en", "actualizado"])
    _nota(orden, autor, f"dirección devolvió la orden de compra {orden.numero}: {observaciones}")
    notif._enviar(f"[{orden.numero}] Orden de compra devuelta para corregir",
                  "\n".join([f"{autor.get_full_name() or autor.username} devolvió la orden:", "", observaciones, "",
                             f"Corregir: {_enlace(orden)}"]),
                  correos_bodega())


# --- 2. Financiera paga ----------------------------------------------------------------------------
def registrar_pago(orden, autor, fecha, valor, medio, soporte):
    """Financiera registra el pago con su soporte. La anticipada queda lista para enviarla al proveedor."""
    if orden.pagada:
        raise ValidationError("Esta orden ya tiene el pago registrado.")
    if orden.estado in (E.BORRADOR, E.POR_REVISAR, E.REVISION, E.CANCELADA):
        raise ValidationError("La orden aún no está aprobada para pago.")
    if not soporte:
        raise ValidationError("Adjunte el soporte de pago.")
    if fecha > timezone.localdate():
        raise ValidationError("La fecha del pago no puede ser futura.")
    if valor <= 0:
        raise ValidationError("Escriba el valor pagado.")
    with transaction.atomic():
        orden.pagada_en, orden.valor_pagado, orden.medio_pago = fecha, valor, medio.strip()[:80]
        orden.soporte_pago, orden.pago_por = soporte, autor
        if orden.estado == E.POR_PAGAR:
            orden.estado = E.LISTA
        orden.save()
        _nota(orden, autor, f"financiera pagó la orden de compra {orden.numero} ({_pesos(valor)}).")
    if orden.estado == E.LISTA:
        notif._enviar(f"[{orden.numero}] Pago listo: reenvíe el soporte al proveedor · {orden.proveedor}",
                      "\n".join([f"Financiera pagó {_pesos(valor)} el {fecha:%d/%m/%Y}" + (f" por {orden.medio_pago}" if orden.medio_pago else "") + ".",
                                 "", "Envíele al proveedor la orden con el soporte de pago para que despache.", "",
                                 f"Enviar: {_enlace(orden)}"]),
                      correos_bodega())


# --- 3. Bodega se la envía al proveedor ------------------------------------------------------------
def correos_proveedor(proveedor):
    """Correos del proveedor sin repetir: primero el de su asesor, luego el general."""
    asesor = proveedor.asesor
    return [c for c in dict.fromkeys([asesor.correo if asesor else "", proveedor.correo]) if c]


def whatsapp_proveedor(orden):
    """Chat de WhatsApp del proveedor con el mensaje listo (los PDF se adjuntan a mano)."""
    pr = orden.proveedor
    if not pr:
        return ""
    asesor = pr.asesor
    numero = re.sub(r"\D", "", (asesor.celular if asesor and asesor.celular else pr.telefono) or "")
    if len(numero) == 10 and numero.startswith("3"):
        numero = "57" + numero
    texto = (f"Buenos días. Le enviamos la orden de compra {orden.numero} de JANOTEC S.A.S."
             + (" con el soporte de pago" if orden.pagada else "") + ". Le adjunto los PDF; quedamos atentos al despacho y la factura.")
    return f"https://wa.me/{numero}?text={quote(texto)}" if len(numero) >= 10 else ""


def enviar_al_proveedor(orden, autor):
    """Le manda al proveedor la orden (y el soporte de pago si es anticipada). Devuelve True si salió por correo."""
    if orden.estado != E.LISTA:
        raise ValidationError("La orden se envía al proveedor cuando está aprobada" +
                              (" y pagada." if not orden.a_credito else "."))
    destinos = correos_proveedor(orden.proveedor) if orden.proveedor else []
    enviado = False
    if destinos:
        adjuntos = [_pdf_orden(orden)] + [a for a in (_archivo(orden.soporte_pago),) if a]
        enviado = _correo(
            f"Orden de compra {orden.numero} · JANOTEC S.A.S.",
            "\n".join([f"Señores {orden.proveedor.razon_social}:", "",
                       f"Adjuntamos la orden de compra {orden.numero}" + (" y el soporte de pago" if orden.pagada else "") + ".",
                       f"Fecha estimada de entrega: {orden.fecha_estimada:%d/%m/%Y}." if orden.fecha_estimada else "",
                       "Por favor envíen la factura electrónica junto con el despacho.", "",
                       "Cordialmente,", "Bodega y logística · JANOTEC S.A.S."]),
            destinos, adjuntos)
    with transaction.atomic():
        orden.estado, orden.enviada_proveedor_en = E.ENVIADA, timezone.now()
        orden.save(update_fields=["estado", "enviada_proveedor_en", "actualizado"])
        _nota(orden, autor, f"orden de compra {orden.numero} enviada a {orden.proveedor}" +
              (f"; llega el {orden.fecha_estimada:%d/%m/%Y}" if orden.fecha_estimada else "") + ".")
    return enviado


def servicio_prestado(orden, autor, nota=""):
    """El contratista prestó el servicio (mano de obra subcontratada): sus líneas quedan recibidas. Si ya llegó todo
    el material, la orden queda recibida y financiera recibe el aviso para validar la factura."""
    if orden.estado not in (E.LISTA, E.ENVIADA):
        raise ValidationError("El servicio se confirma cuando la orden ya está aprobada (y pagada, si es anticipada).")
    lineas = orden.servicios_pendientes
    if not lineas:
        raise ValidationError("Esta orden no tiene servicios por confirmar.")
    with transaction.atomic():
        for l in lineas:
            l.recibido = l.total
            l.save(update_fields=["recibido"])
        if orden.completa:
            orden.estado, orden.recibida_por, orden.recibida_en = E.RECIBIDA, autor, timezone.now()
            orden.save(update_fields=["estado", "recibida_por", "recibida_en", "actualizado"])
        nota = " ".join(nota.split())[:300]
        _nota(orden, autor, f"orden de compra {orden.numero}: {orden.proveedor} prestó el servicio"
                            + (f" ({nota})" if nota else "") + ".")
    if orden.estado == E.RECIBIDA:
        recibida(orden)
    return orden


def recibida(orden):
    """Llegó la mercancía: si financiera aún no tiene la factura validada, le avisa para que la registre."""
    if orden.factura_validada_en:
        return
    notif._enviar(f"[{orden.numero}] Llegó lo comprado: valide la factura · {orden.proveedor}",
                  "\n".join(["Bodega recibió la mercancía de esta orden de compra." +
                             (f" Llegó con la factura {orden.factura}." if orden.factura else ""), "", *_resumen(orden), "",
                             "Cuando le llegue la factura, regístrela y compárela con la orden.", "",
                             f"Registrar la factura: {_enlace(orden)}"]),
                  correos_financiera())


# --- 4. Financiera valida la factura ------------------------------------------------------------
def registrar_factura(orden, autor, numero, fecha, valor, archivo=None, acepta_diferencia=False, nota=""):
    """Financiera registra la factura y la compara con la orden. Si cuadra (o acepta la diferencia con una nota),
    queda validada; si no, queda marcada como «factura con diferencia»."""
    if orden.estado in (E.BORRADOR, E.POR_REVISAR, E.REVISION, E.CANCELADA):
        raise ValidationError("La factura se registra cuando la orden ya está aprobada.")
    if not numero.strip() or valor <= 0:
        raise ValidationError("Escriba el número y el valor total de la factura.")
    if fecha > timezone.localdate():
        raise ValidationError("La fecha de la factura no puede ser futura.")
    nota = nota.strip()
    if acepta_diferencia and len(nota) < 5:
        raise ValidationError("Para aceptar la diferencia escriba el motivo en la nota.")
    with transaction.atomic():
        orden.factura, orden.fecha_factura, orden.factura_valor = numero.strip()[:60], fecha, valor
        orden.factura_nota = nota[:300]
        if archivo:
            orden.factura_pdf = archivo
        validada = orden.factura_cuadra or acepta_diferencia
        orden.factura_validada_en = timezone.now() if validada else None
        orden.factura_validada_por = autor if validada else None
        orden.save()
        d = orden.diferencia_factura
        _nota(orden, autor, f"financiera registró la factura {orden.factura} de la orden {orden.numero}" +
              ("" if orden.factura_cuadra else f" (diferencia de {_pesos(d)}{', aceptada: ' + nota if acepta_diferencia else ''})") + ".")
    return validada


# --- Vistas -----------------------------------------------------------------------------------
def _decimal(texto):
    """Valor de un campo numérico del formulario (type=number: «113050» o «113050.5»)."""
    try:
        valor = Decimal(str(texto or "").replace("$", "").replace(" ", "").replace(",", ".").strip() or "0")
    except InvalidOperation:
        return Decimal(0)
    return valor if valor.is_finite() else Decimal(0)


def _fecha(texto):
    """Convierte la fecha del formulario (AAAA-MM-DD); si viene vacía o mal escrita, error de validación."""
    try:
        return date.fromisoformat(texto or "")
    except ValueError:
        raise ValidationError("Escriba la fecha.")


@login_required
@compras_requerido
@require_POST
def accion(request, pk, cual):
    """Los pasos de la orden que no editan sus ítems: devolver (dirección), pago y factura (financiera), enviar al
    proveedor (bodega)."""
    orden = get_object_or_404(OrdenCompra.objects.select_related("proveedor", "solicitud__ticket"), pk=pk)
    u = request.user
    try:
        if cual == "devolver" and u.es_direccion:
            devolver(orden, u, request.POST.get("observaciones", ""))
            messages.info(request, f"{orden.numero} devuelta a bodega con sus observaciones.")
        elif cual == "pago" and u.ve_financiera:
            registrar_pago(orden, u, _fecha(request.POST.get("fecha")), _decimal(request.POST.get("valor")),
                           request.POST.get("medio", ""), request.FILES.get("soporte_pago"))
            messages.success(request, f"Pago de {orden.numero} registrado: bodega recibe el aviso para reenviar el soporte.")
        elif cual == "revisar" and (u.revisa_compras or u.es_direccion):
            revisar(orden, u)
            messages.success(request, f"{orden.numero} revisada: pasó a dirección para su aprobación." if orden.estado == E.REVISION
                             else f"{orden.numero} aprobada.")
        elif cual == "devolver_revision" and (u.revisa_compras or u.es_direccion):
            devolver_revision(orden, u, request.POST.get("observaciones", ""))
            messages.info(request, f"{orden.numero} devuelta a quien la preparó con sus observaciones.")
        elif cual == "servicio" and u.gestiona_inventario:
            servicio_prestado(orden, u, request.POST.get("nota", ""))
            messages.success(request, f"Servicio de {orden.proveedor} confirmado en {orden.numero}.")
        elif cual == "proveedor" and u.gestiona_inventario:
            if enviar_al_proveedor(orden, u):
                messages.success(request, f"{orden.numero} enviada por correo a {orden.proveedor}.")
            else:
                messages.warning(request, f"{orden.numero} quedó enviada, pero {orden.proveedor} no tiene correo: "
                                          "mándele los PDF por WhatsApp.")
        elif cual == "factura" and u.ve_financiera:
            ok = registrar_factura(orden, u, request.POST.get("numero", ""), _fecha(request.POST.get("fecha")),
                                   _decimal(request.POST.get("valor")), request.FILES.get("factura_pdf"),
                                   request.POST.get("acepta") == "1", request.POST.get("nota", ""))
            if ok:
                messages.success(request, f"Factura de {orden.numero} validada.")
            else:
                messages.warning(request, f"La factura no cuadra con la orden (diferencia de {_pesos(orden.diferencia_factura)}). "
                                          "Quedó marcada; acéptela con una nota o pida al proveedor que la corrija.")
        else:
            raise PermissionDenied
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    return redirect(orden)


@login_required
@compras_requerido
def archivo(request, pk, cual):
    """Soporte de pago o factura del proveedor."""
    orden = get_object_or_404(OrdenCompra, pk=pk)
    campo = {"soporte_pago": orden.soporte_pago, "factura_pdf": orden.factura_pdf}.get(cual)
    if not campo:
        raise Http404
    return FileResponse(campo.open("rb"), as_attachment=False, filename=campo.name.rsplit("/", 1)[-1])


@login_required
@financiera_requerida
def bandeja(request):
    """Módulo del área financiera: qué pagar, qué factura falta y cuál no cuadra."""
    base = OrdenCompra.objects.exclude(estado__in=[E.CANCELADA, E.BORRADOR]).select_related(
        "proveedor", "solicitud__ticket__empresa").prefetch_related("lineas")
    anticipadas = list(base.filter(estado=E.POR_PAGAR))
    credito = sorted(base.filter(forma_pago=OrdenCompra.FormaPago.CREDITO, pagada_en__isnull=True,
                                 estado__in=[E.LISTA, E.ENVIADA, E.RECIBIDA]),
                     key=lambda o: (o.vence is None, o.vence or date.max))
    sin_factura = list(base.filter(estado__in=[E.LISTA, E.ENVIADA, E.RECIBIDA], factura_valor__isnull=True))
    con_diferencia = list(base.filter(factura_valor__isnull=False, factura_validada_en__isnull=True))
    cerradas = [o for o in base.filter(estado=E.RECIBIDA, factura_validada_en__isnull=False, pagada_en__isnull=False)
                .order_by("-factura_validada_en")[:15]]
    return render(request, "inventario/financiera.html", {
        "anticipadas": anticipadas, "credito": credito, "sin_factura": sin_factura, "con_diferencia": con_diferencia,
        "cerradas": cerradas, "hoy": timezone.localdate(),
    })
