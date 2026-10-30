from collections import Counter, defaultdict
from decimal import Decimal
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from accounts.models import Usuario

from . import servicios
from .forms import (
    CambioEstadoForm, CompraActivoForm, CompraInsumoForm, DevolucionForm, EntregaForm, MaterialMixin, tecnicos,
)
from .models import (
    Activo, CategoriaActivo, CodigoActivo, Insumo, Lugar, Marca, Modalidad, MovimientoActivo, MovimientoInsumo,
    Proveedor,
)

T = MovimientoActivo.Tipo
E = Activo.Estado
FUERA_DE_SERVICIO = [E.BAJA, E.NO_ENCONTRADO]


def inventario_requerido(vista):
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not request.user.ve_inventario:
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envoltura


def funcionarios_requerido(vista):
    """Funcionarios: los ve quien usa el inventario y también coordinación (los documentos del personal sirven para
    las autorizaciones de ingreso), aunque coordinación no entre al resto del módulo."""
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not (request.user.ve_inventario or request.user.es_coordinador):
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envoltura


def gestion_requerida(vista):
    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        if not request.user.gestiona_inventario:
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envoltura


def _filas_insumos(insumos, saldos, costos):
    from .solicitudes import total_apartado

    apartado = total_apartado()
    filas = []
    for i in insumos:
        s = saldos.get(i.pk)
        bodega = s["bodega"] if s else Decimal(0)
        en_tecnicos = sum((v for v in s["tecnicos"].values() if v > 0), Decimal(0)) if s else Decimal(0)
        costo = costos.get(i.pk, Decimal(0))
        filas.append({"insumo": i, "bodega": bodega, "en_tecnicos": en_tecnicos, "costo": costo,
                      "apartado": apartado.get(i.codigo, Decimal(0)),
                      "valor": bodega * costo if bodega > 0 else Decimal(0),
                      "alerta": bodega < 0 or (i.stock_minimo and bodega <= i.stock_minimo)})
    return filas


@login_required
@gestion_requerida
def ordenes(request):
    """Órdenes de compra de bodega: las abiertas primero, con su fecha estimada de llegada."""
    from .models import OrdenCompra

    estado = request.GET.get("estado", "abiertas")
    qs = OrdenCompra.objects.select_related("proveedor", "solicitud", "solicitud__ticket").prefetch_related("lineas")
    if estado == "abiertas":
        qs = qs.filter(estado__in=["borrador", "enviada"])
    elif estado in OrdenCompra.Estado.values:
        qs = qs.filter(estado=estado)
    return render(request, "inventario/ordenes.html", {"ordenes": qs[:200], "estado": estado,
                                                        "estados": OrdenCompra.Estado.choices})


@login_required
@gestion_requerida
@require_POST
def orden_nueva(request):
    """Orden en preparación; desde una solicitud en compra llega con lo que falta."""
    from . import solicitudes as sol
    from .models import LineaOrdenCompra, OrdenCompra, SolicitudMaterial

    solicitud = None
    if str(request.POST.get("solicitud", "")).isdigit():
        solicitud = get_object_or_404(SolicitudMaterial, pk=request.POST["solicitud"])
    orden = OrdenCompra.objects.create(solicitud=solicitud, creado_por=request.user)
    if solicitud:
        costos = servicios.costo_promedio()
        for f in sol.faltantes(solicitud):
            item = f["item"]
            costo = costos.get(item.insumo_id, Decimal(0)) if item.insumo_id else Decimal(0)
            LineaOrdenCompra.objects.create(orden=orden, item=item, descripcion=item.descripcion[:300], cantidad=f["falta"],
                                            valor_unitario=(costo / Decimal("1.19")).quantize(Decimal("1")) if costo else 0)
        sol._nota(solicitud, request.user, f"bodega preparó la orden de compra {orden.numero}.")
    return redirect(orden)


@login_required
@gestion_requerida
def orden_detalle(request, pk):
    from .forms import OrdenCompraForm, formset_orden
    from .models import OrdenCompra

    orden = get_object_or_404(OrdenCompra.objects.select_related("proveedor", "solicitud", "solicitud__ticket"), pk=pk)
    editable = orden.estado in (OrdenCompra.Estado.BORRADOR, OrdenCompra.Estado.ENVIADA)
    form = OrdenCompraForm(request.POST or None, request.FILES or None, instance=orden)
    lineas = formset_orden()(request.POST or None, instance=orden, prefix="lineas")
    if request.method == "POST":
        accion = request.POST.get("accion", "guardar")
        if accion == "cancelar":  # lo que llega se recibe con la factura (orden_recibir)
            orden.estado = OrdenCompra.Estado.CANCELADA
            orden.save(update_fields=["estado", "actualizado"])
            from .cotizaciones import reabrir
            reabrir(orden)
            if orden.solicitud:
                from . import solicitudes as sol
                sol._nota(orden.solicitud, request.user, f"orden de compra {orden.numero}: cancelada.")
            messages.info(request, f"{orden.numero} cancelada.")
            return redirect(orden)
        if editable and form.is_valid() and lineas.is_valid():
            with transaction.atomic():
                form.save()
                lineas.save()
                from .cotizaciones import reabrir
                reabrir(orden)
                if accion == "enviar":
                    faltan = [t for t, v in (("el proveedor", orden.proveedor_id), ("la fecha estimada de llegada", orden.fecha_estimada),
                                             ("al menos un ítem", orden.lineas.exists())) if not v]
                    if faltan:
                        messages.error(request, "Para enviarla falta " + ", ".join(faltan) + ".")
                        return redirect(orden)
                    orden.estado = OrdenCompra.Estado.ENVIADA
                    orden.save(update_fields=["estado", "actualizado"])
                    if orden.solicitud:
                        from . import solicitudes as sol
                        sol._nota(orden.solicitud, request.user, f"orden de compra {orden.numero} enviada a {orden.proveedor}; "
                                                                 f"llega el {orden.fecha_estimada:%d/%m/%Y}.")
            messages.success(request, f"{orden.numero} enviada al proveedor: descargue el PDF y envíeselo." if accion == "enviar"
                             else f"{orden.numero} guardada.")
            return redirect(orden)
        messages.error(request, "Revise los datos de la orden.")
    return render(request, "inventario/orden.html", {"orden": orden, "form": form, "lineas": lineas, "editable": editable,
                                                     "totales": orden.totales()})


@login_required
@gestion_requerida
def orden_recibir(request, pk):
    """Llegó la factura: bodega confirma lo que vino y todo entra a bodega de una vez."""
    from . import recepcion
    from .models import OrdenCompra

    orden = get_object_or_404(OrdenCompra.objects.select_related("proveedor", "solicitud", "solicitud__ticket"), pk=pk)
    if orden.estado not in (OrdenCompra.Estado.BORRADOR, OrdenCompra.Estado.ENVIADA):
        messages.error(request, f"{orden.numero} está {orden.get_estado_display().lower()}: no hay nada por recibir.")
        return redirect(orden)
    inicial = {"proveedor": orden.proveedor.razon_social if orden.proveedor else "", "factura": orden.factura,
               "fecha": orden.fecha_factura or servicios.timezone.localdate()}
    cabecera = recepcion.RecepcionForm(request.POST or None, request.FILES or None, initial=inicial)
    filas = recepcion.FilasRecepcion(request.POST or None, initial=None if request.POST else recepcion.filas_iniciales(orden),
                                     prefix="filas")
    if request.method == "POST":
        if cabecera.is_valid() and filas.is_valid():
            datos = [f.cleaned_data for f in filas.forms if f.cleaned_data and not f.vacia]
            if not any(d.get("clase") != "no" and d.get("cantidad") for d in datos):
                messages.error(request, "No marcó nada como recibido.")
            else:
                insumos, placas = recepcion.recibir(orden, cabecera.cleaned_data, datos, request.user,
                                                    soporte=cabecera.cleaned_data.get("soporte"))
                _liberar_compras(request)
                partes = ([f"insumos: {', '.join(insumos)}"] if insumos else []) + ([f"equipos: {', '.join(placas)}"] if placas else [])
                messages.success(request, f"Factura {cabecera.cleaned_data['factura']} recibida. Entraron a bodega " + "; ".join(partes)
                                 + (". Imprima sus etiquetas desde la orden y márquelos." if placas else "."))
                if orden.por_recibir:
                    messages.info(request, f"{orden.numero} sigue abierta: aún falta que llegue parte de lo pedido.")
                return redirect(orden)
        else:
            messages.error(request, "Revise las filas marcadas.")
    return render(request, "inventario/orden_recibir.html", {
        "orden": orden, "cabecera": cabecera, "filas": filas,
        "proveedores": Proveedor.objects.values_list("razon_social", flat=True),
        "insumos": Insumo.objects.filter(activo=True), "codigos": CodigoActivo.objects.all(),
    })


@login_required
@gestion_requerida
def orden_archivo(request, pk, cual):
    """El PDF de la orden para el proveedor, o el soporte (cotización/factura) que subió bodega."""
    from django.http import FileResponse, HttpResponse

    from .models import OrdenCompra
    from .pdf import orden_compra_pdf

    orden = get_object_or_404(OrdenCompra, pk=pk)
    if cual == "pdf":
        nombre, contenido = orden_compra_pdf(orden)
        respuesta = HttpResponse(contenido, content_type="application/pdf")
        respuesta["Content-Disposition"] = f'inline; filename="{nombre}"'
        return respuesta
    if not orden.soporte:
        raise Http404
    return FileResponse(orden.soporte.open("rb"), as_attachment=False, filename=orden.soporte.name.rsplit("/", 1)[-1])


