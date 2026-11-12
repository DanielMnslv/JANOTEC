"""Recibir la factura de una orden de compra (o una compra sin orden): todo lo que llegó entra a bodega de una vez.

Cada línea se ingresa como insumo (por cantidad, con su código) o como equipos con placa (placas consecutivas y
seriales). Lo que la orden pedía para el ticket queda apartado para su solicitud de material (lo hace
`solicitudes.revisar_compras`); lo adicional queda libre en bodega. También se pueden agregar líneas de la factura que no
estaban en la orden.

Sin orden (``orden=None``, «Ingreso desde la factura»), cada fila entra a bodega libre, sin apartar nada.
"""
from decimal import Decimal

from django import forms
from django.db import transaction
from django.utils import timezone

from tickets.forms import BootstrapMixin

from . import servicios
from .models import Activo, Insumo, LineaOrdenCompra, Lugar, MovimientoActivo, OrdenCompra, Proveedor

FILAS_EXTRA = 3


class RecepcionForm(BootstrapMixin, forms.Form):
    factura = forms.CharField(label="N.º de factura", max_length=60)
    fecha = forms.DateField(label="Fecha de la factura", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    proveedor = forms.CharField(max_length=200, widget=forms.TextInput(attrs={"list": "lista-proveedores"}))
    soporte = forms.FileField(label="Factura (PDF o foto)", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["soporte"].widget.attrs["accept"] = ".pdf,image/*"
        self._aplicar_estilos()


class FilaRecepcionForm(BootstrapMixin, forms.Form):
    CLASES = [("insumo", "Insumo / material"), ("equipo", "Equipo con placa"), ("no", "No llegó")]

    linea = forms.IntegerField(required=False, widget=forms.HiddenInput)
    clase = forms.ChoiceField(choices=CLASES, initial="insumo")
    descripcion = forms.CharField(max_length=250, required=False)
    cantidad = forms.DecimalField(min_value=0, max_digits=12, decimal_places=2, required=False)
    valor_unitario = forms.DecimalField(label="Valor unit. sin IVA", min_value=0, max_digits=14, decimal_places=2, required=False)
    iva = forms.DecimalField(min_value=0, max_digits=5, decimal_places=2, initial=19, required=False)
    # Sin límite de largo en la casilla: la sugerencia trae «CÓDIGO · descripción» y el código se separa en clean
    codigo = forms.CharField(label="Código del insumo", max_length=300, required=False,
                             widget=forms.TextInput(attrs={"list": "lista-insumos"}))
    unidad = forms.ChoiceField(choices=Insumo.Unidad.choices, initial=Insumo.Unidad.UNIDAD, required=False)
    prefijo = forms.CharField(label="Código de placa", max_length=36, required=False,
                              widget=forms.TextInput(attrs={"list": "lista-codigos"}))
    referencia = forms.CharField(max_length=80, required=False)
    marca = forms.CharField(max_length=80, required=False)
    seriales = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 1, "placeholder": "Un serial por línea"}))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()

    @property
    def vacia(self):
        d = getattr(self, "cleaned_data", {}) or {}
        return not d.get("linea") and not (d.get("descripcion") or d.get("codigo") or d.get("prefijo"))

    def clean(self):
        d = super().clean()
        if d.get("clase") == "no" or self.vacia:
            return d
        cantidad = d.get("cantidad") or Decimal(0)
        if cantidad <= 0:
            self.add_error("cantidad", "¿Cuánto llegó?")
        if d.get("valor_unitario") is None:
            self.add_error("valor_unitario", "Escriba el valor de la factura.")
        d["iva"] = d.get("iva") if d.get("iva") is not None else Decimal(19)
        if d.get("clase") == "insumo":
            d["codigo"] = (d.get("codigo") or "").split(" · ")[0].strip().upper()
            if not d["codigo"]:
                self.add_error("codigo", "Elija el insumo o escriba un código nuevo.")
            elif len(d["codigo"]) > 40:
                self.add_error("codigo", "El código es de máximo 40 caracteres.")
            elif not Insumo.objects.filter(codigo=d["codigo"]).exists() and not d.get("descripcion"):
                self.add_error("descripcion", "Es un código nuevo: escriba la descripción.")
        else:
            prefijo = (d.get("prefijo") or "").strip().upper().replace(" ", "")
            d["prefijo"] = Activo.prefijo(prefijo) if prefijo else ""
            if not d["prefijo"]:
                self.add_error("prefijo", "Escriba el código de placa (ej.: CAM-BAL-2MP).")
            if cantidad != cantidad.to_integral_value():
                self.add_error("cantidad", "Los equipos se reciben por unidades enteras.")
            seriales = [s.strip() for s in (d.get("seriales") or "").splitlines() if s.strip()]
            if len(seriales) > cantidad:
                self.add_error("seriales", f"Hay {len(seriales)} seriales para {cantidad:g} equipos.")
            repetidos = list(Activo.objects.filter(serial__in=seriales).values_list("serial", flat=True))
            if repetidos:
                self.add_error("seriales", f"Ya existe un activo con el serial {', '.join(repetidos)}.")
            d["lista_seriales"] = seriales
        return d


FilasRecepcion = forms.formset_factory(FilaRecepcionForm, extra=0)


