from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError

from accounts.models import Empresa, Usuario  # noqa: F401
from tickets.forms import BootstrapMixin
from tickets.models import Ticket

from .models import Activo, Insumo, LineaOrdenCompra, Modalidad, OrdenCompra


class _Estilos:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()


def tecnicos():
    return Usuario.tecnicos().order_by("first_name", "last_name")


class MaterialMixin:
    """Filas de placas (activos) y de código + cantidad (insumos) que se agregan con «+»."""

    def leer_material(self, datos, placas_validas=None):
        activos, insumos, errores = [], [], []
        placas = [p.strip().upper() for p in datos.getlist("placa") if p.strip()]
        for placa in dict.fromkeys(placas):
            activo = Activo.objects.filter(placa=placa).first()
            if activo is None:
                errores.append(f"No existe la placa {placa}.")
            elif placas_validas is not None and activo.pk not in placas_validas:
                errores.append(f"{placa}: {activo.get_estado_display()} · {activo.ubicacion}.")
            else:
                activos.append(activo)
        codigos, cantidades = datos.getlist("insumo"), datos.getlist("cantidad")
        for codigo, cantidad in zip(codigos, cantidades):
            codigo = codigo.split(" · ")[0].strip().upper()
            if not codigo and not cantidad.strip():
                continue
            insumo = Insumo.objects.filter(codigo=codigo).first()
            try:
                cantidad = Decimal(cantidad.replace(",", "."))
            except Exception:  # noqa: BLE001
                cantidad = Decimal(0)
            if insumo is None:
                errores.append(f"No existe el insumo {codigo}.")
            elif cantidad <= 0:
                errores.append(f"{codigo}: escriba la cantidad.")
            else:
                insumos.append((insumo, cantidad))
        return activos, insumos, errores


class EntregaForm(_Estilos, BootstrapMixin, forms.Form):
    tecnico = forms.ModelChoiceField(label="Técnico que recibe", queryset=Usuario.objects.none())
    modalidad = forms.ChoiceField(label="Modalidad", choices=[("", "— Elija —")] + [
        c for c in Modalidad.choices if c[0] != Modalidad.STOCK])
    ticket = forms.ModelChoiceField(label="Para el ticket (opcional)", required=False, queryset=Ticket.objects.none(),
                                    help_text="Al diligenciar el acta de este ticket, el material aparece marcado.")
    empresa = forms.ModelChoiceField(label="Cliente (si no hay ticket)", required=False,
                                     queryset=Empresa.objects.filter(activa=True))
    observaciones = forms.CharField(required=False, max_length=500,
                                    widget=forms.TextInput(attrs={"placeholder": "Ej.: Alquiler para Bioagrícola sede Las Marías"}))
    # Firmas a mano, como en el acta de soporte: quien entrega (bodega) y quien recibe (el técnico, si está presente)
    firma_entrega = forms.CharField(required=False, widget=forms.HiddenInput)
    firma_recibe = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tecnico"].queryset = tecnicos()
        self.fields["ticket"].queryset = (Ticket.objects.filter(estado__in=Ticket.ESTADOS_ABIERTOS)
                                          .select_related("empresa").order_by("-creado"))
        self.fields["ticket"].label_from_instance = lambda t: f"{t.radicado} · {t.empresa} · {t.asunto[:50]}"

    def clean_firma_entrega(self):
        from tickets.forms import leer_firma
        return leer_firma(self.cleaned_data.get("firma_entrega"), False, "de quien entrega")

    def clean_firma_recibe(self):
        from tickets.forms import leer_firma
        return leer_firma(self.cleaned_data.get("firma_recibe"), False, "de quien recibe")

    def clean(self):
        d = super().clean()
        if d.get("modalidad") not in (Modalidad.ASIGNACION, None, "") and not d.get("ticket") and not d.get("empresa"):
            raise ValidationError("Indique el ticket o el cliente para el que lleva el material.")
        return d


class DevolucionForm(_Estilos, BootstrapMixin, forms.Form):
    observaciones = forms.CharField(required=False, max_length=500,
                                    widget=forms.TextInput(attrs={"placeholder": "Ej.: Sobrante de la instalación en Gases del Llano"}))


