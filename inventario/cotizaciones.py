"""Solicitudes de cotización: preguntarle a los proveedores antes de comprar.

La solicitud solo pregunta precio y fecha de entrega, no compromete a JANOTEC. Se manda a uno o varios proveedores, cada
respuesta se registra con sus precios y se comparan lado a lado. De la cotización elegida sale la orden de compra, que sí
es el compromiso; si conviene comprarle cada cosa a uno distinto, lo que quede sin ordenar se ordena con otra cotización.

Su razón de ser es armar la propuesta comercial: bodega trae del informe del ticket los materiales y equipos que
necesita el cliente, ve con qué precio se ha manejado cada uno (historial de compras y cotizaciones o el catálogo de
precios) y en rojo lo que no tiene precio o proveedor. También llegan de una solicitud de material (lo que falta en
bodega) o se escriben a mano. Cada línea es equipo o material y va a UN proveedor: se genera un PDF y un correo por
proveedor, solo con sus elementos (cámara a uno y cable a otro = dos solicitudes), y su enlace de WhatsApp.
"""
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from django import forms
from django.contrib import messages
from django.contrib.admin.models import ADDITION, CHANGE
from django.contrib.auth.decorators import login_required
from django.core.mail import EmailMultiAlternatives
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
from .pdf import solicitud_cotizacion_pdf, solicitud_pdf
from .productos import _registro
from .views import gestion_requerida


def cotiza_requerido(vista):
    """Las solicitudes de cotización las manejan bodega, dirección y el área administrativa (que arma las propuestas).
    La orden de compra que sale de una cotización sigue siendo de bodega (gestion_requerida)."""
    from functools import wraps

    from django.core.exceptions import PermissionDenied

    @wraps(vista)
    def envoltura(request, *args, **kwargs):
        """Deja pasar a bodega y al área administrativa (arma propuestas); los demás reciben 403."""
        if not (request.user.gestiona_inventario or request.user.arma_propuestas):
            raise PermissionDenied
        return vista(request, *args, **kwargs)
    return envoltura

E = SolicitudCotizacion.Estado


class SolicitudForm(BootstrapMixin, forms.ModelForm):
    """Datos generales de la solicitud de cotización: fecha límite y notas para el proveedor."""
    class Meta:
        model = SolicitudCotizacion
        fields = ["fecha_limite", "notas"]
        widgets = {"fecha_limite": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "notas": forms.Textarea(attrs={"rows": 3, "placeholder": "Marca preferida, garantía, lugar de entrega…"})}

    def __init__(self, *args, **kwargs):
        """Aplica los estilos de la plataforma a los campos."""
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()


class LineaForm(BootstrapMixin, forms.ModelForm):
    """Un renglón por cotizar: qué es, cuánto se pide y a qué proveedor se le pregunta."""
    class Meta:
        model = LineaSolicitudCotizacion
        fields = ["tipo", "descripcion", "cantidad", "adicional", "proveedor", "precio_referencia"]
        widgets = {"cantidad": forms.NumberInput(attrs={"step": "any", "min": "0"}),
                   "adicional": forms.NumberInput(attrs={"step": "any", "min": "0"}),
                   "precio_referencia": forms.NumberInput(attrs={"step": "any", "min": "0", "placeholder": "—"}),
                   "descripcion": forms.TextInput(attrs={"list": "elementos-conocidos", "autocomplete": "off"})}

    def __init__(self, *args, **kwargs):
        """Cantidad, adicional y tipo opcionales; el proveedor se elige de la lista ordenada."""
        super().__init__(*args, **kwargs)
        self.fields["cantidad"].required = self.fields["adicional"].required = self.fields["tipo"].required = False
        self.fields["proveedor"].queryset = Proveedor.objects.order_by("razon_social")
        self.fields["proveedor"].empty_label = "— Elija el proveedor —"
        self._aplicar_estilos()

    def clean(self):
        """Vacíos cuentan como cero y el tipo por defecto es material; exige pedir algo (cantidad + adicional > 0)."""
        d = super().clean()
        d["cantidad"], d["adicional"] = d.get("cantidad") or Decimal(0), d.get("adicional") or Decimal(0)
        d["tipo"] = d.get("tipo") or LineaSolicitudCotizacion.Tipo.MATERIAL
        if d["cantidad"] < 0 or d["adicional"] < 0 or d["cantidad"] + d["adicional"] <= 0:
            self.add_error("adicional", "Indique cuánto se va a cotizar.")
        return d