@login_required
@gestion_requerida
def stock_minimo(request):
    """Bodega fija el mínimo de cada insumo; los lunes le llega la lista de lo que quedó en el mínimo o por debajo."""
    from . import alertas
    from .solicitudes import total_apartado

    insumos = list(Insumo.objects.filter(activo=True).order_by("descripcion"))
    if request.method == "POST":
        cambiados = 0
        for i in insumos:
            texto = request.POST.get(f"minimo_{i.pk}", "").replace(",", ".").strip()
            try:
                valor = max(Decimal(texto or "0"), Decimal(0))
            except Exception:  # noqa: BLE001
                continue
            if valor != i.stock_minimo:
                i.stock_minimo = valor
                i.save(update_fields=["stock_minimo"])
                cambiados += 1
        messages.success(request, f"Stock mínimo actualizado en {cambiados} insumo(s).")
        if request.POST.get("enviar_ya"):
            n = alertas.enviar(forzar=True)
            messages.info(request, f"Se envió la alerta con {n} insumo(s) por reponer." if n else "Ningún insumo está en el mínimo.")
        return redirect("inventario:stock_minimo")
    saldos = servicios.saldos_insumos()
    apartado = total_apartado()
    filas = []
    for i in insumos:
        bodega = saldos[i.pk]["bodega"] if i.pk in saldos else Decimal(0)
        libre = bodega - apartado.get(i.codigo, Decimal(0))
        filas.append({"insumo": i, "bodega": bodega, "libre": libre, "apartado": apartado.get(i.codigo, Decimal(0)),
                      "bajo": i.stock_minimo > 0 and libre <= i.stock_minimo})
    filas.sort(key=lambda f: (not f["bajo"], f["insumo"].stock_minimo <= 0, f["insumo"].descripcion.lower()))
    return render(request, "inventario/stock_minimo.html", {"filas": filas, "bajos": sum(f["bajo"] for f in filas)})


@login_required
@inventario_requerido
def panel(request):
    ver = request.GET.get("ver", "activos")
    q = request.GET.get("q", "").strip()
    activos = Activo.objects.select_related("tecnico", "empresa", "sede")
    if q:
        activos = activos.filter(Q(placa__icontains=q) | Q(serial__icontains=q) | Q(referencia__icontains=q)
                                 | Q(descripcion__icontains=q) | Q(tipo__icontains=q) | Q(cliente_texto__icontains=q)
                                 | Q(empresa__razon_social__icontains=q) | Q(tecnico_nombre__icontains=q))
    estado = request.GET.get("estado", "")
    if estado:
        activos = activos.filter(estado=estado)
    elif not q:  # los dados de baja y los no encontrados van aparte (se ven eligiendo ese estado o buscándolos)
        activos = activos.exclude(estado__in=FUERA_DE_SERVICIO)
    categoria = request.GET.get("categoria", "")
    if categoria:
        activos = activos.filter(categoria=categoria)

    conteo = Counter(Activo.objects.values_list("estado", flat=True))
    disponibles = Activo.objects.filter(estado=E.DISPONIBLE)
    insumos = Insumo.objects.filter(activo=request.GET.get("inactivos") != "1")
    if q and ver == "insumos":
        insumos = insumos.filter(Q(codigo__icontains=q) | Q(descripcion__icontains=q))
    saldos, costos = servicios.saldos_insumos(), servicios.costo_promedio()
    filas = _filas_insumos(insumos, saldos, costos)
    if ver == "insumos" and request.GET.get("stock") == "1":
        filas = [f for f in filas if f["bodega"] > 0 or f["en_tecnicos"] > 0]

    # Lo que tiene cada técnico (activos e insumos)
    por_tecnico = defaultdict(lambda: {"activos": 0, "insumos": 0, "usuario": None})
    for a in Activo.objects.filter(lugar=Lugar.TECNICO).select_related("tecnico"):
        clave = a.tecnico or a.tecnico_nombre
        por_tecnico[str(clave)]["activos"] += 1
        por_tecnico[str(clave)]["usuario"] = a.tecnico
    usuarios = {u.pk: u for u in Usuario.objects.filter(pk__in=[k for s in saldos.values() for k in s["tecnicos"] if isinstance(k, int)])}
    for s in saldos.values():
        for clave, cantidad in s["tecnicos"].items():
            if cantidad > 0:
                persona = usuarios.get(clave, clave)
                por_tecnico[str(persona)]["insumos"] += 1
                if isinstance(persona, Usuario):
                    por_tecnico[str(persona)]["usuario"] = persona

    # Visitas de las próximas dos semanas con material planeado en el cronograma
    from cronograma.models import Actividad
    hoy = servicios.timezone.localdate()
    proximas = list(Actividad.objects.filter(fecha__range=(hoy, hoy + servicios.timedelta(days=14))).exclude(insumos="")
                    .select_related("ticket").prefetch_related("encargados", "acompanantes").order_by("fecha", "hora_inicio"))
    entregadas = servicios.entregas_de(proximas)
    for a in proximas:
        a.entregado = entregadas.get(a.pk)

    contexto = {
        "ver": ver, "q": q, "estado": estado, "categoria": categoria,
        "fuera_de_servicio": sum(conteo.get(v, 0) for v in FUERA_DE_SERVICIO),
        "inactivos": Insumo.objects.filter(activo=False).count(),
        "proximas": proximas, "por_entregar": sum(1 for a in proximas if not a.entregado),
        "activos": activos.order_by("placa") if ver == "activos" else [],
        "filas": filas, "estados": [(v, n, conteo.get(v, 0)) for v, n in E.choices], "conteo": dict(conteo),
        "categorias": sorted(set(Activo.objects.exclude(categoria="").values_list("categoria", flat=True))),
        "kpi": {
            "disponibles": conteo.get(E.DISPONIBLE, 0), "en_tecnicos": conteo.get(E.EN_TECNICO, 0) + conteo.get(E.ASIGNADO, 0),
            "en_clientes": conteo.get(E.ALQUILADO, 0) + conteo.get(E.VENDIDO, 0),
            "valor_activos": sum((a.valor_con_iva or 0 for a in disponibles), Decimal(0)),
            "insumos_con_stock": sum(1 for f in filas if f["bodega"] > 0),
            "valor_insumos": sum((f["valor"] for f in filas), Decimal(0)),
            "alertas": sum(1 for f in filas if f["alerta"]),
            "prestamos_vencidos": sum(a.vencido for a in Activo.objects.filter(
                estado__in=[E.ALQUILADO, E.PRESTADO], lugar=Lugar.CLIENTE, fecha_devolucion__isnull=False)),
        },
        "tecnicos": sorted(por_tecnico.items()),
        "gestiona": request.user.gestiona_inventario,
    }
    return render(request, "inventario/panel.html", contexto)


@login_required
@inventario_requerido
def activo_detalle(request, placa):
    activo = get_object_or_404(Activo.objects.select_related("tecnico", "empresa", "sede", "ticket_destino"), placa=placa)
    movimientos = activo.movimientos.select_related("tecnico", "empresa", "sede", "ticket", "acta").order_by("-fecha", "-creado")
    form = CambioEstadoForm(initial={"fecha": servicios.timezone.localdate()}) if request.user.gestiona_inventario else None
    return render(request, "inventario/activo.html", {"activo": activo, "movimientos": movimientos, "form": form})


@login_required
@gestion_requerida
@require_POST
def cambiar_estado(request, placa):
    activo = get_object_or_404(Activo, placa=placa)
    form = CambioEstadoForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Indique qué pasó, la fecha y el motivo.")
        return redirect(activo)
    d = form.cleaned_data
    if d["accion"] == "garantia":  # la garantía es un caso con el proveedor: se sigue hasta que se resuelve
        from . import garantias as gar
        try:
            g = gar.abrir(activo, d["observaciones"], d["fecha"], request.user)
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
            return redirect(activo)
        messages.success(request, f"{activo.placa} quedó en garantía con el proveedor ({g.numero}).")
        return redirect("inventario:garantia", g.pk)
    destino, tipo, estado = {
        "devolucion": (Lugar.BODEGA, T.DEVOLUCION, E.DISPONIBLE),
        "mantenimiento": (Lugar.BODEGA, T.MANTENIMIENTO, E.MANTENIMIENTO),
        "garantia": (Lugar.PROVEEDOR, T.GARANTIA, E.GARANTIA),
        "baja": (Lugar.FUERA, T.BAJA, E.BAJA),
        "no_encontrado": (Lugar.FUERA, T.AJUSTE, E.NO_ENCONTRADO),
    }[d["accion"]]
    servicios.mover_activo(activo, tipo, destino, request.user, estado=estado, fecha=d["fecha"],
                           observaciones=d["observaciones"])
    messages.success(request, f"{activo.placa}: {activo.get_estado_display()}.")
    return redirect(activo)