def filas_iniciales(orden):
    """Una fila por cada línea a la que le falta llegar algo, con lo que se sabe de ella."""
    filas = []
    for l in orden.por_recibir:
        item = l.item
        fila = {"linea": l.pk, "descripcion": l.descripcion, "cantidad": l.pendiente, "valor_unitario": l.valor_unitario,
                "iva": l.iva, "clase": "insumo"}
        if item and item.insumo_id:
            fila.update(codigo=item.insumo.codigo, unidad=item.insumo.unidad)
        elif item and item.referencia:
            fila.update(clase="equipo", referencia=item.referencia)
        filas.append(fila)
    return filas + [{"clase": "insumo", "iva": 19}] * FILAS_EXTRA


@transaction.atomic
def recibir(orden, cabecera, filas, usuario, soporte=None):
    """Ingresa a bodega lo que llegó en la factura. Devuelve (insumos, placas) para el mensaje.

    Con ``orden=None`` es una compra sin orden: no hay líneas que marcar ni solicitud que apartar."""
    proveedor, _ = Proveedor.objects.get_or_create(razon_social=cabecera["proveedor"].strip())
    comunes = {"fecha": cabecera["fecha"], "proveedor": proveedor.razon_social, "factura": cabecera["factura"]}
    nombre = usuario.get_full_name() or usuario.username
    lineas = {l.pk: l for l in orden.lineas.select_related("item", "item__insumo")} if orden else {}
    solicitud = orden.solicitud if orden else None
    nota = f"Factura {cabecera['factura']}" + (f" · {orden.numero}" if orden else " · ingreso desde la factura")
    insumos, placas = [], []
    for d in filas:
        if d.get("clase") == "no" or not d.get("cantidad"):
            continue
        linea = lineas.get(d.get("linea"))
        if linea is None and orden:  # vino en la factura sin estar en la orden: se agrega, todo para bodega
            linea = LineaOrdenCompra.objects.create(orden=orden, descripcion=(d.get("descripcion") or d.get("codigo")
                                                                             or d.get("prefijo"))[:300],
                                                    adicional=d["cantidad"], valor_unitario=d["valor_unitario"], iva=d["iva"])
        item = linea.item if linea else None
        descripcion = d.get("descripcion") or (linea.descripcion if linea else "") or d.get("codigo") or d.get("prefijo")
        if d["clase"] == "insumo":
            insumo, _ = Insumo.objects.get_or_create(codigo=d["codigo"], defaults={
                "descripcion": descripcion[:250], "unidad": d.get("unidad") or Insumo.Unidad.UNIDAD})
            servicios.mover_insumo(insumo, d["cantidad"], MovimientoActivo.Tipo.COMPRA, Lugar.PROVEEDOR, Lugar.BODEGA, usuario,
                                   valor_unitario=d["valor_unitario"], iva=d["iva"], solicitud=solicitud,
                                   observaciones=nota, **comunes)
            if item and not item.insumo_id:  # lo escrito a mano en la solicitud ya tiene su insumo en bodega
                item.insumo = insumo
                item.save(update_fields=["insumo"])
            insumos.append(f"{d['cantidad']:g} {insumo.unidad.lower()} de {insumo.codigo}")
        else:
            referencia = (d.get("referencia") or (item.referencia if item else "") or d["prefijo"].rstrip("-")).strip()
            plantilla = Activo.objects.filter(placa__startswith=d["prefijo"]).order_by("-placa").first()
            seriales = d["lista_seriales"]
            for n in range(int(d["cantidad"])):
                activo = Activo.objects.create(
                    placa=Activo.siguiente_placa(d["prefijo"]), serial=seriales[n] if n < len(seriales) else "",
                    categoria=plantilla.categoria if plantilla else "", tipo=plantilla.tipo if plantilla else "",
                    subtipo=plantilla.subtipo if plantilla else "", marca=d.get("marca") or (plantilla.marca if plantilla else ""),
                    referencia=referencia, descripcion=descripcion[:300],
                    fecha_compra=cabecera["fecha"], proveedor=proveedor.razon_social, factura=cabecera["factura"],
                    valor=d["valor_unitario"], iva=d["iva"], responsable_compra=nombre, fecha_ultimo_movimiento=cabecera["fecha"])
                MovimientoActivo.objects.create(
                    activo=activo, tipo=MovimientoActivo.Tipo.COMPRA, origen=Lugar.PROVEEDOR, destino=Lugar.BODEGA,
                    estado=Activo.Estado.DISPONIBLE, registrado_por=usuario, registrado_por_nombre=nombre,
                    solicitud=solicitud, observaciones=nota, **comunes)
                placas.append(activo.placa)
            if item and not item.insumo_id and not item.referencia:  # así la solicitud reconoce los equipos que llegaron
                item.referencia = referencia[:80]
                item.save(update_fields=["referencia"])
        if linea:
            linea.recibido += d["cantidad"]
            linea.save(update_fields=["recibido"])
    if orden is None:
        return insumos, placas
    orden.factura, orden.fecha_factura = cabecera["factura"], cabecera["fecha"]
    orden.recibida_por, orden.recibida_en = usuario, timezone.now()
    orden.proveedor = orden.proveedor or proveedor
    if soporte:
        orden.soporte = soporte
    if orden.completa:
        orden.estado = OrdenCompra.Estado.RECIBIDA
    orden.save()
    if orden.solicitud:
        from . import solicitudes as sol
        falta = "" if orden.estado == OrdenCompra.Estado.RECIBIDA else " (llegó una parte; falta el resto)"
        sol._nota(orden.solicitud, usuario, f"llegó la factura {cabecera['factura']} de {proveedor} ({orden.numero}) y "
                                            f"entró a bodega{falta}.")
    return insumos, placas
