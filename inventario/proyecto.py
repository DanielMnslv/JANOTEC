"""Orden de compra del proyecto: la hoja consolidada de JANOTEC con todo lo de la propuesta.

Es el mismo formato con el que administrativa armaba la orden en Excel (PE-2026-10-047): el encabezado de la propuesta,
cada ítem con su proveedor, marca y referencia, cuánto se requiere y cuánto se compra (lo que hay en bodega sale de
inventario), el resumen por proveedor y las firmas Realizó (quien prepara las órdenes), Revisó (coordinación
administrativa) y Aprobó (dirección).

Mientras la propuesta no esté aprobada la hoja se puede ver, pero no tiene número: «PROPUESTA NO APROBADA - NO SE PUEDE
GENERAR OC». Aprobada, desde aquí se prepara la orden de cada proveedor (también la de la mano de obra subcontratada,
que no pasa por bodega).
"""
import re
from collections import Counter
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from tickets.models import Ticket

from . import servicios
from . import solicitudes as sol
from .models import Activo, Insumo, LineaOrdenCompra, OrdenCompra, Proveedor, SolicitudMaterial

CERO = Decimal(0)
INVENTARIO, COMPRAR = "INVENTARIO", "COMPRAR"
NO_APROBADA = "PROPUESTA NO APROBADA - NO SE PUEDE GENERAR OC"
SIN_PROVEEDOR = "Sin proveedor"


# --- Permisos ----------------------------------------------------------------------------------
def puede_ver(usuario):
    """La ven bodega, la coordinación que revisa compras, financiera, dirección y administrativa (arma la propuesta)."""
    return (usuario.gestiona_inventario or usuario.ve_financiera or getattr(usuario, "revisa_compras", False)
            or usuario.es_administrativo)


def _ticket(usuario, radicado):
    """El ticket con ficha comercial, si el usuario puede ver sus compras (403 si no)."""
    if not puede_ver(usuario):
        raise PermissionDenied
    return get_object_or_404(Ticket.objects.select_related("empresa", "sede", "categoria", "ficha"),
                             radicado=radicado, ficha__isnull=False)


# --- De dónde salen los datos ----------------------------------------------------------------------
def propuesta_del_proyecto(ficha):
    """(cotización, versión, aprobada): la de la versión aprobada; si no hay, la última enviada o la que se arma."""
    version = ficha.versiones.filter(estado="aprobada").order_by("-numero").first() or ficha.versiones.order_by("-numero").first()
    cotizacion = getattr(version, "cotizacion", None) if version else None
    if cotizacion is None:
        cotizacion = ficha.cotizaciones.order_by("-creado").first()
    return cotizacion, version, bool(version and version.estado == "aprobada")


def _clave(texto):
    """La descripción sin el [CÓDIGO] de bodega ni mayúsculas: así se reconoce el mismo ítem en la solicitud y la orden."""
    return " ".join(re.sub(r"\s*\[[^\]]+\]\s*$", "", texto or "").lower().split())


def ordenes_del_proyecto(ticket):
    """Las órdenes de compra del ticket que siguen vigentes (sin las canceladas)."""
    return (OrdenCompra.objects.filter(Q(ticket=ticket) | Q(solicitud__ticket=ticket)).exclude(estado=OrdenCompra.Estado.CANCELADA)
            .select_related("proveedor", "creado_por", "revisada_por", "aprobada_por").distinct())


def _solicitud(ticket):
    """La solicitud de material de la propuesta aprobada (la más reciente que no se canceló)."""
    return (ticket.solicitudes_material.filter(origen=SolicitudMaterial.Origen.COMERCIAL)
            .exclude(estado=SolicitudMaterial.Estado.CANCELADA).order_by("-creado").first())