@login_required
@inventario_requerido
def insumo_detalle(request, codigo):
    insumo = get_object_or_404(Insumo, codigo=codigo)
    if request.method == "POST" and request.user.gestiona_inventario:  # ya no se usa: sale de las listas, no se borra
        insumo.activo = not insumo.activo
        insumo.save(update_fields=["activo"])
        messages.success(request, f"{insumo.codigo} {'vuelve a las listas' if insumo.activo else 'quedó inactivo: ya no sale en las listas'}.")
        return redirect(insumo)
    movimientos = list(insumo.movimientos.select_related("tecnico", "empresa", "ticket", "acta").order_by("fecha", "creado"))
    saldo = Decimal(0)
    for m in movimientos:  # kardex: saldo de bodega después de cada movimiento
        saldo += (m.cantidad if m.destino == Lugar.BODEGA else 0) - (m.cantidad if m.origen == Lugar.BODEGA else 0)
        m.saldo_bodega = saldo
    s = servicios.saldos_insumos([insumo]).get(insumo.pk)
    usuarios = {u.pk: u for u in Usuario.objects.filter(pk__in=[k for k in (s["tecnicos"] if s else {}) if isinstance(k, int)])}
    en_tecnicos = [(usuarios.get(k, k), v) for k, v in (s["tecnicos"].items() if s else []) if v > 0]
    from .proveedores import de_insumo
    return render(request, "inventario/insumo.html", {
        "insumo": insumo, "movimientos": reversed(movimientos), "bodega": saldo, "en_tecnicos": en_tecnicos,
        "vendedores": de_insumo(insumo),
        "costo": servicios.costo_promedio([insumo]).get(insumo.pk, Decimal(0)),
    })


def _listas_material():
    disponibles = Activo.objects.filter(estado=E.DISPONIBLE).order_by("placa")
    saldos = servicios.saldos_insumos()
    insumos = [(i, saldos[i.pk]["bodega"]) for i in Insumo.objects.filter(activo=True) if i.pk in saldos and saldos[i.pk]["bodega"] > 0]
    return {"disponibles": disponibles, "insumos_bodega": insumos}


@login_required
@gestion_requerida
def entrega(request):
    """Bodega entrega material al técnico.

    Para un cliente solo sale con una solicitud de material aprobada (?solicitud=), que llega precargada con lo
    aprobado y su holgura. Las herramientas del personal (modalidad «Uso del personal») se entregan directo.
    """
    from . import solicitudes as sol
    from .models import SolicitudMaterial

    id_actividad = request.GET.get("actividad")
    if id_actividad and id_actividad.isdigit():  # desde la visita del cronograma: su solicitud
        s = SolicitudMaterial.objects.filter(actividad_id=id_actividad).exclude(estado=SolicitudMaterial.Estado.CANCELADA).first()
        if s:
            return redirect(s if s.estado != SolicitudMaterial.Estado.APROBADA else f"{reverse('inventario:entrega')}?solicitud={s.pk}")
        messages.warning(request, "Esa visita no tiene material planeado: el coordinador lo pide en «Insumos y equipos».")
        return redirect("inventario:panel")
    solicitud = None
    id_solicitud = request.POST.get("solicitud") or request.GET.get("solicitud")
    if id_solicitud and str(id_solicitud).isdigit():
        solicitud = get_object_or_404(SolicitudMaterial.objects.select_related("ticket", "tecnico"), pk=id_solicitud)
    inicial = {"tecnico": request.GET.get("tecnico"), "modalidad": request.GET.get("modalidad")}
    pendiente = None
    if solicitud:
        pendiente = sol.por_entregar(solicitud)
        inicial.update(tecnico=solicitud.tecnico_id, ticket=solicitud.ticket_id, modalidad=solicitud.modalidad,
                       observaciones=f"{solicitud.numero} · {solicitud.nota}"[:500])
    form = EntregaForm(request.POST or None, initial=inicial)
    errores = []
    if solicitud and solicitud.estado != SolicitudMaterial.Estado.APROBADA:
        errores.append(f"La solicitud {solicitud.numero} está «{solicitud.get_estado_display()}»: solo se entrega aprobada.")
    if request.method == "POST" and form.is_valid() and not errores:
        d = form.cleaned_data
        activos, insumos, errores = MaterialMixin().leer_material(
            request.POST, placas_validas=set(Activo.objects.filter(estado=E.DISPONIBLE).values_list("pk", flat=True)))
        a_mano = []
        if solicitud:
            elegidos = set(request.POST.getlist("a_mano"))
            a_mano = [i for i in sol.por_entregar(solicitud)["a_mano"] if str(i.pk) in elegidos]
        if not activos and not insumos and not a_mano and not errores:
            errores.append("Agregue al menos una placa o un insumo.")
        if solicitud and not d.get("firma_entrega"):
            errores.append("Falta la firma de quien entrega (bodega).")
        if d["modalidad"] != Modalidad.ASIGNACION:
            if solicitud is None:
                errores.append("El material para clientes solo sale con una solicitud de material aprobada (firma de "
                               "bodega y autorización del coordinador TIC o dirección). Las herramientas del personal se entregan con «Uso del personal».")
            elif not errores:
                errores += sol.permitido_entregar(solicitud, activos, insumos)
        elif insumos and solicitud is None:
            errores.append("Con «Uso del personal» solo salen herramientas (activos). Los insumos necesitan una solicitud "
                           "de material aprobada.")
        if not errores:
            try:
                with transaction.atomic():
                    if activos or insumos:
                        servicios.entregar_a_tecnico(d["tecnico"], activos, insumos, d["modalidad"], request.user,
                                                     empresa=d.get("empresa"), ticket=d.get("ticket"), observaciones=d["observaciones"],
                                                     actividad=solicitud.actividad if solicitud else None, solicitud=solicitud)
                    acta = sol.registrar_acta_entrega(solicitud, request.user, d["tecnico"], activos, insumos, a_mano,
                                                      firma_entrega=d.get("firma_entrega"),
                                                      firma_recibe=d.get("firma_recibe")) if solicitud else None
            except ValidationError as e:
                errores += e.messages
            else:
                completa = sol.marcar_entregada(solicitud) if solicitud else False
                if acta and acta.recibido_en:  # firmó en bodega: si ya salió todo, la instalación sigue sola
                    sol._a_instalacion_si_listo(acta, request.user)
                messages.success(request, f"Entregado a {d['tecnico']}: {len(activos)} activo(s), {len(insumos)} insumo(s)"
                                          + (f" y {len(a_mano)} escrito(s) a mano" if a_mano else "") + ". "
                                          + (f"Quedó el acta de entrega {acta.numero} firmada por los dos. " if acta and acta.recibido_en
                                             else f"Quedó el acta de entrega {acta.numero}; falta que el técnico firme que la recibió. " if acta else "")
                                          + "Él lo descuenta en el acta de servicio y devuelve lo que sobre."
                                 + (f" {solicitud.numero} quedó entregada." if completa else ""))
                return redirect(solicitud or reverse("inventario:tecnico", args=[d["tecnico"].pk]))
    if request.method == "POST" or not pendiente:
        placas = request.POST.getlist("placa")
        insumos_filas = list(zip(request.POST.getlist("insumo"), request.POST.getlist("cantidad")))
    else:
        placas = pendiente["placas"]
        insumos_filas = [(f"{i.codigo} · {i.descripcion}", f"{c.normalize():f}") for i, c in pendiente["insumos"]]
    return render(request, "inventario/entrega.html", {
        "form": form, "errores": errores, **_listas_material(), "placas": placas, "insumos_post": insumos_filas,
        "solicitud": solicitud, "pendiente": pendiente,
    })


@login_required
@gestion_requerida
def devolucion(request, pk):
    """El técnico devuelve a bodega lo que no usó."""
    from tickets.models import Ticket

    from . import solicitudes as sol

    tecnico = get_object_or_404(Usuario, pk=pk)
    activos, insumos = servicios.en_poder_de(tecnico)
    # ¿De qué ticket sobró? Así el material de ese ticket queda cuadrado (entregado = instalado + devuelto)
    con_pendiente = []
    ids = {pk for modelo in (MovimientoInsumo, MovimientoActivo)
           for pk in modelo.objects.filter(tecnico=tecnico, tipo="entrega", ticket__isnull=False).values_list("ticket_id", flat=True)}
    for t in Ticket.objects.filter(pk__in=ids).order_by("-creado")[:30]:
        c = sol.cuadre(t)
        if any(f["tecnico"] == tecnico for f in c["pendientes"]):
            con_pendiente.append((t, [f for f in c["pendientes"] if f["tecnico"] == tecnico]))
    id_ticket = request.POST.get("ticket") or request.GET.get("ticket") or ""
    ticket = next((t for t, _ in con_pendiente if str(t.pk) == id_ticket), None)
    sugerido = {f["codigo"]: f["pendiente"] for f in dict(con_pendiente).get(ticket, [])} if ticket else {}
    form = DevolucionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        devolver_activos = [a for a in activos if str(a.pk) in request.POST.getlist("activo")]
        devolver_insumos = []
        for insumo, tiene in insumos:
            try:
                cantidad = Decimal(request.POST.get(f"cantidad_{insumo.pk}", "0").replace(",", ".") or 0)
            except Exception:  # noqa: BLE001
                cantidad = Decimal(0)
            if cantidad > 0:
                devolver_insumos.append((insumo, cantidad))
        if not devolver_activos and not devolver_insumos:
            messages.error(request, "Marque lo que devuelve.")
        else:
            try:
                servicios.devolver_a_bodega(tecnico, devolver_activos, devolver_insumos, request.user,
                                            form.cleaned_data["observaciones"], ticket=ticket)
            except ValidationError as e:
                messages.error(request, " ".join(e.messages))
            else:
                messages.success(request, f"Devolución registrada: {len(devolver_activos)} activo(s) y {len(devolver_insumos)} insumo(s) volvieron a bodega."
                                 + (f" Quedó cuadrada con {ticket.radicado}." if ticket else ""))
                return redirect("inventario:tecnico", pk=tecnico.pk)
    return render(request, "inventario/devolucion.html", {"tecnico": tecnico, "activos": activos, "insumos": insumos, "form": form,
                                                          "con_pendiente": con_pendiente, "ticket": ticket, "sugerido": sugerido})


