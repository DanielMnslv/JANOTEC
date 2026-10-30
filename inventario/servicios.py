"""Reglas del inventario: todo cambio pasa por aquí para que el stock sea siempre correcto.

Stock de un insumo en un lugar = lo que entró a ese lugar − lo que salió de él.
Lugar de un activo = el destino de su último movimiento (se guarda en el Activo).
"""
from collections import defaultdict
from datetime import timedelta  # noqa: F401 (lo usan las vistas)
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Activo, Insumo, Lugar, Modalidad, MovimientoActivo, MovimientoInsumo, normalizar

T = MovimientoActivo.Tipo
E = Activo.Estado
CERO = Decimal("0")

ESTADO_POR_MODALIDAD = {
    Modalidad.ALQUILER: E.ALQUILADO, Modalidad.PRESTAMO: E.PRESTADO, Modalidad.VENTA: E.VENDIDO,
    Modalidad.REINVERSION: E.VENDIDO,
    Modalidad.ASIGNACION: E.ASIGNADO, Modalidad.MANTENIMIENTO: E.VENDIDO,
}


def gestiona_inventario(usuario):
    """Bodega, área técnica (coordinador) y dirección registran compras, entregas y devoluciones."""
    return usuario.is_authenticated and (usuario.is_superuser or usuario.rol in ("bodega", "direccion"))


def ve_inventario(usuario):
    return gestiona_inventario(usuario) or (usuario.is_authenticated and usuario.rol == "agente")


def _nombre(usuario):
    return (usuario.get_full_name() or usuario.username) if usuario else ""


# --- Stock de insumos ------------------------------------------------------------------
def saldos_insumos(insumos=None, tecnico=None):
    """{insumo_id: {"bodega": n, "tecnicos": {clave: n}, "entradas": n, "salidas": n}}.

    clave de técnico = id del usuario o, para datos importados sin usuario, el nombre.
    """
    movs = MovimientoInsumo.objects.all()
    if insumos is not None:
        movs = movs.filter(insumo__in=insumos)
    if tecnico is not None:
        movs = movs.filter(Q(origen=Lugar.TECNICO) | Q(destino=Lugar.TECNICO), tecnico=tecnico)
    saldos = defaultdict(lambda: {"bodega": CERO, "tecnicos": defaultdict(lambda: CERO), "entradas": CERO, "salidas": CERO})
    for m in movs.values("insumo_id", "origen", "destino", "cantidad", "tecnico_id", "tecnico_nombre"):
        s = saldos[m["insumo_id"]]
        clave = m["tecnico_id"] or m["tecnico_nombre"]
        if m["destino"] == Lugar.BODEGA:
            s["bodega"] += m["cantidad"]
            s["entradas"] += m["cantidad"]
        if m["origen"] == Lugar.BODEGA:
            s["bodega"] -= m["cantidad"]
            s["salidas"] += m["cantidad"]
        if m["destino"] == Lugar.TECNICO:
            s["tecnicos"][clave] += m["cantidad"]
        if m["origen"] == Lugar.TECNICO:
            s["tecnicos"][clave] -= m["cantidad"]
    return saldos


def costo_promedio(insumos=None):
    """Costo unitario con IVA = total pagado ÷ unidades compradas (como VALOR UNITARIO de INVENTARIO_INSUMO)."""
    totales = defaultdict(lambda: [CERO, CERO])
    compras = MovimientoInsumo.objects.filter(tipo=T.COMPRA, valor_unitario__isnull=False)
    if insumos is not None:
        compras = compras.filter(insumo__in=insumos)
    for c in compras.values("insumo_id", "cantidad", "valor_unitario", "iva"):
        totales[c["insumo_id"]][0] += c["cantidad"] * c["valor_unitario"] * (1 + (c["iva"] or CERO) / 100)
        totales[c["insumo_id"]][1] += c["cantidad"]
    return {k: (v[0] / v[1] if v[1] else CERO) for k, v in totales.items()}