def _en_bodega(linea, saldos, disponibles):
    """Cuánto hay libre en bodega de una línea de la propuesta (sin solicitud todavía); None si no se sabe qué es."""
    m = re.search(r"\[([^\]]+)\]\s*$", linea.descripcion)
    codigo = m.group(1).strip().upper() if m else ""
    if codigo:
        insumo = Insumo.objects.filter(codigo=codigo).first()
        if insumo:
            return max(saldos[insumo.pk]["bodega"], CERO) if insumo.pk in saldos else CERO
        if codigo in disponibles:
            return Decimal(disponibles[codigo])
    referencia = (linea.referencia or "").strip().upper()
    if referencia:
        return Decimal(disponibles.get(referencia, 0))
    return None


def filas(ticket, cotizacion):
    """Cada línea de la propuesta con lo que se requiere, lo que se compra, a quién y por cuánto (precio del proveedor).

    Lo que se compra sale de la revisión de bodega (la solicitud de material) o, antes de ella, de lo que hay en bodega.
    Si ya hay órdenes de compra, manda lo que se ordenó. La mano de obra se compra siempre (al contratista).
    """
    if cotizacion is None:
        return []
    solicitud = _solicitud(ticket)
    revision = {}
    if solicitud and solicitud.estado in SolicitudMaterial.ESTADOS_ABIERTOS:
        revision = {_clave(f["item"].descripcion): f for f in sol.existencias(solicitud) if f["item"].seccion == "aprobado"}
    ordenado, proveedor_oc, precio_oc = Counter(), {}, {}
    for l in LineaOrdenCompra.objects.filter(orden__in=ordenes_del_proyecto(ticket)).select_related("orden__proveedor", "item"):
        clave = _clave(l.item.descripcion if l.item else l.descripcion)
        ordenado[clave] += l.cantidad
        proveedor_oc.setdefault(clave, l.orden.proveedor)
        precio_oc.setdefault(clave, l.valor_unitario)
    saldos = servicios.saldos_insumos()
    disponibles = Counter(r.upper() for r in Activo.objects.filter(estado=Activo.Estado.DISPONIBLE)
                          .exclude(referencia="").values_list("referencia", flat=True))
    resultado = []
    etiquetas = dict(cotizacion.lineas.model.Categoria.choices)
    for linea in cotizacion.lineas.select_related("item", "item__proveedor", "proveedor"):
        clave = _clave(linea.descripcion)
        requerida = linea.cantidad
        if linea.es_servicio:
            comprar = requerida
        elif ordenado[clave]:
            comprar = min(ordenado[clave], requerida)
        elif clave in revision and (revision[clave]["en_inventario"] or revision[clave]["item"].a_mano_listo):
            comprar = min(revision[clave]["falta"], requerida)
        else:
            hay = _en_bodega(linea, saldos, disponibles)
            comprar = requerida if hay is None else max(requerida - hay, CERO)
        if comprar <= 0:
            decision = INVENTARIO
        elif comprar >= requerida:
            decision = COMPRAR
        else:
            decision = f"COMPRAR {comprar.normalize():f} · {(requerida - comprar).normalize():f} DE INVENTARIO"
        proveedor = linea.proveedor or (linea.item.proveedor if linea.item else None) or proveedor_oc.get(clave)
        unitario = precio_oc.get(clave, linea.valor_unitario) if comprar > 0 else None
        base = (unitario * comprar).quantize(Decimal(1)) if unitario is not None else None
        iva = (base * linea.iva / 100).quantize(Decimal(1)) if base is not None else None
        resultado.append({
            "linea": linea, "clave": clave, "categoria": etiquetas[linea.categoria_final].upper(),
            "descripcion": re.sub(r"\s*\[[^\]]+\]\s*$", "", linea.descripcion), "requerida": requerida, "comprar": comprar,
            "decision": decision, "proveedor": proveedor, "marca": linea.marca or (linea.item.marca if linea.item else ""),
            "referencia": linea.referencia or (linea.item.referencia if linea.item else ""), "servicio": linea.es_servicio,
            "unitario": unitario, "base": base, "iva": iva, "total": (base + iva) if base is not None else None,
            "ordenado": ordenado[clave], "pendiente": max(comprar - ordenado[clave], CERO),
        })
    return resultado


