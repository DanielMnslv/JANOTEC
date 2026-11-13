import os

from django import forms
from django.conf import settings

from accounts.models import Usuario
from tickets.forms import BootstrapMixin, ConSugerencias, validar_correos
from tickets.models import Ticket

from .models import Actividad, ConfiguracionEnvio, Contacto, DocumentoPersonal, ReporteHorasExtra


def tecnicos_queryset():
    """Quienes pueden ir a una visita: técnicos, coordinadores y quien tenga «también técnico»."""
    return Usuario.tecnicos().order_by("first_name", "username")


class ActividadForm(BootstrapMixin, forms.ModelForm):
    ticket = forms.ModelChoiceField(queryset=Ticket.objects.all(), required=False, widget=forms.HiddenInput)
    # Viáticos: no son de la actividad sino de la solicitud que le llega a financiera (cronograma/viaticos.py)
    via_sst = forms.BooleanField(label="Viáticos SST / SGI", required=False)
    via_tecnicos = forms.BooleanField(label="Viáticos técnicos", required=False)
    via_alimentacion = forms.BooleanField(label="Alimentación", required=False)
    via_transporte = forms.BooleanField(label="Transporte", required=False)
    via_gasolina = forms.BooleanField(label="Gasolina", required=False)
    via_otros = forms.BooleanField(label="Otros", required=False)
    via_detalle = forms.CharField(label="Detalle", required=False, max_length=300,
                                  widget=forms.TextInput(attrs={"placeholder": "Ej.: peajes, hospedaje una noche"}))

    class Meta:
        model = Actividad
        fields = ["ticket", "fecha", "hora_inicio", "hora_fin", "cliente", "sede", "actividad", "tipo", "linea",
                  "encargados", "insumos", "notificar_a", "trabajo_alturas", "sst", "sst_persona",
                  "sst_trabajo", "sst_duracion", "vehiculo"]
        widgets = {
            "fecha": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "hora_inicio": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "hora_fin": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "actividad": forms.Textarea(attrs={"rows": 3}),
            "insumos": forms.Textarea(attrs={"rows": 2, "placeholder": "Busque arriba en el inventario o escriba: 3 batería 12V 7.5 A, 1 PIR sensor, 20 m UTP"}),
            "notificar_a": forms.TextInput(attrs={"placeholder": "correo@cliente.com; otro@cliente.com"}),
            "encargados": forms.CheckboxSelectMultiple,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import Empresa, Sede

        self.fields["cliente"].widget = ConSugerencias(
            Empresa.objects.filter(activa=True).order_by("razon_social").values_list("razon_social", flat=True),
            attrs={"placeholder": "Escriba el cliente…"})
        self.fields["sede"].widget = ConSugerencias(
            Sede.objects.filter(activa=True).order_by("nombre").values_list("nombre", flat=True).distinct(),
            attrs={"placeholder": "Escriba la sede…"})
        self.fields["linea"].widget = ConSugerencias(Actividad.LINEAS, attrs={"placeholder": "CCTV / videovigilancia…"})
        personas = (Actividad.objects.exclude(sst_persona="").order_by().values_list("sst_persona", flat=True).distinct()[:50])
        self.fields["sst_persona"].widget = ConSugerencias(personas, attrs={"placeholder": "Ej.: Javier Jaramillo"})
        self.fields["encargados"].queryset = tecnicos_queryset()
        self.fields["sst_duracion"].required = False
        self.fields["notificar_a"].help_text = "Reciben la autorización de ingreso. Sin correo, la actividad queda en rojo."
        self._aplicar_estilos()
        self.fields["encargados"].widget.attrs["class"] = "form-check-input"

    def clean_notificar_a(self):
        return " ; ".join(validar_correos(self.cleaned_data["notificar_a"]))

    def clean_via_detalle(self):
        return self.cleaned_data.get("via_detalle", "").strip()

    def clean(self):
        datos = super().clean()
        inicio, fin = datos.get("hora_inicio"), datos.get("hora_fin")
        if inicio and fin and fin <= inicio:
            self.add_error("hora_fin", "La hora de fin debe ser posterior a la de inicio.")
        ticket = datos.get("ticket") or (self.instance.ticket if self.instance.pk else None)
        if ticket is None:
            # Actividad interna: sin cliente a quien pedir autorización ni avisar
            datos["tipo"], datos["notificar_a"] = Actividad.Tipo.INTERNA, ""
        elif datos.get("tipo") == Actividad.Tipo.INTERNA:
            self.add_error("tipo", "Una visita de un ticket no puede ser «Actividad interna»: elija el tipo de servicio.")
        ticket = datos.get("ticket")
        if ticket and self.instance.pk is None and ticket.estado not in Ticket.ESTADOS_PROGRAMABLES:
            self.add_error(None, f"{ticket.radicado} está «{ticket.get_estado_display()}»: solo se programan "
                                 "tickets recepcionados o pendientes de nueva visita.")
        if not datos.get("sst_duracion"):
            datos["sst_duracion"] = "medio día"
        if (datos.get("via_sst") or datos.get("via_tecnicos")) and not any(
                datos.get(f"via_{c}") for c in ("alimentacion", "transporte", "gasolina", "otros")):
            self.add_error(None, "Viáticos: marque al menos un concepto (alimentación, transporte, gasolina u otros).")
        if datos.get("via_otros") and not datos.get("via_detalle"):
            self.add_error(None, "Viáticos: escriba en el detalle cuáles son los otros conceptos.")
        return datos


EXTENSIONES_DOCUMENTO = ("pdf", "jpg", "jpeg", "png")


class DocumentoForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = DocumentoPersonal
        fields = ["tipo", "archivo", "vigente_hasta"]
        widgets = {"vigente_hasta": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "tipo": forms.HiddenInput}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["archivo"].widget.attrs["accept"] = ",".join("." + e for e in EXTENSIONES_DOCUMENTO)
        self._aplicar_estilos()

    def clean_archivo(self):
        archivo = self.cleaned_data["archivo"]
        ext = os.path.splitext(archivo.name)[1].lower().lstrip(".")
        if ext not in EXTENSIONES_DOCUMENTO:
            raise forms.ValidationError("Suba el documento en PDF, JPG o PNG.")
        if archivo.size > settings.ADJUNTO_MAX_MB * 1024 * 1024:
            raise forms.ValidationError(f"El archivo supera el máximo de {settings.ADJUNTO_MAX_MB} MB.")
        return archivo

    def clean(self):
        datos = super().clean()
        if datos.get("tipo") in (DocumentoPersonal.Tipo.SEGURIDAD_SOCIAL, DocumentoPersonal.Tipo.ALTURAS) \
                and not datos.get("vigente_hasta"):
            self.add_error("vigente_hasta", "Indique hasta cuándo está vigente: así la plataforma avisa cuando venza.")
        return datos


class ContactoForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Contacto
        fields = ["nombre", "email", "tipo"]
        widgets = {"email": forms.EmailInput(attrs={"placeholder": "nombre@correo.com"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["tipo"].choices = [(v, e.split(" (")[0]) for v, e in Contacto.Tipo.choices]
        self._aplicar_estilos()


class ConfiguracionEnvioForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = ConfiguracionEnvio
        fields = ["envio_automatico", "hora", "semana_objetivo", "remitente", "avisar_contactos"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()


class ImportarForm(forms.Form):
    archivo = forms.FileField(label="Respaldo (.json) de la plataforma anterior o de esta",
                              widget=forms.ClearableFileInput(attrs={"accept": "application/json,.json", "class": "form-control"}))

    def clean_archivo(self):
        import json

        archivo = self.cleaned_data["archivo"]
        if archivo.size > 5 * 1024 * 1024:
            raise forms.ValidationError("El respaldo supera 5 MB.")
        try:
            datos = json.loads(archivo.read().decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            raise forms.ValidationError("El archivo no es un respaldo JSON válido.")
        if not isinstance(datos, dict) or not isinstance(datos.get("rows"), list):
            raise forms.ValidationError("El archivo no tiene actividades («rows»).")
        return datos


class ReporteHorasExtraForm(BootstrapMixin, forms.ModelForm):
    """Primer momento de las horas extras: el inicio.

    * En el momento (``tardio=False``): la fecha y la hora las pone la plataforma; la foto es obligatoria y el
      celular envía la ubicación en campos ocultos (ver ``cronograma/horas_validacion.py``).
    * Tardío: el técnico escribe la fecha y la hora reales y por qué no lo registró a tiempo; la foto es opcional.
    """
    motivo = forms.CharField(label="¿Por qué no lo registró en el momento?", required=False, max_length=500,
                             widget=forms.Textarea(attrs={"rows": 2}))

    class Meta:
        model = ReporteHorasExtra
        fields = ["colaborador", "rol", "fecha", "hora_salida", "cliente", "evidencia_salida"]
        widgets = {
            "rol": forms.RadioSelect,
            "fecha": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "hora_salida": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "evidencia_salida": forms.ClearableFileInput(attrs={"accept": "image/*", "capture": "environment"}),
        }

    def __init__(self, *args, usuario=None, tardio=False, **kwargs):
        super().__init__(*args, **kwargs)
        from accounts.models import Empresa

        self.tardio = tardio
        if usuario.es_supervisor:  # el coordinador puede registrar por otro técnico (queda por validar)
            self.fields["colaborador"].queryset = tecnicos_queryset()
            self.fields["colaborador"].initial = usuario.pk if tecnicos_queryset().filter(pk=usuario.pk).exists() else None
        else:
            del self.fields["colaborador"]
        self.fields["rol"].required = True
        self.fields["rol"].choices = ReporteHorasExtra.Rol.choices
        self.fields["cliente"].widget = ConSugerencias(Empresa.objects.filter(activa=True).order_by("razon_social").values_list("razon_social", flat=True),
                                                       attrs={"placeholder": "Escriba o elija el cliente"})
        if tardio:
            self.fields["evidencia_salida"].required = False
            self.fields["motivo"].required = True
            self.fields["hora_salida"].label = "Hora real de inicio"
        else:  # la fecha y la hora las pone el servidor
            del self.fields["fecha"], self.fields["hora_salida"], self.fields["motivo"]
            self.fields["evidencia_salida"].required = True
        self._aplicar_estilos()

    def clean(self):
        d = super().clean()
        if self.tardio and d.get("fecha") and d.get("hora_salida"):
            from .horas_validacion import hora_valida_tardia

            error = hora_valida_tardia(d["fecha"], d["hora_salida"])
            if error:
                self.add_error("hora_salida", error)
        if self.tardio and len((d.get("motivo") or "").strip()) < 10:
            self.add_error("motivo", "Explique en una frase por qué no lo registró en el momento.")
        return d


class CierreHorasExtraForm(BootstrapMixin, forms.ModelForm):
    """Segundo momento: el regreso. En el momento solo pide la foto (la hora es la del servidor); tardío pide la hora
    real de llegada y el motivo."""
    motivo = forms.CharField(label="¿Por qué no lo registró en el momento?", required=False, max_length=500,
                             widget=forms.Textarea(attrs={"rows": 2}))

    class Meta:
        model = ReporteHorasExtra
        fields = ["hora_llegada", "evidencia_llegada"]
        widgets = {
            "hora_llegada": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "evidencia_llegada": forms.ClearableFileInput(attrs={"accept": "image/*", "capture": "environment"}),
        }

    def __init__(self, *args, tardio=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.tardio = tardio
        if tardio:
            self.fields["hora_llegada"].required = True
            self.fields["hora_llegada"].label = "Hora real de llegada"
            self.fields["evidencia_llegada"].required = False
            self.fields["motivo"].required = True
        else:
            del self.fields["hora_llegada"], self.fields["motivo"]
            self.fields["evidencia_llegada"].required = True
        self._aplicar_estilos()

    def clean(self):
        d = super().clean()
        if self.tardio and len((d.get("motivo") or "").strip()) < 10:
            self.add_error("motivo", "Explique en una frase por qué no lo registró en el momento.")
        return d