class CambioEstadoForm(_Estilos, BootstrapMixin, forms.Form):
    """Cambios de un activo que no son entregas ni actas: retiro de un alquiler, mantenimiento, garantía, baja…"""

    ACCIONES = [
        ("devolucion", "Volvió a bodega (retiro de alquiler, devolución del cliente…)"),
        ("mantenimiento", "Enviar a mantenimiento / reparación"),
        ("garantia", "Enviar a garantía con el proveedor"),
        ("baja", "Dar de baja (obsolescencia, daño irreparable)"),
        ("no_encontrado", "No se encontró en la verificación física"),
    ]
    accion = forms.ChoiceField(label="Qué pasó", choices=ACCIONES)
    fecha = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    observaciones = forms.CharField(max_length=500, widget=forms.TextInput(attrs={"placeholder": "Motivo o detalle"}))


class SinFacturaMixin(forms.Form):
    """Ingreso a bodega sin factura (inventario inicial, devolución de un cliente, cortesía…): los datos de compra
    pasan a ser opcionales y el ingreso queda como ajuste de inventario, no como compra."""

    MOTIVOS = [("inventario_inicial", "Inventario inicial: ya estaba en bodega"), ("devolucion_cliente", "Lo devolvió un cliente"),
               ("cortesia", "Cortesía o muestra de un proveedor"), ("otro", "Otro (explíquelo en observaciones)")]
    DATOS_COMPRA = ()  # campos que exige una compra

    stock_minimo = forms.DecimalField(label="Stock mínimo", required=False, min_value=0, max_digits=12, decimal_places=2,
                                      widget=forms.NumberInput(attrs={"placeholder": "1", "step": "any"}),
                                      help_text="Se avisa para reponer cuando en bodega queda menos. Vacío: se deja como está (1 si es nuevo).")
    sin_factura = forms.BooleanField(label="Ingreso sin factura", required=False,
                                     help_text="Para lo que entra sin compra: no pide proveedor, factura ni valor.")
    motivo_sin_factura = forms.ChoiceField(label="¿De dónde sale?", choices=MOTIVOS, required=False)

    def _exigir_datos_de_compra(self, d):
        if d.get("sin_factura"):
            return
        for campo in self.DATOS_COMPRA:
            if d.get(campo) in (None, ""):
                self.add_error(campo, "Este campo es obligatorio (o marque «Ingreso sin factura»).")

    def nota_sin_factura(self):
        return f"Ingreso sin factura: {dict(self.MOTIVOS).get(self.cleaned_data.get('motivo_sin_factura'), 'sin motivo')}"