@login_required
@inventario_requerido
def tecnico(request, pk):
    persona = get_object_or_404(Usuario, pk=pk)
    if not request.user.gestiona_inventario and persona != request.user:
        raise PermissionDenied
    activos, insumos = servicios.en_poder_de(persona)
    recientes = (MovimientoActivo.objects.filter(tecnico=persona).select_related("activo", "empresa", "ticket")[:15])
    recientes_insumos = MovimientoInsumo.objects.filter(tecnico=persona).select_related("insumo", "empresa", "ticket")[:15]
    from .models import AuditoriaHerramientas, Herramienta
    return render(request, "inventario/tecnico.html", {
        "persona": persona, "activos": activos, "insumos": insumos, "recientes": recientes, "recientes_insumos": recientes_insumos,
        "herramientas": Herramienta.objects.filter(tecnico=persona).exclude(estado=Herramienta.Estado.BAJA),
        "ultima_auditoria": AuditoriaHerramientas.objects.filter(tecnico=persona).first(),
    })


@login_required
@inventario_requerido
def mi_material(request):
    return tecnico(request, request.user.pk)


@login_required
@gestion_requerida
def compra(request):
    """Ingreso a bodega: activos con placa (uno o varios iguales) o insumos por cantidad."""
    tipo = request.POST.get("tipo") or request.GET.get("tipo", "activo")
    form_activo = CompraActivoForm(request.POST if tipo == "activo" else None, prefix="a",
                                   initial={"iva": 19, "fecha_compra": servicios.timezone.localdate()})
    form_insumo = CompraInsumoForm(request.POST if tipo == "insumo" else None, prefix="i",
                                   initial={"iva": 19, "fecha": servicios.timezone.localdate()})
    if request.method == "POST" and tipo == "activo" and form_activo.is_valid():
        d = form_activo.cleaned_data
        creados = []
        # Sin factura no es una compra: entra como ajuste de inventario (no cuenta en el listado de compras ni en costos)
        sin_factura = d.get("sin_factura")
        nota = form_activo.nota_sin_factura() if sin_factura else ""
        with transaction.atomic():
            for n in range(d["cantidad_iguales"]):
                activo = form_activo.save(commit=False)
                activo.pk = None
                activo.placa = Activo.siguiente_placa(d["prefijo"])
                activo.serial = d["lista_seriales"][n] if d["lista_seriales"] else ""
                activo.responsable_compra = request.user.get_full_name() or request.user.username
                if sin_factura:
                    activo.observaciones = " · ".join(x for x in (nota, activo.observaciones) if x)[:500]
                activo.save()
                fecha = activo.fecha_compra or servicios.timezone.localdate()
                MovimientoActivo.objects.create(
                    activo=activo, tipo=T.AJUSTE if sin_factura else T.COMPRA,
                    origen=Lugar.FUERA if sin_factura else Lugar.PROVEEDOR, destino=Lugar.BODEGA, estado=E.DISPONIBLE,
                    fecha=fecha, proveedor=activo.proveedor, factura=activo.factura,
                    observaciones=activo.observaciones, registrado_por=request.user,
                    registrado_por_nombre=activo.responsable_compra)
                activo.fecha_ultimo_movimiento = fecha
                activo.save(update_fields=["fecha_ultimo_movimiento"])
                creados.append(activo.placa)
            if d.get("proveedor"):
                Proveedor.objects.get_or_create(razon_social=d["proveedor"].strip())
            if d.get("referencia"):  # stock mínimo del modelo: el que escribió, o 1 si el modelo es nuevo
                from .models import MinimoModelo
                if d.get("stock_minimo") is not None:
                    MinimoModelo.objects.update_or_create(referencia=d["referencia"].strip(), defaults={"minimo": int(d["stock_minimo"])})
        _liberar_compras(request)
        messages.success(request, f"Ingresaron a bodega{' (sin factura)' if sin_factura else ''}: {', '.join(creados)}. Imprima las etiquetas y márquelos.")
        return redirect(f"{reverse('inventario:etiquetas')}?placas={','.join(creados)}")
    if request.method == "POST" and tipo == "insumo" and form_insumo.is_valid():
        d = form_insumo.cleaned_data
        with transaction.atomic():
            insumo, nuevo = Insumo.objects.get_or_create(
                codigo=d["codigo"], defaults={"descripcion": d["descripcion"], "unidad": d["unidad"] or "Unidad"})
            if d.get("stock_minimo") is not None and d["stock_minimo"] != insumo.stock_minimo:
                insumo.stock_minimo = d["stock_minimo"]
                insumo.save(update_fields=["stock_minimo"])
            sin_factura = d.get("sin_factura")
            servicios.mover_insumo(insumo, d["cantidad"], T.AJUSTE if sin_factura else T.COMPRA,
                                   Lugar.FUERA if sin_factura else Lugar.PROVEEDOR, Lugar.BODEGA, request.user,
                                   fecha=d["fecha"] or servicios.timezone.localdate(), proveedor=(d["proveedor"] or "").strip(),
                                   factura=d["factura"] or "", valor_unitario=d["valor_unitario"], iva=d["iva"],
                                   observaciones=form_insumo.nota_sin_factura() if sin_factura else "")
            if d.get("proveedor"):
                Proveedor.objects.get_or_create(razon_social=d["proveedor"].strip())
        _liberar_compras(request)
        messages.success(request, f"Ingresaron a bodega {d['cantidad']:g} {insumo.unidad.lower()} de {insumo}."
                                  + (" (insumo nuevo en el catálogo)" if nuevo else ""))
        return redirect(insumo)
    activos = Activo.objects.all()
    return render(request, "inventario/compra.html", {
        "tipo": tipo, "form_activo": form_activo, "form_insumo": form_insumo,
        "proveedores": Proveedor.objects.values_list("razon_social", flat=True),
        "codigos": CodigoActivo.objects.all(), "insumos": Insumo.objects.filter(activo=True),
        "categorias": CategoriaActivo.objects.values_list("nombre", flat=True),
        "tipos": sorted(set(activos.exclude(tipo="").values_list("tipo", flat=True))),
        "subtipos": sorted(set(activos.exclude(subtipo="").values_list("subtipo", flat=True))),
        "marcas": Marca.objects.values_list("nombre", flat=True),
    })


def _liberar_compras(request):
    from .solicitudes import revisar_compras
    for s in revisar_compras(request.user):
        messages.info(request, f"Con esta compra, la solicitud {s.numero} ({s.ticket.radicado}) ya tiene todo: pasa a firmas.")


@login_required
@gestion_requerida
def siguiente_placa(request):
    prefijo = Activo.prefijo(request.GET.get("prefijo", "").strip().upper().replace(" ", ""))
    codigo = CodigoActivo.objects.filter(codigo=prefijo.rstrip("-")).first() or CodigoActivo.objects.filter(codigo=prefijo).first()
    ultimo = Activo.objects.filter(placa__startswith=prefijo).order_by("-placa").first() if prefijo else None
    return JsonResponse({"placa": Activo.siguiente_placa(prefijo) if prefijo else "",
                         "descripcion": codigo.descripcion if codigo else "",
                         "plantilla": {c: getattr(ultimo, c) for c in ("categoria", "tipo", "subtipo", "marca", "referencia",
                                                                        "descripcion")} if ultimo else {}})


# --- Solicitudes de material: validación, compra, propuesta y firmas ------------------------
def _puede_ver_solicitud(usuario, s):
    return (usuario.gestiona_inventario or usuario.es_coordinador or s.solicitado_por_id == usuario.pk
            or (usuario.ve_comercial and s.origen == s.Origen.COMERCIAL) or (s.tecnico_id == usuario.pk))


def _puede_editar_materiales(usuario, s):
    return s.estado == s.Estado.BORRADOR and (usuario.gestiona_inventario or usuario.ve_comercial
                                               or s.solicitado_por_id == usuario.pk)