def en_poder_de(tecnico):
    """Lo que tiene el técnico ahora: (activos, [(insumo, cantidad)])."""
    activos = list(Activo.objects.filter(lugar=Lugar.TECNICO, tecnico=tecnico).order_by("placa"))
    saldos = saldos_insumos(tecnico=tecnico)
    ids = [i for i, s in saldos.items() if s["tecnicos"].get(tecnico.pk, CERO) > 0]
    insumos = [(i, saldos[i.pk]["tecnicos"][tecnico.pk]) for i in Insumo.objects.filter(pk__in=ids)]
    return activos, insumos


def catalogo_planeacion():
    """Lo que el coordinador puede planear para una visita: insumos (por código) y activos disponibles agrupados por
    referencia, con lo que hay en bodega. [{codigo, nombre, unidad: m|und, disponible, clase}]"""
    from .solicitudes import total_apartado

    saldos = saldos_insumos()
    apartado = total_apartado()  # lo que ya está apartado para otros tickets no se puede planear
    items = []
    for i in Insumo.objects.filter(activo=True):
        hay = (saldos[i.pk]["bodega"] if i.pk in saldos else CERO) - apartado.get(i.codigo, CERO)
        items.append({"codigo": i.codigo, "nombre": i.descripcion, "unidad": "m" if i.unidad == Insumo.Unidad.METRO else "und",
                      "disponible": float(max(hay, CERO)), "clase": "insumo"})
    grupos = defaultdict(list)
    for a in Activo.objects.filter(estado=E.DISPONIBLE):
        grupos[a.referencia or a.descripcion_corta].append(a)
    for codigo, lista in grupos.items():
        libres = len(lista) - apartado.get((lista[0].referencia or "").upper(), CERO)
        items.append({"codigo": codigo, "nombre": lista[0].descripcion_corta, "unidad": "und",
                      "disponible": float(max(libres, 0)), "clase": "activo"})
    return sorted(items, key=lambda x: (x["disponible"] <= 0, x["nombre"].lower()))


def existencias_por_codigo():
    """{código o referencia: disponible en bodega} para comparar con lo planeado en el cronograma."""
    return {i["codigo"].upper(): i["disponible"] for i in catalogo_planeacion()}


def disponible_en(insumo, lugar, tecnico=None):
    s = saldos_insumos([insumo], tecnico=tecnico if lugar == Lugar.TECNICO else None).get(insumo.pk)
    if not s:
        return CERO
    return s["bodega"] if lugar == Lugar.BODEGA else s["tecnicos"].get(tecnico.pk if tecnico else None, CERO)


# --- Movimientos -----------------------------------------------------------------------
def _datos_comunes(kw, usuario):
    tecnico = kw.pop("tecnico", None)
    datos = {"tecnico": tecnico, "tecnico_nombre": kw.pop("tecnico_nombre", "") or _nombre(tecnico),
             "registrado_por": usuario, "registrado_por_nombre": kw.pop("registrado_por_nombre", "") or _nombre(usuario),
             "fecha": kw.pop("fecha", None) or timezone.localdate()}
    datos.update(kw)
    return datos