def formset_lineas():
    """Formset de los renglones de la solicitud (dos vacíos de más para agregar, y se pueden borrar)."""
    return forms.inlineformset_factory(SolicitudCotizacion, LineaSolicitudCotizacion, form=LineaForm, extra=2, can_delete=True)


class RespuestaForm(BootstrapMixin, forms.ModelForm):
    """Cotización que devuelve el proveedor: referencia, fechas, condiciones y el archivo (PDF o imagen)."""
    class Meta:
        model = CotizacionProveedor
        fields = ["referencia", "fecha", "fecha_entrega", "condiciones", "archivo"]
        widgets = {"fecha": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "fecha_entrega": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "condiciones": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        """El archivo solo admite PDF o imagen."""
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


# --- Elementos del informe del ticket y precio de referencia ---------------------------------------
ETAPAS_POR_COTIZAR = ("gestion", "elaborando", "enviada", "ajustes")  # en comercial, antes de que el cliente apruebe


def tipo_de(descripcion):
    """Equipo o material según la descripción (las mismas palabras con las que el cotizador agrupa la propuesta)."""
    from comercial.models import LineaCotizacion

    T = LineaSolicitudCotizacion.Tipo
    # Switch, router, UPS… van en conectividad o energía en la propuesta, pero son equipos
    es_equipo = (LineaCotizacion(descripcion=descripcion).categoria_final == "equipos"
                 or re.search(r"\b(switch|router|ups|regulador|transformador|antena)", descripcion.lower()))
    return T.EQUIPO if es_equipo else T.MATERIAL


def elementos_del_informe(ficha):
    """Los materiales y equipos que pide el informe del requerimiento del ticket: [(cantidad, descripción, tipo)]."""
    from comercial.cotizador import materiales_del_informe

    return [(cantidad, descripcion, tipo_de(descripcion)) for cantidad, descripcion in materiales_del_informe(ficha)]


def tickets_por_cotizar():
    """Los tickets en comercial (aún sin propuesta aprobada) cuyo informe pide materiales o equipos."""
    from comercial.models import FichaComercial

    fichas = (FichaComercial.objects.filter(etapa__in=ETAPAS_POR_COTIZAR, informe__isnull=False)
              .exclude(informe__materiales="").select_related("ticket", "ticket__empresa", "informe")
              .prefetch_related("solicitudes_cotizacion"))
    salida = []
    for ficha in fichas.order_by("-ticket__creado")[:50]:
        ficha.n_elementos = len(elementos_del_informe(ficha))
        if ficha.n_elementos:
            ficha.ya_cotizada = [p for p in ficha.solicitudes_cotizacion.all() if p.estado != E.CANCELADA]
            salida.append(ficha)
    return salida


def historial_de_precios(limite=500):
    """{descripción en minúscula: (proveedor, valor unitario)} con lo último que se compró o se cotizó.

    Manda lo que de verdad se compró (órdenes de compra); si no, la última cotización recibida.
    """
    conocidos = {}
    for d, prov, valor in (PrecioCotizado.objects.order_by("-cotizacion__recibida_en")
                           .values_list("linea__descripcion", "cotizacion__proveedor", "valor_unitario")[:limite]):
        conocidos.setdefault(d.strip().lower(), (prov, valor))
    compras = {}
    for d, prov, valor in (LineaOrdenCompra.objects.filter(orden__proveedor__isnull=False, valor_unitario__gt=0)
                           .exclude(orden__estado=OrdenCompra.Estado.CANCELADA).order_by("-orden__creado")
                           .values_list("descripcion", "orden__proveedor", "valor_unitario")[:limite]):
        compras.setdefault(d.strip().lower(), (prov, valor))
    conocidos.update(compras)
    return conocidos


def sugerir_proveedor(lineas, conocidos=None):
    """Llena el proveedor y el precio de referencia de las líneas que no los tienen.

    Primero el historial (a quién se le compró o quién lo cotizó, y a cuánto); si no hay, el precio del catálogo de
    precios (sin proveedor). Lo que quede sin precio o sin proveedor sale en rojo para que bodega lo complete.
    """
    from comercial.cotizador import _buscar_en_catalogo

    conocidos = historial_de_precios() if conocidos is None else conocidos
    for l in lineas:
        if l.proveedor_id and l.precio_referencia:
            continue
        proveedor, precio = conocidos.get(l.descripcion.strip().lower(), (None, None))
        if precio is None:
            item = _buscar_en_catalogo(l.descripcion)
            precio = item.valor_unitario if item and item.valor_unitario > 0 else None
        l.proveedor_id = l.proveedor_id or proveedor
        l.precio_referencia = l.precio_referencia or precio
        l.save(update_fields=["proveedor", "precio_referencia"])


def sincronizar_proveedores(pedido):
    """A quién se le pregunta sale de las líneas: un proveedor por cada uno elegido en ellas.

    Se quita el que ya no tiene líneas (si aún no respondió).
    """
    elegidos = {l.proveedor_id for l in pedido.lineas.all() if l.proveedor_id}
    for proveedor_id in elegidos:
        CotizacionProveedor.objects.get_or_create(solicitud=pedido, proveedor_id=proveedor_id)
    pedido.cotizaciones.exclude(proveedor_id__in=elegidos).filter(recibida_en__isnull=True).delete()


# --- Envío a los proveedores: correo con su PDF y enlace de WhatsApp -------------------------------
def _correos_proveedor(proveedor):
    """Correos del proveedor sin repetir: primero el de su asesor, luego el general."""
    asesor = proveedor.asesor
    return [c for c in dict.fromkeys([asesor.correo if asesor else "", proveedor.correo]) if c]


def enlace_whatsapp(cotizacion):
    """Enlace para abrir el chat de WhatsApp del proveedor con el mensaje listo (el PDF se adjunta a mano)."""
    asesor = cotizacion.proveedor.asesor
    numero = re.sub(r"\D", "", (asesor.celular if asesor and asesor.celular else cotizacion.proveedor.telefono) or "")
    if len(numero) == 10 and numero.startswith("3"):
        numero = "57" + numero  # celular de Colombia sin indicativo
    texto = (f"Buenos días. Le enviamos la solicitud de cotización {cotizacion.solicitud.numero} de JANOTEC S.A.S. "
             "Le adjunto el PDF con los elementos; le agradecemos precio, IVA y fecha de entrega.")
    return f"https://wa.me/{numero}?text={quote(texto)}" if len(numero) >= 10 else ""


def enviar_a_proveedores(pedido, usuario):
    """Le manda a cada proveedor de la solicitud su PDF por correo, solo con los elementos que son suyos.

    Devuelve (enviados, sin_correo) con los nombres de los proveedores, para avisarle a bodega a quién hay que
    mandárselo por WhatsApp u otro medio.
    """
    enviados, sin_correo = [], []
    for c in pedido.cotizaciones.select_related("proveedor").prefetch_related("proveedor__asesores"):
        pr = c.proveedor
        if not pedido.lineas_para(pr):
            continue
        destinos = _correos_proveedor(pr)
        if not destinos:
            sin_correo.append(pr.razon_social)
            continue
        nombre, contenido = solicitud_pdf(pedido, pr)
        asunto = f"Solicitud de cotización {pedido.numero} · JANOTEC S.A.S."
        cuerpo = "\n".join([f"Señores {pr.razon_social}:", "",
                             "Le pedimos cotizar los elementos del documento adjunto, con precio, IVA y fecha de entrega."
                             + (f" Le agradecemos responder antes del {pedido.fecha_limite:%d/%m/%Y}." if pedido.fecha_limite else ""),
                             "Esta solicitud no es una orden de compra ni compromete a JANOTEC S.A.S.", "",
                             "Cordialmente,", usuario.get_full_name() or usuario.username,
                             "Bodega, inventario y logística · JANOTEC S.A.S."])
        correo = EmailMultiAlternatives(asunto, cuerpo, None, destinos, reply_to=[usuario.email] if usuario.email else None)
        correo.attach(nombre, contenido, "application/pdf")
        try:
            correo.send(fail_silently=False)
            enviados.append(pr.razon_social)
        except Exception:  # noqa: BLE001 — el correo no debe tumbar el envío; se avisa como «sin correo»
            sin_correo.append(pr.razon_social)
    return enviados, sin_correo


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
        c.sin_cotizar = len(pedido.lineas_para(c.proveedor)) - len(precios)  # de lo que se le preguntó a él
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
@cotiza_requerido
def listado(request):
    """Bandeja de solicitudes de cotización (SC), por defecto las abiertas; ?estado= filtra por otro estado."""
    estado = request.GET.get("estado", "abiertas")
    qs = (SolicitudCotizacion.objects.select_related("solicitud", "solicitud__ticket", "propuesta__ticket")
          .prefetch_related("lineas", "cotizaciones"))
    if estado == "abiertas":
        qs = qs.filter(estado__in=[E.BORRADOR, E.ENVIADA])
    elif estado in E.values:
        qs = qs.filter(estado=estado)
    filas = list(qs[:200])
    for p in filas:
        p.recibidas = sum(1 for c in p.cotizaciones.all() if c.recibida_en)
    return render(request, "inventario/cotizaciones.html", {"pedidos": filas, "estado": estado, "estados": E.choices,
                                                            "por_cotizar": tickets_por_cotizar()})


@login_required
@cotiza_requerido
@require_POST
def nueva(request):
    """Solicitud en preparación. Desde un ticket en comercial llega con los materiales y equipos de su informe; desde
    una solicitud de material, con lo que falta. A cada línea se le sugiere el proveedor y el precio de referencia."""
    from comercial.models import FichaComercial

    solicitud = ficha = None
    if str(request.POST.get("solicitud", "")).isdigit():
        solicitud = get_object_or_404(SolicitudMaterial, pk=request.POST["solicitud"])
    elif str(request.POST.get("propuesta", "")).isdigit():
        ficha = get_object_or_404(FichaComercial.objects.select_related("ticket"), pk=request.POST["propuesta"])
        if not elementos_del_informe(ficha):
            messages.error(request, "El informe de ese ticket no pide materiales ni equipos.")
            return redirect("inventario:cotizaciones")
    pedido = SolicitudCotizacion.objects.create(solicitud=solicitud, propuesta=ficha, creado_por=request.user)
    if ficha:
        for cantidad, descripcion, tipo in elementos_del_informe(ficha):
            LineaSolicitudCotizacion.objects.create(solicitud=pedido, tipo=tipo, descripcion=descripcion[:300], cantidad=cantidad)
        from comercial.servicios import _seguimiento
        _seguimiento(ficha.ticket, request.user, f"Bodega pidió cotización a proveedores ({pedido.numero}) de los materiales "
                                                 "y equipos del informe, para armar la propuesta.", interno=True)
    if solicitud:
        for f in sol.faltantes(solicitud):
            LineaSolicitudCotizacion.objects.create(solicitud=pedido, item=f["item"], descripcion=f["item"].descripcion[:300],
                                                    cantidad=f["falta"])
        sol._nota(solicitud, request.user, f"bodega preparó la solicitud de cotización {pedido.numero}.")
    sugerir_proveedor(pedido.lineas.all())
    sincronizar_proveedores(pedido)
    return redirect(pedido)


@login_required
@cotiza_requerido
def detalle(request, pk):
    """Ficha de una SC: mientras está en preparación se editan sus datos y renglones; enviada ya no cambia."""
    pedido = get_object_or_404(SolicitudCotizacion.objects.select_related("solicitud", "solicitud__ticket",
                                                                          "propuesta__ticket__empresa"), pk=pk)
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
                sugerir_proveedor(lineas.new_objects)
                sincronizar_proveedores(pedido)
                if accion == "enviar":
                    sin_proveedor = [l.descripcion for l in pedido.lineas.all() if not l.proveedor_id]
                    if not pedido.lineas.exists():
                        messages.error(request, "Para enviarla falta al menos un elemento.")
                        return redirect(pedido)
                    if sin_proveedor:
                        messages.error(request, "Elija el proveedor de: " + ", ".join(sin_proveedor) + " (en rojo).")
                        return redirect(pedido)
                    pedido.estado = E.ENVIADA
                    pedido.save(update_fields=["estado", "actualizado"])
                    if pedido.solicitud:
                        nombres = ", ".join(c.proveedor.razon_social for c in pedido.cotizaciones.select_related("proveedor"))
                        sol._nota(pedido.solicitud, request.user, f"solicitud de cotización {pedido.numero} enviada a {nombres}.")
            if accion == "enviar" and pedido.estado == E.ENVIADA:
                enviados, sin_correo = enviar_a_proveedores(pedido, request.user)
                if enviados:
                    messages.success(request, f"{pedido.numero} enviada por correo a: {', '.join(enviados)}.")
                if sin_correo:
                    messages.warning(request, f"Sin correo; envíele su PDF por WhatsApp u otro medio a: {', '.join(sin_correo)}.")
            else:
                messages.success(request, f"{pedido.numero} guardada.")
            return redirect(pedido)
        messages.error(request, "Revise los datos de la solicitud.")
    recibidas, filas = comparacion(pedido)
    cotizaciones = list(pedido.cotizaciones.select_related("proveedor").prefetch_related("proveedor__asesores"))
    for c in cotizaciones:
        c.whatsapp, c.n_elementos = enlace_whatsapp(c), len(pedido.lineas_para(c.proveedor))
    conocidos = LineaSolicitudCotizacion.objects.order_by("-pk").values_list("descripcion", flat=True)[:300]
    return render(request, "inventario/cotizacion.html", {
        "conocidos": sorted(set(conocidos)),
        "pedido": pedido, "form": form, "lineas": lineas, "editable": editable, "recibidas": recibidas, "filas": filas,
        "cotizaciones": cotizaciones,
        "abierta": pedido.estado in (E.BORRADOR, E.ENVIADA),
        # En rojo: lo que aún no tiene precio de referencia o proveedor que lo maneje
        "sin_completar": sum(1 for l in pedido.lineas.all() if not (l.proveedor_id and l.precio_referencia)),
        "ordenes": OrdenCompra.objects.filter(cotizacion__solicitud=pedido).select_related("proveedor")})


@login_required
@cotiza_requerido
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
    lineas = pedido.lineas_para(cotizacion.proveedor)  # solo lo que se le preguntó a este proveedor
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
@cotiza_requerido
def pdf(request, pk, cot_pk):
    """PDF de la solicitud que se le envía a un proveedor (solo sus renglones), abierto en el navegador."""
    cotizacion = get_object_or_404(CotizacionProveedor.objects.select_related("proveedor", "solicitud"), pk=cot_pk, solicitud_id=pk)
    nombre, contenido = solicitud_cotizacion_pdf(cotizacion)
    r = HttpResponse(contenido, content_type="application/pdf")
    r["Content-Disposition"] = f'inline; filename="{nombre}"'
    return r


@login_required
@cotiza_requerido
def archivo(request, pk, cot_pk):
    """Abre el archivo que adjuntó el proveedor con su cotización (404 si no hay)."""
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