@login_required
def solicitud_detalle(request, pk):
    from . import solicitudes as sol
    from .forms import ValidarSolicitudForm
    from .models import SolicitudMaterial

    s = get_object_or_404(SolicitudMaterial.objects.select_related("ticket", "ticket__empresa", "tecnico", "solicitado_por",
                                                                   "actividad"), pk=pk)
    if not _puede_ver_solicitud(request.user, s):
        raise PermissionDenied
    puede_validar = request.user.gestiona_inventario and s.estado in (s.Estado.VALIDACION, s.Estado.COMPRA)
    contexto = {
        "s": s, "filas": sol.existencias(s), "firmas": s.firmas.select_related("usuario").order_by("-ronda", "creado"),
        "casillas": sol.casillas(s),
        "puede_firmar": sol.roles_que_puede_firmar(s, request.user),
        "puede_editar": _puede_editar_materiales(request.user, s),
        "form_validar": ValidarSolicitudForm(solicitud=s) if puede_validar else None,
        "texto": sol.texto_materiales(s), "catalogo_inventario": servicios.catalogo_planeacion(),
        "movimientos": list(MovimientoActivo.objects.filter(solicitud=s).select_related("activo"))
        + list(MovimientoInsumo.objects.filter(solicitud=s).select_related("insumo")),
        "roles": dict(s.firmas.model.Rol.choices),
        "seguimiento": sol.seguimiento_ticket(s.ticket, list(s.ticket.solicitudes_material.exclude(estado="cancelada")
                                                             .prefetch_related("actas_entrega"))),
    }
    return render(request, "inventario/solicitud.html", contexto)


@login_required
@require_POST
def solicitud_accion(request, pk, accion):
    from . import solicitudes as sol
    from .forms import ValidarSolicitudForm
    from .models import SolicitudMaterial

    s = get_object_or_404(SolicitudMaterial, pk=pk)
    if not _puede_ver_solicitud(request.user, s):
        raise PermissionDenied
    try:
        if accion == "enviar":
            if not _puede_editar_materiales(request.user, s):
                raise PermissionDenied
            sol.enviar_a_bodega(s, request.user, request.POST.get("materiales", ""))
            messages.success(request, f"{s.numero} se envió a bodega para validar existencias.")
        elif accion == "validar":
            if not request.user.gestiona_inventario:
                raise PermissionDenied
            form = ValidarSolicitudForm(request.POST, solicitud=s)
            if not form.is_valid():
                raise ValidationError([e for lista in form.errors.values() for e in lista])
            d = form.cleaned_data
            sol.validar(s, request.user, cargo=d["cargo"], modalidad=d["modalidad"], tecnico=d["tecnico"],
                        adicionales=form.adicionales(), comentario=d["comentario"], fecha_devolucion=d["fecha_devolucion"],
                        a_mano_listos=form.a_mano_listos())
            s.refresh_from_db()
            messages.success(request, f"{s.numero}: {s.get_estado_display()}.")
        elif accion == "firmar":
            from tickets.forms import leer_firma
            aprobada = request.POST.get("decision") == "aprobar"
            sol.firmar(s, request.user, aprobada, request.POST.get("comentario", "").strip(), rol=request.POST.get("rol") or None,
                       firma=leer_firma(request.POST.get("firma"), False, "de aprobación") if aprobada else None)
            s.refresh_from_db()
            messages.success(request, f"Firma registrada. {s.numero}: {s.get_estado_display()}." if aprobada
                             else f"{s.numero} se devolvió para corregirla.")
        elif accion == "adicionales":
            # Lo adicional para poder instalar lo agregan bodega y coordinación
            if not (request.user.gestiona_inventario or request.user.es_coordinador):
                raise PermissionDenied
            n = sol.agregar_adicionales(s, request.user, request.POST.get("adicionales", ""))
            messages.success(request, f"Se agregaron {n} adicional(es) a la lista de {s.numero}.")
        elif accion == "recibido":
            from tickets.forms import leer_firma
            acta = get_object_or_404(s.actas_entrega, pk=request.POST.get("acta"))
            sol.confirmar_recibido(acta, request.user, firma=leer_firma(request.POST.get("firma"), False, "de quien recibe"))
            messages.success(request, f"Firmó que recibió lo del acta de entrega {acta.numero}.")
        elif accion == "cancelar":
            if not (request.user.gestiona_inventario or s.solicitado_por_id == request.user.pk):
                raise PermissionDenied
            motivo = request.POST.get("motivo", "").strip()
            if len(motivo) < 10:
                raise ValidationError("Explique por qué se cancela.")
            sol.cancelar(s, request.user, motivo)
            messages.info(request, f"{s.numero} quedó cancelada.")
        else:
            raise Http404
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    volver = request.POST.get("volver", "")
    if volver.startswith("/") and not volver.startswith("//"):  # desde la lista de alistamiento del ticket
        return redirect(volver)
    return redirect(s)


@login_required
@inventario_requerido
def bandeja(request):
    """Tickets en bodega, como la bandeja de técnica o de comercial: en qué paso va cada uno y qué sigue."""
    from . import solicitudes as sol

    ver = request.GET.get("ver", "abiertos")
    paso = request.GET.get("paso", "")
    q = request.GET.get("q", "").strip().lower()
    filas = sol.bandeja("terminados" if ver == "terminados" else "abiertos")
    conteo = Counter(f["paso"] for f in filas)
    if paso:
        filas = [f for f in filas if f["paso"] == paso]
    if q:
        filas = [f for f in filas if q in f"{f['ticket'].radicado} {f['ticket'].empresa} {f['ticket'].asunto}".lower()]
    return render(request, "inventario/bandeja.html", {
        "filas": filas, "ver": ver, "paso": paso, "q": request.GET.get("q", ""),
        "pasos": [(c, n, conteo.get(c, 0)) for c, n, _, _ in sol.PASOS_BODEGA if c != "listo" or ver == "terminados"],
        "propuesta": conteo.get("propuesta", 0), "atrasados": sum(1 for f in filas if f["atrasado"]),
    })


@login_required
@inventario_requerido
def solicitudes(request):
    from . import solicitudes as sol
    from .models import SolicitudMaterial

    estado = request.GET.get("estado", "abiertas")
    qs = SolicitudMaterial.objects.select_related("ticket", "ticket__empresa", "tecnico").prefetch_related("items", "firmas")
    if estado == "abiertas":
        qs = qs.filter(estado__in=SolicitudMaterial.ESTADOS_ABIERTOS)
    elif estado == "mias":
        qs = [s for s in qs.filter(estado=SolicitudMaterial.Estado.APROBACION) if sol.roles_que_puede_firmar(s, request.user)]
    elif estado:
        qs = qs.filter(estado=estado)
    abiertas = SolicitudMaterial.objects.filter(estado__in=SolicitudMaterial.ESTADOS_ABIERTOS)
    por_firmar = [s for s in abiertas.filter(estado=SolicitudMaterial.Estado.APROBACION).prefetch_related("firmas")
                  if sol.roles_que_puede_firmar(s, request.user)]
    compras = [(s, f) for s in abiertas.filter(estado=SolicitudMaterial.Estado.COMPRA).select_related("ticket")
               for f in sol.faltantes(s)]
    return render(request, "inventario/solicitudes.html", {
        "lista": qs, "estado": estado, "estados": SolicitudMaterial.Estado.choices,
        "conteo": Counter(abiertas.values_list("estado", flat=True)), "por_firmar": len(por_firmar), "compras": compras,
    })


# --- Herramientas de trabajo por técnico y auditoría ----------------------------------------
def _herramientas_visibles(usuario):
    from .models import Herramienta
    qs = Herramienta.objects.select_related("tecnico")
    return qs if usuario.gestiona_inventario else qs.filter(tecnico=usuario)


@login_required
@inventario_requerido
def herramientas(request):
    from .models import AuditoriaHerramientas, Herramienta

    qs = _herramientas_visibles(request.user)
    q, estado, tecnico = request.GET.get("q", "").strip(), request.GET.get("estado", ""), request.GET.get("tecnico", "")
    if q:
        qs = qs.filter(Q(nombre__icontains=q) | Q(serial__icontains=q) | Q(marca__icontains=q) | Q(tecnico_nombre__icontains=q))
    if estado:
        qs = qs.filter(estado=estado)
    elif not q:
        qs = qs.exclude(estado=Herramienta.Estado.BAJA)
    if tecnico == "bodega":
        qs = qs.filter(tecnico__isnull=True, tecnico_nombre="")
    elif tecnico.isdigit():
        qs = qs.filter(tecnico_id=tecnico)
    # Resumen por técnico: cuántas tiene, valor y su última auditoría
    resumen = {}
    for h in Herramienta.objects.exclude(estado=Herramienta.Estado.BAJA).select_related("tecnico"):
        clave = h.tecnico_id or (h.tecnico_nombre and f"txt:{h.tecnico_nombre}") or "bodega"
        r = resumen.setdefault(clave, {"nombre": h.responsable or "Bodega (sin responsable)", "usuario": h.tecnico,
                                       "herramientas": 0, "unidades": 0, "valor": Decimal(0), "auditoria": None})
        r["herramientas"] += 1
        r["unidades"] += h.cantidad
        r["valor"] += h.valor
    for a in AuditoriaHerramientas.objects.filter(tecnico__isnull=False).order_by("tecnico_id", "-fecha"):
        if a.tecnico_id in resumen and resumen[a.tecnico_id]["auditoria"] is None:
            resumen[a.tecnico_id]["auditoria"] = a
    if not request.user.gestiona_inventario:
        resumen = {k: v for k, v in resumen.items() if k == request.user.pk}
    return render(request, "inventario/herramientas.html", {
        "lista": qs.order_by("tecnico__first_name", "tecnico_nombre", "nombre"), "q": q, "estado": estado, "tecnico": tecnico,
        "estados": Herramienta.Estado.choices, "tecnicos": tecnicos(),
        "resumen": sorted(resumen.values(), key=lambda r: (r["usuario"] is None, r["nombre"])),
    })