def mover_activo(activo, tipo, destino, usuario=None, estado=None, **kw):
    """Registra el movimiento y deja el activo donde quedó."""
    datos = _datos_comunes(kw, usuario)
    modalidad = datos.get("modalidad") or ""
    if estado is None:
        estado = {Lugar.BODEGA: E.DISPONIBLE, Lugar.PROVEEDOR: E.GARANTIA, Lugar.FUERA: E.BAJA}.get(destino)
        if destino == Lugar.TECNICO:
            estado = E.ASIGNADO if modalidad == Modalidad.ASIGNACION else E.EN_TECNICO
        elif destino == Lugar.CLIENTE:
            estado = ESTADO_POR_MODALIDAD.get(modalidad, E.VENDIDO)
    mov = MovimientoActivo.objects.create(activo=activo, tipo=tipo, origen=activo.lugar, destino=destino, estado=estado,
                                          **datos)
    activo.estado, activo.lugar, activo.fecha_ultimo_movimiento = estado, destino, mov.fecha
    activo.observaciones = mov.observaciones or activo.observaciones
    if destino in (Lugar.TECNICO, Lugar.CLIENTE):
        activo.tecnico, activo.tecnico_nombre = mov.tecnico, mov.tecnico_nombre
    elif destino == Lugar.BODEGA:
        activo.tecnico, activo.tecnico_nombre = None, mov.registrado_por_nombre or activo.tecnico_nombre
    if destino == Lugar.CLIENTE or (destino == Lugar.TECNICO and (mov.empresa or mov.cliente_texto)):
        activo.empresa, activo.cliente_texto, activo.sede = mov.empresa, mov.cliente_texto, mov.sede
    elif destino == Lugar.BODEGA:
        activo.empresa, activo.cliente_texto, activo.sede = None, "", None
    activo.modalidad = modalidad if destino != Lugar.BODEGA else ""
    # Préstamo o alquiler: la fecha en que debe volver viaja con el equipo (de la entrega al acta)
    if destino in (Lugar.TECNICO, Lugar.CLIENTE) and modalidad in (Modalidad.ALQUILER, Modalidad.PRESTAMO):
        activo.fecha_devolucion = mov.fecha_devolucion or activo.fecha_devolucion
    else:
        activo.fecha_devolucion = None
    activo.ticket_destino = mov.ticket if destino == Lugar.TECNICO else None
    activo.save()
    return mov


def mover_insumo(insumo, cantidad, tipo, origen, destino, usuario=None, validar=True, **kw):
    cantidad = Decimal(str(cantidad))
    if cantidad <= 0:
        raise ValidationError(f"{insumo.codigo}: la cantidad debe ser mayor que cero.")
    if validar and origen in (Lugar.BODEGA, Lugar.TECNICO):
        hay = disponible_en(insumo, origen, kw.get("tecnico"))
        if cantidad > hay:
            donde = "en bodega" if origen == Lugar.BODEGA else f"en poder de {kw.get('tecnico')}"
            raise ValidationError(f"{insumo.codigo}: solo hay {hay:g} {insumo.unidad.lower()} {donde} (pidió {cantidad:g}).")
    return MovimientoInsumo.objects.create(insumo=insumo, cantidad=cantidad, tipo=tipo, origen=origen, destino=destino,
                                           **_datos_comunes(kw, usuario))


@transaction.atomic
def entregar_a_tecnico(tecnico, activos, insumos, modalidad, usuario, empresa=None, sede=None, ticket=None,
                       observaciones="", actividad=None, solicitud=None, fecha_devolucion=None):
    """Bodega → técnico. La bodega define la modalidad (venta, alquiler, reinversión…) y para qué cliente es."""
    cliente = {"empresa": empresa or (ticket.empresa if ticket else None), "sede": sede or (ticket.sede if ticket else None),
               "ticket": ticket, "modalidad": modalidad, "observaciones": observaciones, "actividad": actividad,
               "solicitud": solicitud}
    devolucion = fecha_devolucion or (solicitud.fecha_devolucion if solicitud else None)
    for a in activos:
        if a.estado != E.DISPONIBLE:
            raise ValidationError(f"{a.placa} no está disponible en bodega ({a.get_estado_display()}).")
        mover_activo(a, T.ENTREGA, Lugar.TECNICO, usuario, tecnico=tecnico, fecha_devolucion=devolucion, **cliente)
    for insumo, cantidad in insumos:
        mover_insumo(insumo, cantidad, T.ENTREGA, Lugar.BODEGA, Lugar.TECNICO, usuario, tecnico=tecnico, **cliente)


@transaction.atomic
def devolver_a_bodega(tecnico, activos, insumos, usuario, observaciones="", ticket=None):
    """Lo que el técnico no usó vuelve a bodega (queda disponible). Con el ticket, cuadra el material de ese ticket."""
    for a in activos:
        if a.lugar != Lugar.TECNICO or a.tecnico_id != tecnico.pk:
            raise ValidationError(f"{a.placa} no está en poder de {tecnico}.")
        mover_activo(a, T.DEVOLUCION, Lugar.BODEGA, usuario, tecnico=tecnico, observaciones=observaciones, ticket=ticket)
    for insumo, cantidad in insumos:
        mover_insumo(insumo, cantidad, T.DEVOLUCION, Lugar.TECNICO, Lugar.BODEGA, usuario, tecnico=tecnico,
                     observaciones=observaciones, ticket=ticket)


