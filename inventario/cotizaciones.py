"""Solicitudes de cotización: preguntarle a los proveedores antes de comprar.

La solicitud solo pregunta precio y fecha de entrega, no compromete a JANOTEC. Se manda a uno o varios proveedores, cada
respuesta se registra con sus precios y se comparan lado a lado. De la cotización elegida sale la orden de compra, que sí
es el compromiso; si conviene comprarle cada cosa a uno distinto, lo que quede sin ordenar se ordena con otra cotización.
"""
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE
from django.contrib.auth.decorators import login_required
from django.core.validators import FileExtensionValidator
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from tickets.forms import BootstrapMixin

from . import solicitudes as sol
from .models import (CotizacionProveedor, LineaOrdenCompra, LineaSolicitudCotizacion, OrdenCompra, PrecioCotizado, Proveedor,
                     SolicitudCotizacion, SolicitudMaterial)
from .pdf import solicitud_cotizacion_pdf
from .productos import _registro
from .views import gestion_requerida

E = SolicitudCotizacion.Estado


class SolicitudForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = SolicitudCotizacion
        fields = ["fecha_limite", "notas"]
        widgets = {"fecha_limite": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "notas": forms.Textarea(attrs={"rows": 3, "placeholder": "Marca preferida, garantía, lugar de entrega…"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()


class LineaForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = LineaSolicitudCotizacion
        fields = ["descripcion", "cantidad", "adicional"]
        widgets = {"cantidad": forms.NumberInput(attrs={"step": "any", "min": "0"}),
                   "adicional": forms.NumberInput(attrs={"step": "any", "min": "0"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cantidad"].required = self.fields["adicional"].required = False
        self._aplicar_estilos()

    def clean(self):
        d = super().clean()
        d["cantidad"], d["adicional"] = d.get("cantidad") or Decimal(0), d.get("adicional") or Decimal(0)
        if d["cantidad"] < 0 or d["adicional"] < 0 or d["cantidad"] + d["adicional"] <= 0:
            self.add_error("adicional", "Indique cuánto se va a cotizar.")
        return d


def formset_lineas():
    return forms.inlineformset_factory(SolicitudCotizacion, LineaSolicitudCotizacion, form=LineaForm, extra=2, can_delete=True)


class RespuestaForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = CotizacionProveedor
        fields = ["referencia", "fecha", "fecha_entrega", "condiciones", "archivo"]
        widgets = {"fecha": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "fecha_entrega": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "condiciones": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["archivo"].widget.attrs["accept"] = ".pdf,image/*"
        self.fields["archivo"].validators.append(FileExtensionValidator(["pdf", "png", "jpg", "jpeg"]))
        self._aplicar_estilos()


def _precios(post, lineas):
    """{línea: (valor, IVA)} de lo que se llenó; None si algún dato no sirve. La línea vacía es que no la cotizó."""
    precios = {}
    for l in lineas:
        texto = post.get(f"valor_{l.pk}", "").replace(",", ".").strip()
        if not texto:
            continue
        try:
            valor = Decimal(texto)
            iva = Decimal(post.get(f"iva_{l.pk}", "").replace(",", ".").strip() or "0")
            if not (0 < valor < 10 ** 12 and 0 <= iva <= 100):
                return None
        except InvalidOperation:  # incluye NaN e Infinity al comparar
            return None
        precios[l] = (valor, iva)
    return precios


def comparacion(pedido):
    """Las cotizaciones recibidas lado a lado: por línea, cuál es la más barata (con IVA), y el total de cada una."""
    recibidas = [c for c in pedido.cotizaciones.select_related("proveedor").prefetch_related("precios__linea", "ordenes")
                 if c.recibida_en]
    pendientes = {l.pk for l in pedido.por_ordenar()}
    filas = []
    for l in pedido.lineas.all():
        celdas = [next((p for p in c.precios.all() if p.linea_id == l.pk), None) for c in recibidas]
        con_iva = [p.valor_unitario * (100 + p.iva) for p in celdas if p]
        filas.append({"linea": l, "ordenada": l.pk not in pendientes, "celdas": [
            {"precio": p, "mejor": bool(p) and len(con_iva) > 1 and p.valor_unitario * (100 + p.iva) == min(con_iva)} for p in celdas]})
    for c in recibidas:
        precios = list(c.precios.all())
        c.total = sum((p.subtotal * (100 + p.iva) / 100 for p in precios), Decimal(0))
        c.sin_cotizar = len(filas) - len(precios)
        c.por_comprar = sum(1 for p in precios if p.linea_id in pendientes)
        c.vigentes = [o for o in c.ordenes.all() if o.estado != OrdenCompra.Estado.CANCELADA]
    return recibidas, filas


def reabrir(orden):
    """Se canceló la orden o se le quitó una línea: lo que salió de ella vuelve a estar por ordenar con otra cotización."""
    pedido = orden.cotizacion.solicitud if orden.cotizacion else None
    if pedido and pedido.estado == E.CERRADA and pedido.por_ordenar():
        pedido.estado = E.ENVIADA
        pedido.save(update_fields=["estado", "actualizado"])


@login_required
@gestion_requerida
def listado(request):
    estado = request.GET.get("estado", "abiertas")
    qs = SolicitudCotizacion.objects.select_related("solicitud", "solicitud__ticket").prefetch_related("lineas", "cotizaciones")
    if estado == "abiertas":
        qs = qs.filter(estado__in=[E.BORRADOR, E.ENVIADA])
    elif estado in E.values:
        qs = qs.filter(estado=estado)
    filas = list(qs[:200])
    for p in filas:
        p.recibidas = sum(1 for c in p.cotizaciones.all() if c.recibida_en)
    return render(request, "inventario/cotizaciones.html", {"pedidos": filas, "estado": estado, "estados": E.choices})


@login_required
@gestion_requerida
@require_POST
def nueva(request):
    """Solicitud en preparación; desde una solicitud de material llega con lo que falta."""
    solicitud = None
    if str(request.POST.get("solicitud", "")).isdigit():
        solicitud = get_object_or_404(SolicitudMaterial, pk=request.POST["solicitud"])
    pedido = SolicitudCotizacion.objects.create(solicitud=solicitud, creado_por=request.user)
    if solicitud:
        for f in sol.faltantes(solicitud):
            LineaSolicitudCotizacion.objects.create(solicitud=pedido, item=f["item"], descripcion=f["item"].descripcion[:300],
                                                    cantidad=f["falta"])
        sol._nota(solicitud, request.user, f"bodega preparó la solicitud de cotización {pedido.numero}.")
    return redirect(pedido)


@login_required
@gestion_requerida
def detalle(request, pk):
    pedido = get_object_or_404(SolicitudCotizacion.objects.select_related("solicitud", "solicitud__ticket"), pk=pk)
    editable = pedido.estado == E.BORRADOR  # lo que ya se le mandó al proveedor no cambia
    form = SolicitudForm(request.POST or None, instance=pedido) if editable else None
    lineas = formset_lineas()(request.POST or None, instance=pedido, prefix="lineas") if editable else None
    if request.method == "POST":
        accion = request.POST.get("accion", "guardar")
        if accion == "cancelar":
            if pedido.estado in (E.BORRADOR, E.ENVIADA):
                pedido.estado = E.CANCELADA
                pedido.save(update_fields=["estado", "actualizado"])
                if pedido.solicitud:
                    sol._nota(pedido.solicitud, request.user, f"solicitud de cotización {pedido.numero}: cancelada.")
                messages.info(request, f"{pedido.numero} cancelada. Las órdenes de compra que ya salieron de ella siguen vigentes.")
            return redirect(pedido)
        if not editable:
            return redirect(pedido)
        if form.is_valid() and lineas.is_valid():
            with transaction.atomic():
                form.save()
                lineas.save()
                if accion == "enviar":
                    faltan = [t for t, v in (("al menos un producto", pedido.lineas.exists()),
                                             ("al menos un proveedor", pedido.cotizaciones.exists())) if not v]
                    if faltan:
                        messages.error(request, "Para enviarla falta " + " y ".join(faltan) + ".")
                        return redirect(pedido)
                    pedido.estado = E.ENVIADA
                    pedido.save(update_fields=["estado", "actualizado"])
                    if pedido.solicitud:
                        nombres = ", ".join(c.proveedor.razon_social for c in pedido.cotizaciones.select_related("proveedor"))
                        sol._nota(pedido.solicitud, request.user, f"solicitud de cotización {pedido.numero} enviada a {nombres}.")
            messages.success(request, f"{pedido.numero}: descargue el PDF de cada proveedor y envíeselo." if accion == "enviar"
                             else f"{pedido.numero} guardada.")
            return redirect(pedido)
        messages.error(request, "Revise los datos de la solicitud.")
    recibidas, filas = comparacion(pedido)
    return render(request, "inventario/cotizacion.html", {
        "pedido": pedido, "form": form, "lineas": lineas, "editable": editable, "recibidas": recibidas, "filas": filas,
        "cotizaciones": pedido.cotizaciones.select_related("proveedor"),
        "abierta": pedido.estado in (E.BORRADOR, E.ENVIADA),
        "proveedores": Proveedor.objects.exclude(cotizaciones__solicitud=pedido).order_by("razon_social"),
        "ordenes": OrdenCompra.objects.filter(cotizacion__solicitud=pedido).select_related("proveedor")})


@login_required
@gestion_requerida
@require_POST
def agregar_proveedor(request, pk):
    pedido = get_object_or_404(SolicitudCotizacion, pk=pk)
    proveedor = Proveedor.objects.filter(pk=request.POST.get("proveedor") or None).first()
    if pedido.estado not in (E.BORRADOR, E.ENVIADA) or proveedor is None:
        messages.error(request, "Elija un proveedor.")
    else:
        CotizacionProveedor.objects.get_or_create(solicitud=pedido, proveedor=proveedor)
    return redirect(pedido)


@login_required
@gestion_requerida
@require_POST
def quitar_proveedor(request, pk, cot_pk):
    cotizacion = get_object_or_404(CotizacionProveedor, pk=cot_pk, solicitud_id=pk)
    if cotizacion.recibida_en:
        messages.error(request, "Ese proveedor ya respondió: su cotización no se quita.")
    else:
        cotizacion.delete()
    return redirect("inventario:cotizacion", pk)


@login_required
@gestion_requerida
def respuesta(request, pk, cot_pk):
    """El proveedor respondió: sus precios por línea, la fecha de entrega y el PDF que mandó."""
    pedido = get_object_or_404(SolicitudCotizacion, pk=pk)
    cotizacion = get_object_or_404(CotizacionProveedor.objects.select_related("proveedor"), pk=cot_pk, solicitud=pedido)
    if pedido.estado == E.BORRADOR:
        messages.error(request, "Primero marque la solicitud como enviada.")
        return redirect(pedido)
    editable = pedido.estado == E.ENVIADA and not cotizacion.ordenes.exclude(estado=OrdenCompra.Estado.CANCELADA).exists()
    if request.method == "POST" and not editable:
        messages.error(request, "Esta cotización ya no se puede cambiar.")
        return redirect(pedido)
    inicial = {"fecha": cotizacion.fecha or timezone.localdate()}
    form = RespuestaForm(request.POST or None, request.FILES or None, instance=cotizacion, initial=inicial)
    lineas = list(pedido.lineas.all())
    if request.method == "POST":
        precios = _precios(request.POST, lineas)
        if precios is None:
            messages.error(request, "Revise los precios: deben ser números mayores que cero y el IVA de 0 a 100.")
        elif not precios:
            messages.error(request, "Escriba el precio de al menos un producto.")
        elif form.is_valid():
            with transaction.atomic():
                guardada = form.save(commit=False)
                nueva_respuesta = guardada.recibida_en is None
                if nueva_respuesta:
                    guardada.recibida_en, guardada.recibida_por = timezone.now(), request.user
                guardada.save()
                guardada.precios.exclude(linea__in=precios).delete()
                for linea, (valor, iva) in precios.items():
                    PrecioCotizado.objects.update_or_create(cotizacion=guardada, linea=linea,
                                                            defaults={"valor_unitario": valor, "iva": iva})
                _registro(request.user, guardada, ADDITION if nueva_respuesta else CHANGE,
                          f"Cotización de {guardada.proveedor}: {len(precios)} de {len(lineas)} productos")
            messages.success(request, f"Se registró la cotización de {guardada.proveedor}.")
            return redirect(pedido)
        else:
            messages.error(request, "Revise los datos de la cotización.")
    guardados = {p.linea_id: p for p in cotizacion.precios.all()}
    filas = [{"linea": l, "valor": request.POST.get(f"valor_{l.pk}") if request.method == "POST"
              else (guardados[l.pk].valor_unitario if l.pk in guardados else ""),
              "iva": request.POST.get(f"iva_{l.pk}") if request.method == "POST"
              else (guardados[l.pk].iva if l.pk in guardados else 19)} for l in lineas]
    return render(request, "inventario/cotizacion_respuesta.html", {
        "pedido": pedido, "c": cotizacion, "form": form, "filas": filas, "editable": editable})


@login_required
@gestion_requerida
def pdf(request, pk, cot_pk):
    cotizacion = get_object_or_404(CotizacionProveedor.objects.select_related("proveedor", "solicitud"), pk=cot_pk, solicitud_id=pk)
    nombre, contenido = solicitud_cotizacion_pdf(cotizacion)
    r = HttpResponse(contenido, content_type="application/pdf")
    r["Content-Disposition"] = f'inline; filename="{nombre}"'
    return r


@login_required
@gestion_requerida
def archivo(request, pk, cot_pk):
    cotizacion = get_object_or_404(CotizacionProveedor, pk=cot_pk, solicitud_id=pk)
    if not cotizacion.archivo:
        raise Http404
    return FileResponse(cotizacion.archivo.open("rb"), as_attachment=False, filename=cotizacion.archivo.name.rsplit("/", 1)[-1])


@login_required
@gestion_requerida
@require_POST
def ordenar(request, pk, cot_pk):
    """Se eligió esta cotización: sus precios pasan a una orden de compra en preparación, con lo que aún no se ha ordenado."""
    with transaction.atomic():
        pedido = get_object_or_404(SolicitudCotizacion.objects.select_for_update().select_related("solicitud"), pk=pk)
        cotizacion = get_object_or_404(CotizacionProveedor.objects.select_related("proveedor"), pk=cot_pk, solicitud=pedido)
        pendientes = {l.pk for l in pedido.por_ordenar()}
        precios = [p for p in cotizacion.precios.select_related("linea") if p.linea_id in pendientes]
        if pedido.estado != E.ENVIADA or not cotizacion.recibida_en or not precios:
            messages.error(request, "De esta cotización no queda nada por ordenar.")
            return redirect(pedido)
        orden = OrdenCompra.objects.create(proveedor=cotizacion.proveedor, solicitud=pedido.solicitud, cotizacion=cotizacion,
                                           fecha_estimada=cotizacion.fecha_entrega, notas=cotizacion.condiciones,
                                           soporte=cotizacion.archivo.name, creado_por=request.user)
        for p in precios:
            l = p.linea
            LineaOrdenCompra.objects.create(orden=orden, item=l.item, linea_cotizacion=l, descripcion=l.descripcion,
                                            cantidad=l.cantidad, adicional=l.adicional, valor_unitario=p.valor_unitario, iva=p.iva)
        if not pedido.por_ordenar():
            pedido.estado = E.CERRADA
            pedido.save(update_fields=["estado", "actualizado"])
        _registro(request.user, orden, ADDITION, f"Orden de compra desde {pedido.numero}, cotización de {cotizacion.proveedor}")
        if pedido.solicitud:
            sol._nota(pedido.solicitud, request.user, f"se eligió la cotización de {cotizacion.proveedor} ({pedido.numero}): "
                                                      f"orden de compra {orden.numero}.")
    messages.success(request, f"Se preparó {orden.numero}. Revísela y márquela enviada: ese es el compromiso de compra.")
    return redirect(orden)