def _listas_herramienta():
    from .models import Herramienta
    return {"marcas": list(Marca.objects.values_list("nombre", flat=True)),
            "nombres": sorted(set(Herramienta.objects.values_list("nombre", flat=True)), key=str.lower)}


@login_required
@gestion_requerida
def herramienta_form(request, pk=None):
    from . import herramientas as srv
    from .forms import HerramientaForm
    from .models import Herramienta

    herramienta = get_object_or_404(Herramienta, pk=pk) if pk else None
    anterior = herramienta.tecnico_id if herramienta else None
    form = HerramientaForm(request.POST or None, instance=herramienta,
                           initial={"tecnico": request.GET.get("tecnico")} if not herramienta else None)
    if request.method == "POST" and form.is_valid():
        h = srv.guardar(form.save(commit=False), request.user, responsable_anterior=anterior)
        messages.success(request, f"Herramienta guardada: {h}." + (f" Quedó a cargo de {h.tecnico}." if h.tecnico else ""))
        return redirect(h)
    return render(request, "inventario/herramienta_form.html", {"form": form, "herramienta": herramienta, **_listas_herramienta()})


@login_required
@inventario_requerido
def herramienta_detalle(request, pk):
    h = get_object_or_404(_herramientas_visibles(request.user), pk=pk)
    return render(request, "inventario/herramienta.html", {
        "h": h, "entregas": h.entregas.select_related("tecnico"), "tecnicos": tecnicos(),
        "revisiones": h.auditorias.select_related("auditoria").order_by("-auditoria__fecha"),
    })


@login_required
@gestion_requerida
@require_POST
def herramienta_mover(request, pk):
    from . import herramientas as srv
    from .models import Herramienta

    h = get_object_or_404(Herramienta, pk=pk)
    try:
        if request.POST.get("accion") == "devolver":
            srv.devolver(h, request.user)
            messages.success(request, f"«{h.nombre}» volvió a bodega.")
        else:
            tecnico = get_object_or_404(Usuario, pk=request.POST.get("tecnico") or 0)
            srv.entregar(h, tecnico, request.user)
            messages.success(request, f"«{h.nombre}» quedó a cargo de {tecnico}.")
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    return redirect(h)


@login_required
@gestion_requerida
def auditar(request, pk):
    """Auditoría física de las herramientas de un técnico (todas las que tiene registradas)."""
    from . import herramientas as srv
    from .models import DetalleAuditoria

    tecnico = get_object_or_404(Usuario, pk=pk)
    registradas = srv.por_auditar(tecnico)
    errores = []
    if request.method == "POST":
        encontrado = {h.pk: {"cantidad": request.POST.get(f"cantidad_{h.pk}"), "estado": request.POST.get(f"estado_{h.pk}"),
                             "nota": request.POST.get(f"nota_{h.pk}", ""), "baja": request.POST.get(f"baja_{h.pk}") == "on"}
                      for h in registradas}
        try:
            auditoria = srv.registrar_auditoria(tecnico, request.user, encontrado, request.POST.get("nota_general", ""),
                                                request.META.get("REMOTE_ADDR", ""))
        except ValidationError as e:
            errores = e.messages
        else:
            novedades = f" ({auditoria.novedades} con novedad)." if auditoria.novedades else "."
            messages.success(request, f"Auditoría #{auditoria.pk} de {tecnico}: {auditoria.get_resultado_display()}{novedades}")
            return redirect(auditoria)
    filas = [{"h": h, "cantidad": request.POST.get(f"cantidad_{h.pk}", h.cantidad),
              "estado": request.POST.get(f"estado_{h.pk}", "en_uso" if h.estado == "disponible" else h.estado),
              "nota": request.POST.get(f"nota_{h.pk}", ""), "baja": request.POST.get(f"baja_{h.pk}") == "on"} for h in registradas]
    return render(request, "inventario/auditar.html", {
        "tecnico": tecnico, "filas": filas, "errores": errores, "estados": DetalleAuditoria.EstadoEncontrado.choices,
        "nota_general": request.POST.get("nota_general", ""),
    })


@login_required
@inventario_requerido
def auditorias(request):
    from .models import AuditoriaHerramientas
    qs = AuditoriaHerramientas.objects.select_related("tecnico", "auditor")
    if not request.user.gestiona_inventario:
        qs = qs.filter(tecnico=request.user)
    tecnico = request.GET.get("tecnico", "")
    if tecnico.isdigit():
        qs = qs.filter(tecnico_id=tecnico)
    if request.GET.get("resultado"):
        qs = qs.filter(resultado=request.GET["resultado"])
    return render(request, "inventario/auditorias.html", {"lista": qs[:300], "tecnico": tecnico, "tecnicos": tecnicos(),
                                                          "resultado": request.GET.get("resultado", "")})


@login_required
@inventario_requerido
def auditoria_detalle(request, pk):
    from .models import AuditoriaHerramientas
    a = get_object_or_404(AuditoriaHerramientas.objects.select_related("tecnico", "auditor"), pk=pk)
    if not request.user.gestiona_inventario and a.tecnico_id != request.user.pk:
        raise PermissionDenied
    return render(request, "inventario/auditoria.html", {"a": a, "detalles": a.detalles.select_related("herramienta")})


# --- Préstamos y alquileres (fase 2 de Inventario_Global) ------------------------------------
@login_required
@inventario_requerido
def prestamos(request):
    """Equipos prestados o alquilados en clientes, con su fecha prevista de devolución y los vencidos."""
    qs = (Activo.objects.filter(estado__in=[E.ALQUILADO, E.PRESTADO], lugar=Lugar.CLIENTE)
          .select_related("empresa", "sede", "tecnico"))
    tipo, q, ver = request.GET.get("tipo", ""), request.GET.get("q", "").strip(), request.GET.get("ver", "")
    if tipo in (E.ALQUILADO, E.PRESTADO):
        qs = qs.filter(estado=tipo)
    if q:
        qs = qs.filter(Q(placa__icontains=q) | Q(empresa__razon_social__icontains=q) | Q(cliente_texto__icontains=q)
                       | Q(descripcion__icontains=q) | Q(referencia__icontains=q))
    hoy = servicios.timezone.localdate()
    todos = list(qs.order_by(F("fecha_devolucion").asc(nulls_last=True), "placa"))
    if ver == "vencidos":
        todos = [a for a in todos if a.vencido]
    elif ver == "semana":
        todos = [a for a in todos if a.fecha_devolucion and 0 <= (a.fecha_devolucion - hoy).days <= 7]
    elif ver == "sin_fecha":
        todos = [a for a in todos if not a.fecha_devolucion]
    base = list(Activo.objects.filter(estado__in=[E.ALQUILADO, E.PRESTADO], lugar=Lugar.CLIENTE))
    return render(request, "inventario/prestamos.html", {
        "lista": todos, "tipo": tipo, "q": q, "ver": ver, "hoy": hoy,
        "kpi": {"total": len(base), "prestados": sum(a.estado == E.PRESTADO for a in base),
                "alquilados": sum(a.estado == E.ALQUILADO for a in base), "vencidos": sum(a.vencido for a in base),
                "semana": sum(1 for a in base if a.fecha_devolucion and 0 <= (a.fecha_devolucion - hoy).days <= 7),
                "sin_fecha": sum(1 for a in base if not a.fecha_devolucion)},
    })


@login_required
@gestion_requerida
@require_POST
def cambiar_devolucion(request, placa):
    """Prórroga (o primera fecha) de un préstamo o alquiler: queda en la hoja de vida con el motivo."""
    from datetime import date

    activo = get_object_or_404(Activo, placa=placa)
    motivo = request.POST.get("motivo", "").strip()
    try:
        nueva = date.fromisoformat(request.POST.get("fecha", ""))
    except ValueError:
        nueva = None
    if activo.estado not in (E.ALQUILADO, E.PRESTADO) or not nueva or len(motivo) < 5:
        messages.error(request, "Indique la nueva fecha y el motivo (el equipo debe estar prestado o alquilado).")
        return redirect(activo)
    anterior = activo.fecha_devolucion
    MovimientoActivo.objects.create(
        activo=activo, tipo=T.AJUSTE, origen=activo.lugar, destino=activo.lugar, estado=activo.estado,
        fecha=servicios.timezone.localdate(), tecnico=activo.tecnico, tecnico_nombre=activo.tecnico_nombre,
        empresa=activo.empresa, cliente_texto=activo.cliente_texto, sede=activo.sede, modalidad=activo.modalidad,
        fecha_devolucion=nueva, registrado_por=request.user,
        registrado_por_nombre=request.user.get_full_name() or request.user.username,
        observaciones=f"Fecha de devolución: {anterior:%d/%m/%Y} → {nueva:%d/%m/%Y}. {motivo}"[:500] if anterior
        else f"Fecha de devolución: {nueva:%d/%m/%Y}. {motivo}"[:500])
    activo.fecha_devolucion = nueva
    activo.save(update_fields=["fecha_devolucion"])
    messages.success(request, f"{activo.placa}: debe volver el {nueva:%d/%m/%Y}.")
    return redirect(request.POST.get("volver") or activo)