def resumen_por_proveedor(lista):
    """[{proveedor, items, base, iva, total}] de lo que se compra, en el orden en que aparece cada proveedor."""
    grupos = {}
    for f in lista:
        if f["comprar"] <= 0:
            continue
        nombre = f["proveedor"].razon_social if f["proveedor"] else SIN_PROVEEDOR
        g = grupos.setdefault(nombre, {"proveedor": nombre, "pk": f["proveedor"].pk if f["proveedor"] else None,
                                       "items": 0, "base": CERO, "iva": CERO, "total": CERO, "pendientes": 0})
        g["items"] += 1
        g["pendientes"] += 1 if f["pendiente"] > 0 else 0
        for k in ("base", "iva", "total"):
            g[k] += f[k] or CERO
    return list(grupos.values())


def _personas(usuarios):
    """[(nombre, cargo)] sin repetir, en el orden en que aparecen."""
    vistos = {}
    for u in usuarios:
        if u and u.pk not in vistos:
            vistos[u.pk] = ((u.get_full_name() or u.username).upper(), (u.cargo or "").upper())
    return list(vistos.values())


def hoja(ticket):
    """Todo lo que lleva la orden de compra del proyecto (para la pantalla y el PDF)."""
    from comercial import cotizador as coti

    ficha = ticket.ficha
    cotizacion, version, aprobada = propuesta_del_proyecto(ficha)
    if version and version.codigo:
        codigo = version.codigo
    else:
        codigo = coti.codigo(ficha, ficha.versiones.count() + 1, cotizacion)
    id_proyecto = codigo.rsplit("-V", 1)[0]
    lista = filas(ticket, cotizacion)
    ordenes = list(ordenes_del_proyecto(ticket))
    alcance = (cotizacion.alcance.strip() if cotizacion else "")
    if cotizacion is None:
        modalidad = "—"
    elif cotizacion.modalidad == cotizacion.Modalidad.HIBRIDO:
        modalidad = "Venta/Alquiler"
    else:
        modalidad = cotizacion.get_modalidad_display().split(" (")[0]
    return {
        "ticket": ticket, "ficha": ficha, "cotizacion": cotizacion, "version": version, "aprobada": aprobada,
        "numero": f"OC-{id_proyecto}" if aprobada else NO_APROBADA, "codigo": codigo, "id_proyecto": id_proyecto,
        "modalidad": modalidad, "cliente": ticket.empresa.razon_social, "sede": str(ticket.sede or "—"),
        "nit": ticket.empresa.nit or "—",
        "objeto": ticket.asunto if not alcance or alcance.startswith("Ver el informe") else alcance,
        "linea_servicio": ticket.categoria.nombre if ticket.categoria_id else "—",
        "fecha": timezone.localtime(version.enviada).date() if version else timezone.localdate(),
        "fecha_aprobacion": timezone.localtime(version.respondida).date() if aprobada and version.respondida else None,
        "filas": lista, "resumen": resumen_por_proveedor(lista), "ordenes": ordenes,
        "totales": {k: sum((f[k] or CERO for f in lista), CERO) for k in ("base", "iva", "total")},
        "items_comprar": sum(1 for f in lista if f["comprar"] > 0),
        "realizo": _personas(o.creado_por for o in ordenes), "reviso": _personas(o.revisada_por for o in ordenes),
        "aprobo": _personas(o.aprobada_por for o in ordenes),
    }


def por_ordenar(limite=30):
    """Proyectos con la propuesta aprobada a los que aún les falta ordenar algo: [{ticket, codigo, pendientes, proveedores}].
    Es lo que bodega ve arriba en «Órdenes de compra» para generarlas desde la propuesta."""
    tickets = (Ticket.objects.filter(ficha__versiones__estado="aprobada").exclude(estado__in=Ticket.ESTADOS_FINALES)
               .select_related("empresa", "sede", "ficha").distinct().order_by("-pk")[:limite])
    resultado = []
    for t in tickets:
        cotizacion, version, aprobada = propuesta_del_proyecto(t.ficha)
        if not aprobada:
            continue
        lista = [f for f in filas(t, cotizacion) if f["pendiente"] > 0]
        if lista:
            resultado.append({"ticket": t, "codigo": (version.codigo or "").rsplit("-V", 1)[0], "pendientes": len(lista),
                              "proveedores": len({f["proveedor"].pk if f["proveedor"] else None for f in lista})})
    return resultado