def _modalidad_entregada(insumo, tecnico, ticket):
    entrega = (MovimientoInsumo.objects.filter(insumo=insumo, tecnico=tecnico, tipo=T.ENTREGA)
               .order_by("-fecha", "-creado"))
    return ((entrega.filter(ticket=ticket).first() or entrega.first()) or MovimientoInsumo(modalidad=Modalidad.MANTENIMIENTO)).modalidad


@transaction.atomic
def usar_en_acta(acta, activos, insumos, tecnico):
    """El técnico registra en el acta lo que instaló o gastó: sale de su poder y queda en el cliente."""
    ticket = acta.ticket  # puede no haber: acta de un trabajo no programado
    referencia = f"Acta N° {acta.numero}" + (f" · {ticket.radicado}" if ticket else "")
    for a in activos:
        if a.lugar != Lugar.TECNICO or a.tecnico_id != tecnico.pk:
            raise ValidationError(f"{a.placa} no está en su poder: pídale a bodega que se lo entregue primero.")
        mover_activo(a, T.INSTALACION, Lugar.CLIENTE, tecnico, tecnico=tecnico, empresa=acta.empresa, sede=acta.sede,
                     ticket=ticket, acta=acta, modalidad=a.modalidad or Modalidad.VENTA, fecha_devolucion=a.fecha_devolucion,
                     observaciones=referencia)
    for insumo, cantidad in insumos:
        mover_insumo(insumo, cantidad, T.INSTALACION, Lugar.TECNICO, Lugar.CLIENTE, tecnico, tecnico=tecnico,
                     empresa=acta.empresa, sede=acta.sede, ticket=ticket, acta=acta,
                     modalidad=_modalidad_entregada(insumo, tecnico, ticket),
                     observaciones=referencia)


@transaction.atomic
def retirar_en_acta(acta, tecnico):
    """Equipos de JANOTEC (alquiler) retirados del cliente: pasan a poder del técnico hasta que bodega los recibe con
    «Recibir devolución» y vuelven a estar disponibles. Si no se encuentran en el inventario, se avisa a bodega."""
    from tickets.models import EquipoActa

    ticket = acta.ticket
    referencia = f"Retirado del cliente · Acta N° {acta.numero}" + (f" · {ticket.radicado}" if ticket else "")
    sin_encontrar = []
    for e in acta.equipos.filter(clase=EquipoActa.Clase.RETIRADO, propiedad=EquipoActa.Propiedad.JANOTEC):
        activo = (Activo.objects.filter(placa__iexact=e.placa).first() if e.placa else None) or \
                 (Activo.objects.filter(serial__iexact=e.serial).first() if e.serial else None)
        if activo is None:
            sin_encontrar.append(e)
            continue
        if activo.lugar == Lugar.TECNICO:
            continue  # ya está en poder de un técnico
        mover_activo(activo, T.TRASLADO, Lugar.TECNICO, tecnico, estado=E.EN_TECNICO, tecnico=tecnico, empresa=acta.empresa,
                     sede=acta.sede, ticket=ticket, acta=acta, observaciones=referencia)
    if sin_encontrar:
        from django.conf import settings
        from django.urls import reverse

        from accounts.models import Usuario
        from tickets import notificaciones as notif
        destino = list(Usuario.objects.filter(is_active=True, rol="bodega").exclude(email="").values_list("email", flat=True))
        notif._enviar(f"Equipo de JANOTEC retirado sin placa en el inventario · Acta N° {acta.numero}",
                      "\n".join([f"{tecnico.get_full_name() or tecnico} retiró de {acta.empresa} equipos de JANOTEC que no "
                                  "se encontraron en el inventario por placa ni serial:",
                                  *[f"- {e.equipo} {e.marca} · serial {e.serial or '—'} · placa {e.placa or '—'}" for e in sin_encontrar],
                                  "", "Regístrelos en el inventario cuando lleguen a bodega.",
                                  f"{settings.SITIO_URL.rstrip('/')}{reverse('acta_detalle', args=[acta.numero])}"]),
                      destino or [settings.NOTIFICAR_SOPORTE_EMAIL])