# --- Fase 3: catálogos, clientes y proveedores, funcionarios --------------------------------
@login_required
@inventario_requerido
def catalogo(request, tipo):
    from . import catalogos as cat

    if tipo not in cat.CATALOGOS:
        raise Http404
    if request.method == "POST":
        if not request.user.gestiona_inventario:
            raise PermissionDenied
        modelo = cat.CATALOGOS[tipo]["modelo"]
        accion = request.POST.get("accion")
        try:
            if accion == "crear":
                item = cat.crear(tipo, request.POST.get("nombre", ""))
                messages.success(request, f"Se agregó «{item}».")
            else:
                item = get_object_or_404(modelo, pk=request.POST.get("id"))
                if accion == "renombrar":
                    anterior = item.nombre
                    n = cat.renombrar(tipo, item, request.POST.get("nombre", ""))
                    messages.success(request, f"«{anterior}» ahora es «{item.nombre}» ({n} registro(s) actualizados).")
                elif accion == "unificar":
                    n = cat.unificar(tipo, item)
                    messages.success(request, f"{n} registro(s) quedaron como «{item.nombre}».")
                elif accion == "eliminar":
                    nombre = item.nombre
                    cat.eliminar(tipo, item)
                    messages.success(request, f"Se eliminó «{nombre}».")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
        return redirect("inventario:catalogo", tipo=tipo)
    filas, sueltas = cat.filas(tipo)
    q = request.GET.get("q", "").strip().lower()
    if q:
        filas = [f for f in filas if q in f["item"].nombre.lower()]
    return render(request, "inventario/catalogo.html", {
        "tipo": tipo, "conf": cat.CATALOGOS[tipo], "filas": filas, "sueltas": sueltas, "q": q,
        "con_variantes": sum(1 for f in filas if f["variantes"]),
    })


def _clientes_sin_crear():
    """Clientes que el inventario anterior registró solo como texto (no están en la plataforma)."""
    cuenta = Counter()
    for modelo in (Activo, MovimientoActivo, MovimientoInsumo):
        cuenta.update(modelo.objects.filter(empresa__isnull=True).exclude(cliente_texto="").values_list("cliente_texto", flat=True))
    return cuenta.most_common()


@login_required
@inventario_requerido
def contactos(request):
    from accounts.models import Empresa

    tipo = "proveedores" if request.GET.get("tipo") == "proveedores" else "clientes"
    q = request.GET.get("q", "").strip()
    if tipo == "clientes":
        from .clientes import listado
        lista = listado(q)
    else:
        from .proveedores import listado
        lista = listado(q)
    return render(request, "inventario/contactos.html", {
        "tipo": tipo, "lista": lista, "q": q, "sin_crear": _clientes_sin_crear() if tipo == "clientes" else [],
        "empresas": Empresa.objects.filter(es_prospecto=False).order_by("razon_social"),
    })


@login_required
@gestion_requerida
def contacto_form(request, tipo, pk=None):
    from accounts.models import Empresa

    from .forms import ClienteContactoForm, ProveedorForm
    from .models import Proveedor

    modelo, formulario = (Empresa, ClienteContactoForm) if tipo == "clientes" else (Proveedor, ProveedorForm)
    objeto = get_object_or_404(modelo, pk=pk) if pk else None
    inicial = {"razon_social": request.GET.get("nombre", "")} if not objeto else None
    form = formulario(request.POST or None, instance=objeto, initial=inicial)
    if request.method == "POST" and form.is_valid():
        guardado = form.save()
        texto = request.POST.get("texto_anterior", "")
        if tipo == "clientes" and texto:
            n = _asociar_cliente(texto, guardado)
            messages.success(request, f"Cliente «{guardado}» creado y asociado a {n} registro(s) del inventario.")
        else:
            messages.success(request, f"Se guardó «{guardado}».")
        return redirect("inventario:cliente" if tipo == "clientes" else "inventario:proveedor", guardado.pk)
    return render(request, "inventario/contacto_form.html", {"form": form, "tipo": tipo, "objeto": objeto,
                                                             "texto_anterior": request.GET.get("nombre", "")})


def _asociar_cliente(texto, empresa):
    total = 0
    for modelo in (Activo, MovimientoActivo, MovimientoInsumo):
        total += modelo.objects.filter(empresa__isnull=True, cliente_texto=texto).update(empresa=empresa)
    return total


@login_required
@gestion_requerida
@require_POST
def asociar_cliente(request):
    """Un cliente escrito en el inventario anterior es uno que ya existe (o estaba mal escrito)."""
    from accounts.models import Empresa
    texto = request.POST.get("texto", "")
    empresa = get_object_or_404(Empresa, pk=request.POST.get("empresa") or 0)
    n = _asociar_cliente(texto, empresa)
    messages.success(request, f"«{texto}» quedó asociado a {empresa} ({n} registro(s)).")
    return redirect(f"{reverse('inventario:contactos')}?tipo=clientes")


def _puede_editar_funcionario(editor, persona=None):
    if editor.is_superuser:
        return True
    if editor.rol not in ("direccion", "supervisor"):
        return False
    return persona is None or persona.pk == editor.pk or not (persona.is_superuser or persona.rol in ("direccion", "supervisor"))


def _personas_sin_usuario():
    """Quienes aparecen en el inventario anterior pero aún no tienen usuario (con su cédula si se conoce)."""
    from .models import Herramienta
    cuenta, cedulas = Counter(), {}
    for modelo in (MovimientoActivo, MovimientoInsumo):
        for nombre, cedula in modelo.objects.filter(tecnico__isnull=True).exclude(tecnico_nombre="").values_list(
                "tecnico_nombre", "tecnico_documento"):
            cuenta[nombre] += 1
            cedulas[nombre] = cedula or cedulas.get(nombre, "")
    cuenta.update(Herramienta.objects.filter(tecnico__isnull=True).exclude(tecnico_nombre="").values_list("tecnico_nombre", flat=True))
    return [(n, cedulas.get(n, ""), c) for n, c in cuenta.most_common()]


@login_required
@funcionarios_requerido
def funcionarios(request):
    qs = Usuario.objects.exclude(rol="cliente").order_by("-is_active", "first_name", "last_name")
    area, estado, q = request.GET.get("area", ""), request.GET.get("estado", "activos"), request.GET.get("q", "").strip()
    if area:
        qs = qs.filter(area=area)
    if estado == "activos":
        qs = qs.filter(is_active=True)
    elif estado == "inactivos":
        qs = qs.filter(is_active=False)
    if q:
        qs = qs.filter(Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(documento__icontains=q) | Q(cargo__icontains=q))
    return render(request, "inventario/funcionarios.html", {
        "lista": qs, "area": area, "estado": estado, "q": q, "puede_editar": _puede_editar_funcionario(request.user),
        "areas": sorted(set(Usuario.objects.exclude(area="").values_list("area", flat=True))),
        "pendientes": _personas_sin_usuario(), "sin_cedula": Usuario.objects.exclude(rol="cliente").filter(documento="", is_active=True).count(),
    })


@login_required
@funcionarios_requerido
def funcionario_form(request, pk=None):
    from .forms import AREAS, FuncionarioForm

    persona = get_object_or_404(Usuario.objects.exclude(rol="cliente"), pk=pk) if pk else None
    if not _puede_editar_funcionario(request.user, persona):
        raise PermissionDenied
    inicial = None
    if not persona and request.GET.get("nombre"):
        # «ESPINOSA CALDERON JORGE ALBERTO» (apellidos primero, como el inventario anterior)
        partes = request.GET["nombre"].title().split()
        inicial = {"last_name": " ".join(partes[:2]), "first_name": " ".join(partes[2:]) or partes[0],
                   "documento": request.GET.get("cedula", ""), "rol": "agente"}
    form = FuncionarioForm(request.POST or None, instance=persona, initial=inicial, editor=request.user)
    if request.method == "POST" and form.is_valid():
        nuevo = persona is None
        guardado = form.save()  # al guardarse se le asocia lo que el inventario tenía a su nombre o cédula
        messages.success(request, f"Funcionario guardado: {guardado}."
                         + (f" Usuario «{guardado.username}» sin acceso: asígnele contraseña en Configuración si va a entrar a la plataforma." if nuevo else ""))
        return redirect("inventario:funcionarios")
    return render(request, "inventario/funcionario_form.html", {"form": form, "persona": persona, "areas": AREAS})