class CompraActivoForm(SinFacturaMixin, _Estilos, BootstrapMixin, forms.ModelForm):
    DATOS_COMPRA = ("fecha_compra", "proveedor", "factura", "valor")
    cantidad_iguales = forms.IntegerField(label="¿Cuántos iguales?", min_value=1, max_value=50, initial=1,
                                          help_text="Se crean placas consecutivas; los seriales se escriben uno por línea.")
    seriales = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Un serial por línea"}))
    prefijo = forms.CharField(label="Código de placa", max_length=36,
                              help_text="Ej.: CAM-BAL-2MP. La plataforma pone el consecutivo (CAM-BAL-2MP010).")

    class Meta:
        model = Activo
        fields = ["categoria", "tipo", "subtipo", "marca", "referencia", "descripcion", "fecha_compra", "proveedor",
                  "factura", "valor", "iva", "observaciones"]
        widgets = {"fecha_compra": forms.DateInput(attrs={"type": "date"}),
                   "descripcion": forms.TextInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in ("categoria", "tipo", "descripcion"):
            self.fields[campo].required = True
        for campo in self.DATOS_COMPRA:  # obligatorios salvo «Ingreso sin factura» (se revisa en clean)
            self.fields[campo].required = False
        for campo, lista in (("proveedor", "lista-proveedores"), ("categoria", "lista-categorias"), ("tipo", "lista-tipos"),
                             ("subtipo", "lista-subtipos"), ("marca", "lista-marcas"), ("prefijo", "lista-codigos")):
            self.fields[campo].widget.attrs["list"] = lista

    def clean_prefijo(self):
        return Activo.prefijo(self.cleaned_data["prefijo"].strip().upper().replace(" ", ""))

    def clean_marca(self):
        from .catalogos import canonica
        return canonica("marcas", self.cleaned_data.get("marca"))

    def clean_categoria(self):
        from .catalogos import canonica
        return canonica("categorias", (self.cleaned_data.get("categoria") or "").upper())

    def clean(self):
        d = super().clean()
        seriales = [s.strip() for s in (d.get("seriales") or "").splitlines() if s.strip()]
        if seriales and len(seriales) != d.get("cantidad_iguales", 1):
            self.add_error("seriales", f"Escribió {len(seriales)} seriales para {d.get('cantidad_iguales')} activos.")
        repetidos = Activo.objects.filter(serial__in=seriales).exclude(serial="").values_list("serial", flat=True)
        if repetidos:
            self.add_error("seriales", f"Ya existe un activo con el serial {', '.join(repetidos)}.")
        d["lista_seriales"] = seriales
        self._exigir_datos_de_compra(d)
        return d


class CompraInsumoForm(SinFacturaMixin, _Estilos, BootstrapMixin, forms.Form):
    DATOS_COMPRA = ("valor_unitario", "proveedor", "factura", "fecha")

    codigo = forms.CharField(label="Código del insumo", max_length=40, widget=forms.TextInput(attrs={"list": "lista-insumos"}))
    descripcion = forms.CharField(label="Descripción (solo si es un insumo nuevo)", required=False, max_length=250)
    unidad = forms.ChoiceField(label="Unidad", choices=Insumo.Unidad.choices, required=False)
    cantidad = forms.DecimalField(min_value=Decimal("0.01"), max_digits=12, decimal_places=2)
    valor_unitario = forms.DecimalField(label="Valor unitario sin IVA", min_value=0, max_digits=14, decimal_places=4, required=False)
    iva = forms.DecimalField(label="% IVA", initial=19, min_value=0, max_digits=5, decimal_places=2)
    proveedor = forms.CharField(max_length=200, required=False, widget=forms.TextInput(attrs={"list": "lista-proveedores"}))
    factura = forms.CharField(max_length=60, required=False)
    fecha = forms.DateField(label="Fecha de compra", required=False, widget=forms.DateInput(attrs={"type": "date"}))

    def clean(self):
        d = super().clean()
        codigo = (d.get("codigo") or "").split(" · ")[0].strip().upper()
        d["codigo"] = codigo
        if codigo and not Insumo.objects.filter(codigo=codigo).exists() and not d.get("descripcion"):
            self.add_error("descripcion", "Es un código nuevo: escriba la descripción para crearlo en el catálogo.")
        self._exigir_datos_de_compra(d)
        return d


class ValidarSolicitudForm(_Estilos, BootstrapMixin, forms.Form):
    """Bodega valida la solicitud: a cargo de quién, modalidad, quién la recibe y holgura de consumibles."""

    cargo = forms.ChoiceField(label="¿A cargo de quién está el material?", choices=[
        ("", "— Elija —"), ("janotec", "A cargo de JANOTEC (garantía, contrato, mantenimiento)"),
        ("cliente", "A cargo del cliente: se le envía propuesta y se espera su aprobación")])
    modalidad = forms.ChoiceField(label="Modalidad", choices=[("", "— Elija —")] + [
        c for c in Modalidad.choices if c[0] not in (Modalidad.STOCK, Modalidad.ASIGNACION)])
    tecnico = forms.ModelChoiceField(label="Técnico que lo recibe", queryset=Usuario.objects.none(), required=False)
    fecha_devolucion = forms.DateField(label="Fecha prevista de devolución", required=False,
                                       widget=forms.DateInput(attrs={"type": "date"}),
                                       help_text="Préstamo (obligatoria) o alquiler: cuándo deben volver los equipos a bodega.")
    comentario = forms.CharField(label="Comentario (opcional)", required=False, max_length=500)

    def __init__(self, *args, solicitud, **kwargs):
        self.solicitud = solicitud
        kwargs.setdefault("initial", {}).update(cargo=solicitud.cargo, modalidad=solicitud.modalidad, tecnico=solicitud.tecnico_id,
                                                fecha_devolucion=solicitud.fecha_devolucion)
        super().__init__(*args, **kwargs)
        self.fields["tecnico"].queryset = tecnicos()
        if solicitud.cliente_aprobo:  # la propuesta ya la aprobó el cliente
            self.fields["cargo"].help_text = "El cliente ya aprobó la propuesta."
        self.consumibles = [i for i in solicitud.items.select_related("insumo") if i.insumo_id]
        for item in self.consumibles:
            self.fields[f"adicional_{item.pk}"] = forms.DecimalField(
                label=f"{item.insumo.codigo}", required=False, min_value=0, max_digits=12, decimal_places=2,
                initial=item.adicional, widget=forms.NumberInput(attrs={"class": "form-control form-control-sm", "step": "any"}))
        # Escrito a mano (no está en el inventario): bodega confirma si ya lo tiene; si no, va a solicitud de compra
        self.escritos_a_mano = [i for i in solicitud.items.all() if not i.insumo_id and not i.referencia]
        for item in self.escritos_a_mano:
            self.fields[f"listo_{item.pk}"] = forms.BooleanField(
                label="Ya lo tengo en bodega", required=False, initial=item.a_mano_listo,
                widget=forms.CheckboxInput(attrs={"class": "form-check-input"}))

    def clean(self):
        d = super().clean()
        if d.get("modalidad") == Modalidad.PRESTAMO and not d.get("fecha_devolucion"):
            self.add_error("fecha_devolucion", "En un préstamo indique cuándo deben devolver los equipos.")
        return d

    def campos_adicionales(self):
        return [(item, self[f"adicional_{item.pk}"]) for item in self.consumibles]

    def campos_a_mano(self):
        return [(item, self[f"listo_{item.pk}"]) for item in self.escritos_a_mano]

    def a_mano_listos(self):
        return {item.pk: bool(self.cleaned_data.get(f"listo_{item.pk}")) for item in self.escritos_a_mano}

    def adicionales(self):
        return {item.pk: self.cleaned_data.get(f"adicional_{item.pk}") or Decimal(0) for item in self.consumibles}


class HerramientaForm(_Estilos, BootstrapMixin, forms.ModelForm):
    class Meta:
        from .models import Herramienta
        model = Herramienta
        fields = ["nombre", "marca", "serial", "descripcion", "cantidad", "estado", "tecnico", "costo_compra"]
        widgets = {"marca": forms.TextInput(attrs={"list": "lista-marcas"}),
                   "nombre": forms.TextInput(attrs={"list": "lista-nombres", "placeholder": "Ej.: Ponchadora RJ45"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tecnico"].queryset = tecnicos()
        self.fields["tecnico"].required = False
        self.fields["tecnico"].empty_label = "— En bodega (sin responsable) —"
        self.fields["costo_compra"].label = "Precio de compra (unidad)"

    def clean_marca(self):
        from .catalogos import canonica
        return canonica("marcas", self.cleaned_data.get("marca"))


# --- Fase 3: contactos y funcionarios -------------------------------------------------------
class ProveedorForm(_Estilos, BootstrapMixin, forms.ModelForm):
    class Meta:
        from .models import Proveedor
        model = Proveedor
        fields = ["razon_social", "nit", "correo", "telefono", "direccion", "notas", "marcas"]
        widgets = {"notas": forms.Textarea(attrs={"rows": 2}), "marcas": forms.CheckboxSelectMultiple}


class ClienteContactoForm(_Estilos, BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Empresa
        fields = ["razon_social", "nit", "email_contacto", "telefono", "direccion", "ciudad", "activa"]

    def clean_nit(self):
        return (self.cleaned_data.get("nit") or "").strip() or None


AREAS = ["Técnica", "Administrativa", "Sistemas", "Comercial", "Directiva"]


class FuncionarioForm(_Estilos, BootstrapMixin, forms.ModelForm):
    """Datos del funcionario (como «Funcionarios» de Inventario_Global). La contraseña se da en Configuración."""

    ROLES_BASICOS = ["agente", "bodega", "comercial", "central"]

    class Meta:
        model = Usuario
        fields = ["first_name", "last_name", "documento", "cargo", "area", "sede", "rol", "email", "telefono", "is_active"]
        labels = {"first_name": "Nombres", "last_name": "Apellidos", "is_active": "Activo (si no, queda inactivo)"}
        widgets = {"area": forms.TextInput(attrs={"list": "lista-areas"})}

    def __init__(self, *args, editor=None, **kwargs):
        super().__init__(*args, **kwargs)
        for campo in ("first_name", "last_name", "documento"):
            self.fields[campo].required = True
        if not (editor and editor.is_superuser):
            # Solo el superusuario nombra coordinadores o dirección
            self.fields["rol"].choices = [c for c in Usuario.Rol.choices if c[0] in self.ROLES_BASICOS]
            if self.instance.pk and self.instance.rol not in self.ROLES_BASICOS:
                self.fields["rol"].disabled = True
                self.fields["rol"].choices = Usuario.Rol.choices

    def clean_documento(self):
        cedula = "".join(c for c in self.cleaned_data["documento"] if c.isdigit())
        if not cedula:
            raise ValidationError("Escriba la cédula (solo números).")
        if Usuario.objects.filter(documento=cedula).exclude(pk=self.instance.pk).exists():
            raise ValidationError("Ya hay un funcionario con esa cédula.")
        return cedula

    def save(self, commit=True):
        usuario = super().save(commit=False)
        if not usuario.pk:
            base = (usuario.first_name[:1] + usuario.last_name.split()[0]).lower() if usuario.last_name else usuario.first_name.lower()
            import unicodedata
            base = "".join(c for c in unicodedata.normalize("NFKD", base) if c.isalnum() and c.isascii()) or "funcionario"
            nombre, n = base, 1
            while Usuario.objects.filter(username=nombre).exists():
                n += 1
                nombre = f"{base}{n}"
            usuario.username = nombre
            usuario.set_unusable_password()  # sin acceso hasta que se le asigne contraseña
        if commit:
            usuario.save()
        return usuario


class OrdenCompraForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = OrdenCompra
        fields = ["proveedor", "fecha_estimada", "notas", "soporte"]
        widgets = {"fecha_estimada": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "notas": forms.Textarea(attrs={"rows": 2, "placeholder": "Forma de pago, entrega en bodega, garantía…"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["proveedor"].empty_label = "— Elija el proveedor —"
        self.fields["soporte"].widget.attrs["accept"] = ".pdf,image/*"
        self._aplicar_estilos()


class LineaOrdenForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = LineaOrdenCompra
        fields = ["descripcion", "cantidad", "adicional", "valor_unitario", "iva"]
        widgets = {"cantidad": forms.NumberInput(attrs={"step": "any", "min": "0"}),
                   "adicional": forms.NumberInput(attrs={"step": "any", "min": "0"}),
                   "valor_unitario": forms.NumberInput(attrs={"step": "any", "min": "0"}),
                   "iva": forms.NumberInput(attrs={"step": "any", "min": "0", "max": "100"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cantidad"].required = self.fields["adicional"].required = False
        self._aplicar_estilos()

    def clean(self):
        d = super().clean()
        d["cantidad"], d["adicional"] = d.get("cantidad") or Decimal(0), d.get("adicional") or Decimal(0)
        if d.get("descripcion") and not d.get("DELETE") and d["cantidad"] + d["adicional"] <= 0:
            self.add_error("adicional", "Escriba cuánto se compra.")
        return d


def formset_orden():
    return forms.inlineformset_factory(OrdenCompra, LineaOrdenCompra, form=LineaOrdenForm, extra=2, can_delete=True)