# --- Preparar la orden de un proveedor desde la hoja -------------------------------------------------
@transaction.atomic
def preparar_orden(ticket, proveedor, autor):
    """Orden en preparación con lo que falta ordenar de ese proveedor (proveedor None = los ítems sin proveedor).

    Los ítems de material quedan ligados a la solicitud de material para que, al llegar, queden apartados para el
    ticket; la mano de obra va como servicio (no entra a bodega: se confirma prestado).
    """
    cotizacion, _, aprobada = propuesta_del_proyecto(ticket.ficha)
    if not aprobada:
        raise PermissionDenied(NO_APROBADA)
    pendientes = [f for f in filas(ticket, cotizacion) if f["pendiente"] > 0
                  and (f["proveedor"].pk if f["proveedor"] else None) == (proveedor.pk if proveedor else None)]
    if not pendientes:
        return None
    solicitud = _solicitud(ticket)
    items = {_clave(i.descripcion): i for i in solicitud.items.filter(seccion="aprobado")} if solicitud else {}
    orden = OrdenCompra.objects.create(proveedor=proveedor, ticket=ticket, solicitud=solicitud, creado_por=autor)
    for f in pendientes:
        LineaOrdenCompra.objects.create(
            orden=orden, item=None if f["servicio"] else items.get(f["clave"]), descripcion=f["descripcion"][:300],
            cantidad=f["pendiente"], valor_unitario=f["unitario"] or f["linea"].valor_unitario, iva=f["linea"].iva,
            es_servicio=f["servicio"], marca=f["marca"][:80], referencia=f["referencia"][:80])
    if solicitud:
        sol._nota(solicitud, autor, f"se preparó la orden de compra {orden.numero}"
                                    + (f" con {proveedor}" if proveedor else "") + " desde la orden del proyecto.")
    return orden


# --- Vistas ------------------------------------------------------------------------------------
@login_required
def proyecto(request, radicado):
    """La orden de compra del proyecto en pantalla, con el botón para preparar la orden de cada proveedor."""
    ticket = _ticket(request.user, radicado)
    return render(request, "inventario/orden_proyecto.html", {"h": hoja(ticket), "puede_ordenar": request.user.gestiona_inventario})


@login_required
def proyecto_pdf(request, radicado):
    """El PDF de la orden de compra del proyecto, con el formato de la hoja de JANOTEC."""
    from .pdf import orden_proyecto_pdf

    nombre, contenido = orden_proyecto_pdf(hoja(_ticket(request.user, radicado)))
    respuesta = HttpResponse(contenido, content_type="application/pdf")
    respuesta["Content-Disposition"] = f'inline; filename="{nombre}"'
    return respuesta


@login_required
@require_POST
def proyecto_ordenar(request, radicado):
    """Bodega prepara la orden de un proveedor con lo que falta ordenar de él."""
    ticket = _ticket(request.user, radicado)
    if not request.user.gestiona_inventario:
        raise PermissionDenied
    valor = request.POST.get("proveedor", "")
    proveedor = get_object_or_404(Proveedor, pk=valor) if valor.isdigit() else None
    try:
        orden = preparar_orden(ticket, proveedor, request.user)
    except PermissionDenied:
        messages.error(request, "La propuesta aún no está aprobada: no se puede generar la orden de compra.")
        return redirect("inventario:orden_proyecto", radicado)
    if orden is None:
        messages.info(request, "De ese proveedor no queda nada por ordenar.")
        return redirect("inventario:orden_proyecto", radicado)
    messages.success(request, f"Se preparó {orden.numero}" + ("" if proveedor else ": elija el proveedor") +
                     ". Revise fechas y forma de pago y envíela a revisión.")
    return redirect(orden)