@login_required
@gestion_requerida
def facturas(request):
    """Listado de compras: cada factura con su proveedor, lo que trajo y el total, llegue por orden de compra o suelta."""
    from datetime import datetime

    from .models import OrdenCompra

    q = request.GET.get("q", "").strip()
    fechas = {}
    for campo in ("desde", "hasta"):
        try:
            fechas[campo] = datetime.strptime(request.GET.get(campo, ""), "%Y-%m-%d").date()
        except ValueError:
            fechas[campo] = None

    def filtrar(qs, producto):
        if fechas["desde"]:
            qs = qs.filter(fecha__gte=fechas["desde"])
        if fechas["hasta"]:
            qs = qs.filter(fecha__lte=fechas["hasta"])
        if q:
            qs = qs.filter(Q(proveedor__icontains=q) | Q(factura__icontains=q) | producto)
        return qs

    insumos = filtrar(MovimientoInsumo.objects.filter(tipo=T.COMPRA).select_related("insumo"),
                      Q(insumo__descripcion__icontains=q) | Q(insumo__codigo__icontains=q))
    activos = filtrar(MovimientoActivo.objects.filter(tipo=T.COMPRA).select_related("activo"),
                      Q(activo__descripcion__icontains=q) | Q(activo__placa__icontains=q))
    grupos = {}

    def grupo(m):
        clave = (m.fecha, m.proveedor.strip().upper(), m.factura.strip().upper())
        return grupos.setdefault(clave, {"fecha": m.fecha, "proveedor": m.proveedor or "—", "factura": m.factura or "Sin número",
                                         "productos": [], "total": Decimal(0), "registro": m.registrado_por_nombre})

    for m in insumos:
        g = grupo(m)
        vu, iva = m.valor_unitario or Decimal(0), m.iva or Decimal(0)
        g["productos"].append(f"{m.insumo.descripcion} [{m.cantidad.normalize():f} {m.insumo.unidad.lower()}]")
        g["total"] += m.cantidad * vu * (1 + iva / 100)
    equipos = defaultdict(lambda: [0, Decimal(0)])
    for m in activos:
        g = grupo(m)
        clave = (m.fecha, m.proveedor.strip().upper(), m.factura.strip().upper(), m.activo.descripcion)
        equipos[clave][0] += 1
        valor = (m.activo.valor or Decimal(0)) * (1 + (m.activo.iva or Decimal(0)) / 100)
        equipos[clave][1] += valor
        g["total"] += valor
    for (fecha, prov, fact, desc), (n, _) in equipos.items():
        grupos[(fecha, prov, fact)]["productos"].append(f"{desc} [{n} equipo{'s' if n != 1 else ''}]")
    ordenes = {(o.factura.strip().upper()): o for o in OrdenCompra.objects.exclude(factura="")}
    lista = sorted(grupos.values(), key=lambda g: (g["fecha"], g["factura"]), reverse=True)
    for g in lista:
        g["orden"] = ordenes.get(g["factura"].strip().upper())
    return render(request, "inventario/facturas.html", {
        "facturas": lista[:300], "total": sum((g["total"] for g in lista), Decimal(0)), "cantidad": len(lista), "q": q,
        "desde": request.GET.get("desde", ""), "hasta": request.GET.get("hasta", ""),
    })


@login_required
@gestion_requerida
def etiquetas(request):
    """Etiquetas con QR para marcar los equipos: por placas, por factura o por código de placa. Con «imprimir», el PDF."""
    import re

    from django.http import HttpResponse

    from .pdf import etiquetas_pdf

    placas = request.GET.get("placas", "").strip()
    factura = request.GET.get("factura", "").strip()
    prefijo = request.GET.get("prefijo", "").strip().upper()
    activos = Activo.objects.none()
    if placas:
        lista = [p.strip().upper() for p in re.split(r"[\s,;]+", placas) if p.strip()]
        encontrados = {a.placa: a for a in Activo.objects.filter(placa__in=lista)}
        activos = [encontrados[p] for p in dict.fromkeys(lista) if p in encontrados]  # en el orden en que se escribieron
        faltan = [p for p in lista if p not in encontrados]
        if faltan:
            messages.warning(request, f"No existen estas placas: {', '.join(faltan[:20])}.")
    elif factura:
        activos = list(Activo.objects.filter(factura__iexact=factura).order_by("placa"))
    elif prefijo:
        activos = list(Activo.objects.filter(placa__startswith=prefijo).order_by("placa"))

    def numero(nombre, defecto, maximo):
        valor = request.GET.get(nombre, "")
        return min(max(int(valor), 1), maximo) if valor.isdigit() else defecto

    desde, copias = numero("desde", 1, 30), numero("copias", 1, 5)
    if request.GET.get("imprimir") and activos:
        respuesta = HttpResponse(etiquetas_pdf(activos, desde=desde, copias=copias), content_type="application/pdf")
        respuesta["Content-Disposition"] = 'inline; filename="etiquetas-placas.pdf"'
        return respuesta
    return render(request, "inventario/etiquetas.html", {
        "activos": activos, "placas": placas, "factura": factura, "prefijo": prefijo, "desde": desde, "copias": copias,
        "buscado": bool(placas or factura or prefijo), "codigos": CodigoActivo.objects.all(),
    })


@login_required
@gestion_requerida
def garantias(request):
    """Garantías y devoluciones al proveedor: lo que está por fuera y cómo se resolvió lo que ya volvió."""
    from datetime import datetime

    from tickets.models import Ticket

    from . import garantias as gar
    from .models import GarantiaProveedor

    if request.method == "POST":
        placa = request.POST.get("placa", "").split(" · ")[0].strip().upper()
        activo = Activo.objects.filter(placa=placa).first()
        motivo = request.POST.get("motivo", "").strip()
        try:
            fecha = datetime.strptime(request.POST.get("fecha", ""), "%Y-%m-%d").date()
        except ValueError:
            fecha = None
        radicado = request.POST.get("ticket", "").strip().upper()
        ticket = Ticket.objects.filter(radicado=radicado).first() if radicado else None
        if not activo or len(motivo) < 5 or not fecha or (radicado and not ticket):
            messages.error(request, "Revise la placa, qué falla (al menos unas palabras), la fecha"
                                    + (" y el número del ticket" if radicado and not ticket else "") + ".")
        else:
            try:
                g = gar.abrir(activo, motivo, fecha, request.user, proveedor=request.POST.get("proveedor", ""),
                              radicado=request.POST.get("radicado", "").strip(), ticket=ticket)
            except ValidationError as e:
                messages.error(request, " ".join(e.messages))
            else:
                messages.success(request, f"{activo.placa} quedó en garantía con el proveedor ({g.numero}).")
                return redirect("inventario:garantia", g.pk)
    ver = "cerradas" if request.GET.get("ver") == "cerradas" else "abiertas"
    casos = GarantiaProveedor.objects.select_related("activo", "proveedor", "ticket")
    casos = casos.filter(estado=GarantiaProveedor.Estado.EN_PROVEEDOR) if ver == "abiertas" \
        else casos.exclude(estado=GarantiaProveedor.Estado.EN_PROVEEDOR)
    return render(request, "inventario/garantias.html", {
        "casos": casos[:300], "ver": ver, "abiertas": GarantiaProveedor.objects.filter(estado="en_proveedor").count(),
        "equipos": Activo.objects.exclude(estado__in=[E.BAJA, E.VENDIDO, E.GARANTIA]).order_by("placa"),
        "proveedores": Proveedor.objects.values_list("razon_social", flat=True), "hoy": servicios.timezone.localdate(),
        "placa": request.GET.get("placa", ""),
    })


@login_required
@gestion_requerida
def garantia_detalle(request, pk):
    from datetime import datetime

    from . import garantias as gar
    from .models import GarantiaProveedor

    g = get_object_or_404(GarantiaProveedor.objects.select_related("activo", "proveedor", "ticket", "creado_por"), pk=pk)
    if request.method == "POST" and g.abierta:
        estado = request.POST.get("estado", "")
        try:
            fecha = datetime.strptime(request.POST.get("fecha", ""), "%Y-%m-%d").date()
            valor = request.POST.get("valor_nota", "").strip()
            if estado not in GarantiaProveedor.Estado.values or estado == "en_proveedor":
                raise ValidationError("Elija cómo se resolvió.")
            gar.resolver(g, estado, fecha, request.user, resultado=request.POST.get("resultado", "").strip(),
                         serial_nuevo=request.POST.get("serial_nuevo", ""), valor_nota=Decimal(valor) if valor else None)
        except (ValueError, ArithmeticError):
            messages.error(request, "Revise la fecha y el valor.")
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
        else:
            messages.success(request, f"{g.numero} cerrada: {g.get_estado_display().lower()}. {g.activo.placa} quedó "
                                      f"{g.activo.get_estado_display().lower()}.")
        return redirect("inventario:garantia", g.pk)
    return render(request, "inventario/garantia.html", {
        "g": g, "vence": gar.vence(g.activo), "hoy": servicios.timezone.localdate(),
        "estados": [c for c in GarantiaProveedor.Estado.choices if c[0] != "en_proveedor"],
        "anteriores": g.activo.garantias.exclude(pk=g.pk),
    })


@login_required
def firma_imagen(request, tipo, pk):
    """Las firmas a mano de bodega (aprobaciones y actas de entrega): nunca públicas, se valida el acceso a la solicitud."""
    from django.http import FileResponse

    from .models import ActaEntrega, FirmaSolicitud

    if tipo == "aprobacion":
        objeto = get_object_or_404(FirmaSolicitud.objects.select_related("solicitud"), pk=pk)
        solicitud, campo = objeto.solicitud, objeto.imagen
    elif tipo in ("entrega", "recibe"):
        objeto = get_object_or_404(ActaEntrega.objects.select_related("solicitud"), pk=pk)
        solicitud, campo = objeto.solicitud, objeto.firma_entrega if tipo == "entrega" else objeto.firma_recibe
    else:
        raise Http404
    if not _puede_ver_solicitud(request.user, solicitud) or not campo:
        raise Http404
    try:
        return FileResponse(campo.open("rb"), content_type="image/png")
    except FileNotFoundError:
        raise Http404("La firma ya no está disponible.")