def planeado_de(actividad):
    """Lo que el coordinador planeó en «Insumos y equipos» de la visita, listo para la entrega de bodega.

    Devuelve {"insumos": [(insumo, cantidad)], "placas": [placa], "sin_codigo": [texto], "faltan": [texto]}:
    los equipos se planean por referencia y aquí se sugieren placas disponibles concretas de esa referencia.
    """
    import re

    from cronograma.semanal import parse_insumos

    resultado = {"insumos": [], "placas": [], "sin_codigo": [], "faltan": []}
    usadas = set()
    saldos = saldos_insumos()
    for item in parse_insumos(actividad.insumos):
        m = re.search(r"\[([^\]]+)\]\s*$", item["descripcion"])
        codigo = m.group(1).strip().upper() if m else ""
        cantidad = Decimal(str(item["cantidad"]))
        insumo = Insumo.objects.filter(codigo=codigo).first() if codigo else None
        if insumo:
            hay = saldos[insumo.pk]["bodega"] if insumo.pk in saldos else CERO
            resultado["insumos"].append((insumo, cantidad))
            if cantidad > hay:
                resultado["faltan"].append(f"{insumo.codigo}: planeado {item['cantidad']:g}, en bodega {float(max(hay, CERO)):g}")
            continue
        placas = []
        if codigo:
            placas = list(Activo.objects.filter(estado=E.DISPONIBLE, referencia__iexact=codigo).exclude(placa__in=usadas)
                          .order_by("placa").values_list("placa", flat=True)[:int(cantidad)])
        if codigo and (placas or Activo.objects.filter(referencia__iexact=codigo).exists()):
            usadas.update(placas)
            resultado["placas"] += placas
            if len(placas) < cantidad:
                resultado["faltan"].append(f"{codigo}: planeado {item['cantidad']:g}, disponibles {len(placas)}")
        else:
            resultado["sin_codigo"].append(f"{item['cantidad']:g} {'m ' if item['unidad'] == 'm' else ''}{item['descripcion']}")
    return resultado


def entregas_de(actividades):
    """{id de actividad: fecha de la última entrega de bodega} para saber qué visitas ya tienen su material."""
    fechas = {}
    for modelo in (MovimientoActivo, MovimientoInsumo):
        for pk, fecha in modelo.objects.filter(actividad__in=actividades, tipo=T.ENTREGA).values_list("actividad_id", "fecha"):
            fechas[pk] = max(fecha, fechas.get(pk, fecha))
    return fechas


def vincular_usuario(usuario):
    """Cuando se crea el usuario de un técnico, se le asocia lo que el inventario anterior registraba a su nombre."""
    if usuario.rol in ("cliente",):
        return 0
    nombre = set(normalizar(usuario.get_full_name()).split())
    if len(nombre) < 2 and not usuario.documento:
        return 0
    from .models import AuditoriaHerramientas, EntregaHerramienta, Herramienta

    total = 0
    for modelo in (MovimientoActivo, MovimientoInsumo, Activo, Herramienta, EntregaHerramienta, AuditoriaHerramientas):
        con_cedula = any(f.name == "tecnico_documento" for f in modelo._meta.fields)
        pendientes = modelo.objects.filter(tecnico__isnull=True).exclude(tecnico_nombre="")
        ids = []
        for pk, texto, documento in pendientes.values_list(
                "pk", "tecnico_nombre", "tecnico_documento" if con_cedula else "tecnico_nombre"):
            if con_cedula and usuario.documento and documento == usuario.documento:
                ids.append(pk)
            elif len(nombre) >= 2 and set(normalizar(texto).split()) == nombre:
                ids.append(pk)
        total += modelo.objects.filter(pk__in=ids).update(tecnico=usuario)
    return total
