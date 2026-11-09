"""Formularios de la plataforma de tickets.

Organización del archivo:
- Adjuntos: campo de varios archivos con límite de tamaño y extensiones, y estilos de Bootstrap.
- Disponibilidad del cliente: días, franjas y rangos de fechas en que no puede atender.
- Radicación: el formulario con que cliente, central o coordinación radican (o editan) un ticket.
- Respuesta, calificación y gestión del ticket por el área técnica.
- Filtros de las bandejas de tickets y de actas.
- Acta de soporte: datos de la visita, evidencias, firmas y equipos intervenidos, utilizados o retirados.
"""

import os

from django import forms
from django.conf import settings
from django.db import models
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario

from .models import (
    DIAS_LABORALES, FRANJAS_NO_DISPONIBLE, FRANJAS_SABADO, ActaSoporte, Aplazamiento, Categoria, InformeBorrador, OportunidadComercial,
    Ticket, TipoFalla, leer_franja,
)


# --- Carga múltiple de archivos (patrón oficial de Django 5) ------------------
class MultipleFileInput(forms.ClearableFileInput):
    """Selector de archivos que permite elegir varios a la vez."""
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    """Campo de varios adjuntos. Opcional por defecto; valida tamaño y extensión de cada archivo según
    ADJUNTO_MAX_MB y ADJUNTO_EXTENSIONES de la configuración.
    """
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"class": "form-control"}))
        kwargs.setdefault("required", False)
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        """Devuelve la lista de archivos válidos (vacía si no se subió nada) o el error del primero que no cumple."""
        if not data:
            return []
        archivos = data if isinstance(data, (list, tuple)) else [data]
        limpios = [super(MultipleFileField, self).clean(a, initial) for a in archivos]
        maximo = settings.ADJUNTO_MAX_MB * 1024 * 1024
        permitidas = {e.lower() for e in settings.ADJUNTO_EXTENSIONES}
        if len(limpios) > 5:
            raise forms.ValidationError("Puede adjuntar máximo 5 archivos a la vez.")
        for a in limpios:
            ext = os.path.splitext(a.name)[1].lower().lstrip(".")
            if ext not in permitidas:
                raise forms.ValidationError(
                    f"«{a.name}»: tipo de archivo no permitido. Permitidos: {', '.join(sorted(permitidas))}."
                )
            if a.size > maximo:
                raise forms.ValidationError(
                    f"«{a.name}» supera el máximo de {settings.ADJUNTO_MAX_MB} MB."
                )
        return limpios


def ayuda_adjuntos():
    """Texto de ayuda de los adjuntos, con el tamaño máximo y las extensiones permitidas."""
    return (
        f"Opcional. Hasta 5 archivos de máximo {settings.ADJUNTO_MAX_MB} MB "
        f"({', '.join(settings.ADJUNTO_EXTENSIONES)})."
    )


class BootstrapMixin:
    """Agrega las clases de Bootstrap a todos los campos."""

    def _aplicar_estilos(self):
        """Pone a cada campo la clase de Bootstrap que le corresponde (form-select, form-check-input o
        form-control) sin pisar la que ya tenga.
        """
        for campo in self.fields.values():
            w = campo.widget
            if isinstance(w, (forms.Select,)):
                w.attrs.setdefault("class", "form-select")
            elif isinstance(w, forms.CheckboxInput):
                w.attrs.setdefault("class", "form-check-input")
            elif isinstance(w, forms.RadioSelect):
                continue
            else:
                w.attrs.setdefault("class", "form-control")


# --- Radicación (paso 1 del flujo) ------------------------------------------------
class RangoNoDisponibleForm(forms.Form):
    """Una fila de «Rangos de fecha y hora en que NO tiene disponibilidad»."""

    desde = forms.DateTimeField(
        label="Desde", required=False, input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}, format="%Y-%m-%dT%H:%M"),
    )
    hasta = forms.DateTimeField(
        label="Hasta", required=False, input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local", "class": "form-control"}, format="%Y-%m-%dT%H:%M"),
    )

    def clean(self):
        """El rango necesita inicio y fin, y el fin debe ser posterior al inicio."""
        datos = super().clean()
        desde, hasta = datos.get("desde"), datos.get("hasta")
        if bool(desde) != bool(hasta):
            raise forms.ValidationError("Indique la fecha y hora de inicio y de fin del rango.")
        if desde and hasta and hasta <= desde:
            raise forms.ValidationError("El fin del rango debe ser posterior al inicio.")
        return datos


RangoNoDisponibleFormSet = forms.formset_factory(RangoNoDisponibleForm, extra=1, max_num=8, validate_max=True)


def agregar_dias_no_disponible(form):
    """Un selector por día laboral (lunes a sábado): mañana, tarde o todo el día. El sábado solo hay mañana."""
    for clave, etiqueta in DIAS_LABORALES:
        sabado = clave == "sabado"
        form.fields[f"nd_{clave}"] = forms.ChoiceField(
            label="Sábado (8 a 12 m.)" if sabado else etiqueta, required=False,
            choices=[("", "Puede atender")] + (FRANJAS_SABADO if sabado else FRANJAS_NO_DISPONIBLE))


def iniciales_no_disponible(form, no_disponible):
    """Carga en el formulario los días en que el cliente no puede atender (todo el día, mañana o tarde).

    El sábado solo se trabaja en la mañana, así que cualquier restricción del sábado se muestra como todo el día.
    Las franjas por horas no tienen casilla y se conservan como están.
    """
    for clave, valor in (no_disponible or {}).items():
        tipo = leer_franja(valor)[0]
        if clave == "sabado" and tipo:
            tipo = "todo"  # el sábado solo se trabaja en la mañana
        if f"nd_{clave}" in form.fields and tipo != "horas":
            form.initial[f"nd_{clave}"] = tipo


def leer_no_disponible(form, datos):
    """{día: 'manana'|'tarde'|'todo'} con lo marcado de lunes a sábado."""
    return {clave: datos[f"nd_{clave}"] for clave, _ in DIAS_LABORALES if datos.get(f"nd_{clave}")}


def dias_para_plantilla(form):
    """Los campos «no disponible» de lunes a sábado, en orden, para dibujarlos en la plantilla."""
    return [form[f"nd_{c}"] for c, _ in DIAS_LABORALES]


def validar_correos(texto):
    """«a@x.co; b@y.co» → lista validada (acepta «,» o «;»)."""
    from django.core.validators import validate_email

    correos = [c.strip() for c in (texto or "").replace(",", ";").split(";") if c.strip()]
    for c in correos:
        try:
            validate_email(c)
        except forms.ValidationError:
            raise forms.ValidationError(f"«{c}» no es un correo válido.")
    return correos


class ConSugerencias(forms.TextInput):
    """Campo para escribir que sugiere opciones mientras se escribe, sin limitar a la lista.

    Si la novedad del cliente no está entre las opciones, la escribe tal cual. Las opciones son
    `(valor, ayuda)` o solo `valor`; la plantilla puede cambiarlas con JavaScript (lista dependiente).
    """

    def __init__(self, opciones=(), attrs=None):
        super().__init__(attrs)
        self.opciones = list(opciones)

    def render(self, name, value, attrs=None, renderer=None):
        """Dibuja la caja de texto con su lista de sugerencias (datalist): se puede elegir de la lista o escribir libre."""
        from django.utils.html import escape
        from django.utils.safestring import mark_safe

        attrs = dict(attrs or {})
        lista = f"sug-{attrs.get('id', name)}"
        attrs.update(list=lista, autocomplete="off")
        opciones = []
        for o in self.opciones:
            valor, ayuda = o if isinstance(o, (list, tuple)) else (o, "")
            opciones.append(f'<option value="{escape(valor)}">{escape(ayuda)}</option>' if ayuda
                            else f'<option value="{escape(valor)}"></option>')
        return mark_safe(super().render(name, value, attrs, renderer) + f'<datalist id="{lista}">{"".join(opciones)}</datalist>')


MUNICIPIOS_META = [
    "Villavicencio", "Acacías", "Barranca de Upía", "Cabuyaro", "Castilla la Nueva", "Cubarral", "Cumaral", "El Calvario",
    "El Castillo", "El Dorado", "Fuente de Oro", "Granada", "Guamal", "La Macarena", "Lejanías", "Mapiripán",
    "Mesetas", "Puerto Concordia", "Puerto Gaitán", "Puerto Lleras", "Puerto López", "Puerto Rico", "Restrepo",
    "San Carlos de Guaroa", "San Juan de Arama", "San Juanito", "San Martín", "Uribe", "Vista Hermosa",
]
# Centro aproximado de cada municipio del Meta (latitud, longitud): «Usar mi ubicación» sugiere el más cercano
COORDENADAS_META = {
    "Villavicencio": (4.142, -73.627), "Acacías": (3.987, -73.758), "Barranca de Upía": (4.567, -72.966),
    "Cabuyaro": (4.287, -72.792), "Castilla la Nueva": (3.829, -73.688), "Cubarral": (3.794, -73.839),
    "Cumaral": (4.271, -73.487), "El Calvario": (4.352, -73.713), "El Castillo": (3.563, -73.794),
    "El Dorado": (3.740, -73.835), "Fuente de Oro": (3.462, -73.621), "Granada": (3.547, -73.706),
    "Guamal": (3.880, -73.766), "La Macarena": (2.183, -73.785), "Lejanías": (3.527, -74.023),
    "Mapiripán": (2.889, -72.134), "Mesetas": (3.383, -74.044), "Puerto Concordia": (2.623, -72.762),
    "Puerto Gaitán": (4.314, -72.082), "Puerto Lleras": (3.270, -73.375), "Puerto López": (4.085, -72.957),
    "Puerto Rico": (2.938, -73.208), "Restrepo": (4.258, -73.565), "San Carlos de Guaroa": (3.711, -73.243),
    "San Juan de Arama": (3.374, -73.875), "San Juanito": (4.458, -73.677), "San Martín": (3.696, -73.698),
    "Uribe": (3.240, -74.352), "Vista Hermosa": (3.124, -73.751),
}
DEPARTAMENTOS = [
    "Meta", "Casanare", "Vichada", "Guaviare", "Cundinamarca", "Bogotá D.C.", "Arauca", "Caquetá", "Boyacá", "Tolima",
    "Huila", "Antioquia", "Santander", "Valle del Cauca", "Atlántico", "Bolívar", "Nariño", "Cauca", "Córdoba",
    "Norte de Santander", "Risaralda", "Caldas", "Quindío", "Sucre", "Cesar", "Magdalena", "La Guajira", "Chocó",
    "Putumayo", "Amazonas", "Guainía", "Vaupés", "San Andrés y Providencia",
]


def municipios_sugeridos():
    """Los del Meta y los que ya existen en las sedes (sin repetir)."""
    vistos = {m.lower(): m for m in MUNICIPIOS_META}
    for c in Sede.objects.exclude(ciudad="").values_list("ciudad", flat=True).distinct()[:300]:
        vistos.setdefault(c.strip().lower(), c.strip())
    return sorted(vistos.values())


# --- Lo que entiende el cliente ↔ los términos técnicos -------------------------------
# Palabras con las que el cliente nombra cada sistema (sin tildes, en minúscula)
ALIAS_SISTEMA = [
    (("camara", "cctv", "video", "dvr", "nvr", "grabador", "grabacion"), "Mantenimiento CCTV IP"),
    (("alarma", "sensor", "sirena", "intrusion", "pir"), "Alarma de intrusión"),
    (("radio", "repetidora", "antena", "walkie"), "Radiocomunicaciones"),
    (("internet", "red", "wifi", "switch", "fibra", "cableado", "router"), "Fibra óptica / red"),
    (("solar", "panel", "inversor", "bateria", "fotovolt"), "Energía fotovoltaica"),
    (("acceso", "puerta", "biometr", "lector", "talanquera", "cerradura", "huella"), "Control de acceso"),
    (("diagnostic", "no se que", "revisar todo"), "Diagnóstico técnico"),
]
SISTEMA_DESCONOCIDO = "Diagnóstico técnico"


def _sin_tildes(texto):
    """El texto en minúsculas, sin tildes y con los espacios normalizados, para comparar lo que escribe la gente."""
    import unicodedata

    texto = unicodedata.normalize("NFD", (texto or "").lower())
    return " ".join("".join(c for c in texto if unicodedata.category(c) != "Mn").split())


def es_no_se(texto):
    """Si el cliente escribió que no sabe («No sé», «no estoy seguro», «?»…); ese dato queda por confirmar."""
    return _sin_tildes(texto) in ("no se", "nose", "no estoy seguro", "no sabe", "ns", "?")


def sistema_por_alias(texto, tipo=""):
    """«Cámaras», «el internet», «paneles»… → el sistema a intervenir (o None si no se reconoce).

    Las cámaras son «Instalación CCTV IP» si piden instalar o ampliar, y «Mantenimiento CCTV IP» si no.
    """
    t = _sin_tildes(texto)
    for palabras, nombre in ALIAS_SISTEMA:
        if any(p in t for p in palabras):
            if nombre == "Mantenimiento CCTV IP" and ("instal" in t or tipo == Ticket.Tipo.REQUERIMIENTO):
                nombre = "Instalación CCTV IP"
            return Categoria.objects.filter(nombre=nombre).first()
    return None


def _por_nombre(queryset, campo, texto):
    """El objeto cuyo `campo` coincide con lo escrito (sin importar mayúsculas ni espacios de más)."""
    texto = " ".join((texto or "").split())
    if not texto:
        return None
    return queryset.filter(**{f"{campo}__iexact": texto}).first()


class RadicacionForm(BootstrapMixin, forms.ModelForm):
    """Registro de una solicitud de soporte técnico, mantenimiento o toma de requerimientos.

    La radican el cliente (a nombre de su empresa) y, a nombre de cualquier cliente, el personal JANOTEC,
    la central de atención y los ejecutivos comerciales. Con `instance` lo usa el área técnica para
    corregir cualquier campo al recepcionar.

    Empresa, sede, sistema y falla se escriben con sugerencias: si lo que necesita el cliente no está
    en la lista, se escribe tal cual (la sede nueva se crea; el sistema y la falla quedan escritos).
    """

    archivos = MultipleFileField(label="Evidencia de la novedad (fotos o video)")
    alturas = forms.ChoiceField(label="Trabajo seguro en alturas (supera los 2 metros a nivel del pie)", required=False,
                                choices=[("si", "Sí"), ("no", "No")], widget=forms.RadioSelect)
    sst = forms.ChoiceField(label="¿Requiere acompañamiento SST (experto en seguridad y salud en el trabajo)?", required=False,
                            choices=[("si", "Sí"), ("no", "No")], widget=forms.RadioSelect)
    latitud = forms.DecimalField(max_digits=10, decimal_places=7, required=False, widget=forms.HiddenInput)
    longitud = forms.DecimalField(max_digits=10, decimal_places=7, required=False, widget=forms.HiddenInput)
    empresa_nombre = forms.CharField(label="Empresa", max_length=200, required=False)
    sede_nombre = forms.CharField(label="Sede", max_length=150, required=False)
    sistema = forms.CharField(label="Sistema a intervenir", max_length=150, required=False)
    falla_nombre = forms.CharField(label="Falla o necesidad (opcional)", max_length=200, required=False)
    # Como en un correo: «Para» (contacto_email, el principal) y «CC» (estas copias). Se guardan juntos en
    # contacto_email, el principal primero: «principal ; copia1 ; copia2».
    contacto_copias = forms.CharField(
        label="Con copia a (opcional)", max_length=400, required=False,
        help_text="Reciben copia de las respuestas y también pueden autorizar la visita. Separe varios con «;».",
        widget=forms.TextInput(attrs={"placeholder": "Ej.: jefe@suempresa.com; compras@suempresa.com", "inputmode": "email"}))

    class Meta:
        model = Ticket
        fields = [
            "contacto_nombre", "contacto_telefono", "contacto_email", "departamento", "municipio", "direccion",
            "punto_referencia", "tipo", "asunto", "descripcion", "impacto", "jornada", "restricciones",
        ]
        labels = {
            "tipo": "Tipo de ticket",
            "asunto": "Asunto",
            "descripcion": "Descripción de la novedad",
            "impacto": "Nivel de urgencia",
            "contacto_nombre": "Nombre de quien gestiona",
            "contacto_telefono": "Teléfono de contacto",
            "contacto_email": "Correo principal de quien gestiona",
            "punto_referencia": "Punto de referencia (opcional)",
            "restricciones": "Otras restricciones (opcional)",
        }
        widgets = {
            "tipo": forms.RadioSelect,
            "impacto": forms.RadioSelect,
            "jornada": forms.RadioSelect,
            "asunto": forms.TextInput(attrs={"placeholder": "Ej.: Cámara de la portería sin imagen"}),
            "punto_referencia": forms.TextInput(attrs={"placeholder": "Ej.: frente al parque, portón verde, bodega 3"}),
            "descripcion": forms.Textarea(attrs={
                "rows": 5,
                "placeholder": "Qué ocurre, qué equipos o zonas están afectados, desde cuándo, si ya intentó algo…",
            }),
            "contacto_telefono": forms.TextInput(attrs={"placeholder": "Ej.: 310 000 0000", "inputmode": "tel"}),
            "contacto_email": forms.TextInput(attrs={"placeholder": "Ej.: admin@suempresa.com",
                                                     "inputmode": "email"}),
            "restricciones": forms.TextInput(attrs={"placeholder": "Ej.: festivos no hay personal"}),
        }

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        edicion = bool(self.instance.pk)
        # Solo quien radica para clientes (o corrige el ticket) elige la empresa
        self.elige_empresa = bool(usuario and (usuario.radica_para_clientes or edicion and usuario.es_supervisor))
        if self.elige_empresa:
            self.empresas = Empresa.objects.filter(activa=True).order_by("razon_social")
            self.sedes = Sede.objects.filter(activa=True)
            self.fields["empresa_nombre"].widget = ConSugerencias(
                [(e.razon_social, f"NIT {e.nit}" if e.nit else "") for e in self.empresas],
                attrs={"placeholder": "Escriba el nombre de la empresa…"})
        else:
            del self.fields["empresa_nombre"]
            self.empresas = Empresa.objects.none()
            self.sedes = Sede.objects.filter(empresa_id=usuario.empresa_id if usuario else None, activa=True)
        self.fields["sede_nombre"].widget = ConSugerencias(
            [] if self.elige_empresa else [s.nombre for s in self.sedes.order_by("nombre")],
            attrs={"placeholder": "Escriba o elija la sede…"})
        self.fields["sede_nombre"].help_text = "Si la sede no está en la lista, escríbala: se registra nueva."
        self.categorias = Categoria.objects.filter(activa=True)
        self.fields["sistema"].widget = ConSugerencias(
            [c.nombre for c in self.categorias], attrs={"placeholder": "Mantenimiento CCTV IP, Alarma de intrusión…"})
        self.fields["sistema"].help_text = "Elija uno de la lista o escriba el sistema si no está."
        self.fallas = TipoFalla.objects.filter(activa=True, categoria__activa=True)
        self.fields["falla_nombre"].widget = ConSugerencias(
            [f.nombre for f in self.fallas.order_by("orden", "nombre")],
            attrs={"placeholder": "Escriba la falla con sus palabras o elija una sugerencia…"})
        self.fields["tipo"].choices = [c for c in Ticket.Tipo.choices if c[0] != Ticket.Tipo.COTIZACION]
        if edicion and self.instance.tipo == Ticket.Tipo.COTIZACION:
            self.fields["tipo"].choices = list(Ticket.Tipo.choices)
        # El nivel de urgencia lo define solo el coordinador técnico (el cliente no conoce los tiempos de respuesta)
        self.define_urgencia = bool(usuario and usuario.es_coordinador)
        if self.define_urgencia:
            self.fields["impacto"].choices = list(Ticket.Impacto.choices)
            self.fields["impacto"].required = True
        else:
            del self.fields["impacto"]
        # La disponibilidad se indica solo con los días en que no pueden recibir al personal
        for campo in ("jornada", "restricciones"):
            del self.fields[campo]
        # La falla o necesidad es el asunto del ticket: se escribe una vez
        del self.fields["asunto"]
        self.fields["falla_nombre"].required = True
        self.fields["falla_nombre"].label = "Falla o necesidad"
        self.fields["falla_nombre"].help_text = "Será el asunto de la solicitud. Elija una sugerencia o escríbala con sus palabras."
        # El cliente responde con sus palabras (y puede decir «No sé»); el área técnica confirma al recepcionar
        self.modo_cliente = bool(usuario and usuario.es_cliente and not edicion)
        if self.modo_cliente:
            self.fields["sistema"].help_text = ("Elija de la lista o escríbalo con sus palabras. Si no sabe cuál es, "
                                                "elija «Diagnóstico técnico»: nosotros lo confirmamos.")
        self.fields["departamento"].widget = ConSugerencias(DEPARTAMENTOS, attrs={"placeholder": "Ej.: Meta"})
        self.fields["municipio"].widget = ConSugerencias(municipios_sugeridos(), attrs={"placeholder": "Ej.: Villavicencio"})
        for campo in ("contacto_nombre", "contacto_telefono", "contacto_email", "departamento", "municipio", "direccion",
                      "descripcion"):
            self.fields[campo].required = True
        # Obligatorios que se validan aparte (escritos con sugerencias): se marcan con * en la plantilla
        for campo in ("empresa_nombre", "sede_nombre", "sistema"):
            if campo in self.fields:
                self.fields[campo].widget.attrs["aria_required"] = "true"
        self.fields["direccion"].help_text = "Escríbala completa (calle, número, barrio o vereda): un error puede impedir la visita."
        self.fields["contacto_email"].help_text = "A este correo le enviamos las respuestas. Para copiar a otras personas, use «Con copia a»."
        self.fields["archivos"].help_text = ayuda_adjuntos()
        self.fields["archivos"].widget.attrs.update({"accept": "image/*,video/*,.pdf"})
        agregar_dias_no_disponible(self)
        if edicion or not self.is_bound:  # en la edición también al recibir el envío: así se sabe qué cambió
            self._valores_iniciales(edicion)
        prefijo = "rangos"
        # Sin la sección de rangos en el envío (otro formulario o integración) no hay rangos que guardar
        self.sin_rangos = self.is_bound and f"{prefijo}-TOTAL_FORMS" not in self.data
        if self.is_bound and not self.sin_rangos:
            self.rangos = RangoNoDisponibleFormSet(self.data, prefix=prefijo)
        else:
            inicial = [{"desde": timezone.localtime(r.desde), "hasta": timezone.localtime(r.hasta)}
                       for r in self.instance.rangos_no_disponible.all()] if edicion else []
            self.rangos = RangoNoDisponibleFormSet(prefix=prefijo, initial=inicial)
        self._aplicar_estilos()

    def _valores_iniciales(self, edicion):
        """Valores iniciales del formulario: al editar, los datos del ticket en los campos de texto libre (empresa,
        sede, sistema, falla, alturas, SST, ubicación); al radicar, el tipo «soporte» y, si radica un cliente, él mismo
        como contacto y los datos de su última solicitud.
        """
        if edicion:
            t = self.instance
            self.initial.update(empresa_nombre=t.empresa.razon_social if t.empresa_id else "",
                                sede_nombre=t.sede.nombre if t.sede_id else "", sistema=t.sistema_display,
                                falla_nombre=t.falla_display, alturas="si" if t.trabajo_alturas else "no",
                                sst="si" if t.acompanamiento_sst else "no", latitud=t.latitud, longitud=t.longitud)
            iniciales_no_disponible(self, t.no_disponible)
            self._separar_correos(t.contacto_email)
            return
        self.initial.setdefault("tipo", Ticket.Tipo.SOPORTE)
        u = self.usuario
        if u and u.es_cliente:
            # Por defecto el contacto es quien radica; puede cambiarlo si otra persona atiende la visita.
            self.initial.setdefault("contacto_nombre", u.get_full_name() or u.username)
            self.initial.setdefault("contacto_telefono", u.telefono)
            self.initial.setdefault("contacto_email", u.email)
            ultimo = Ticket.objects.filter(empresa_id=u.empresa_id).order_by("-creado").first()
            if ultimo:  # lo que indicó la última vez, para no escribirlo cada vez
                self._separar_correos(ultimo.contacto_email or u.email)
                iniciales_no_disponible(self, ultimo.no_disponible)
            if self.sedes.count() == 1:
                sede = self.sedes.first()
                self.initial.update(sede_nombre=sede.nombre, departamento=sede.departamento, municipio=sede.ciudad,
                                    direccion=sede.direccion)

    def _separar_correos(self, texto):
        """«principal ; copia1 ; copia2» guardado → el principal en su campo y las copias en «Con copia a»."""
        correos = [c.strip() for c in (texto or "").replace(",", ";").split(";") if c.strip()]
        self.initial["contacto_email"] = correos[0] if correos else ""
        self.initial["contacto_copias"] = "; ".join(correos[1:])

    # --- Ayudas para la plantilla -----------------------------------------------------
    def opciones_tipo(self):
        """Cada tipo de solicitud con su explicación, para mostrarlas como tarjetas."""
        return [(op, Ticket.AYUDA_TIPO[op.data["value"]]) for op in self["tipo"]]

    def opciones_impacto(self):
        """Cada nivel de impacto con su explicación, para mostrarlo como tarjetas."""
        return [(op, Ticket.AYUDA_IMPACTO[op.data["value"]]) for op in self["impacto"]]

    def dias_no_disponible(self):
        """Los días de la semana en que el cliente puede marcar que no atiende."""
        return dias_para_plantilla(self)

    def municipios_coordenadas(self):
        """Municipios del Meta con sus coordenadas, para ubicar el mapa al elegir la ciudad."""
        return [[nombre, lat, lon] for nombre, (lat, lon) in COORDENADAS_META.items()]

    def fallas_por_servicio(self):
        """{nombre del sistema en minúscula: [fallas]}: las sugerencias de falla según el sistema escrito."""
        mapa = {}
        for f in self.fallas.select_related("categoria").order_by("orden", "nombre"):
            mapa.setdefault(f.categoria.nombre.lower(), []).append(f.nombre)
        return mapa

    def sedes_por_empresa(self):
        """{empresa en minúscula: [[sede, departamento, municipio, dirección, contacto, teléfono, correo], ...]}:
        sugiere la sede y llena la ubicación y el contacto de quien recibe."""
        def fila(s):
            """Datos de una sede que se autocompletan al elegirla."""
            return [s.nombre, s.departamento, s.ciudad, s.direccion, s.contacto_nombre, s.contacto_telefono, s.contacto_email]
        mapa = {}
        if self.elige_empresa:
            for s in self.sedes.select_related("empresa").order_by("nombre"):
                mapa.setdefault(s.empresa.razon_social.lower(), []).append(fila(s))
        else:
            mapa[""] = [fila(s) for s in self.sedes.order_by("nombre")]
        return mapa

    def empresas_del_grupo(self):
        """Empresas del grupo JANOTEC (en minúscula): ahí el contacto interno es correcto."""
        return [e.lower() for e in Empresa.objects.filter(es_del_grupo=True).values_list("razon_social", flat=True)]

    @property
    def contacto_es_interno(self):
        """El correo de contacto es del personal JANOTEC y la empresa no es del grupo (los correos no le llegan al cliente)."""
        from config.correo import correos_internos
        d = getattr(self, "cleaned_data", {}) or {}
        empresa = d.get("empresa_final")
        if empresa is None or empresa.es_del_grupo:
            return False
        correos = {c.strip().lower() for c in (d.get("contacto_email") or "").split(";") if c.strip()}
        return bool(correos) and all(c in correos_internos() or c.endswith("@janotec.app") for c in correos)

    # --- Validación -------------------------------------------------------------------
    def clean_contacto_copias(self):
        """Valida los correos en copia (pueden ser varios)."""
        return validar_correos(self.cleaned_data.get("contacto_copias"))

    def clean_contacto_email(self):
        """Valida el correo principal y le junta las copias: «principal ; copia1 ; copia2» (sin repetidos).

        Si en el principal escribieron varios (como antes), el primero queda de principal y los demás de copia.
        """
        correos = validar_correos(self.cleaned_data["contacto_email"])
        if "contacto_copias" in self.fields:
            try:
                correos += validar_correos(self.data.get(self.add_prefix("contacto_copias"), ""))
            except forms.ValidationError:
                pass  # el error se muestra en «Con copia a»
        vistos = set()
        unicos = [c for c in correos if not (c.lower() in vistos or vistos.add(c.lower()))]
        return " ; ".join(unicos)

    def _id(self, nombre):
        """Compatibilidad: integraciones y pruebas que envían el id (empresa=3, sede=5…)."""
        valor = str(self.data.get(nombre, "")).strip()
        return int(valor) if valor.isdigit() else None

    def clean(self):
        """Resuelve lo escrito a mano en registros reales y valida la radicación.

        - Empresa y sede: se eligen de la lista o se escriben; la sede debe ser de la empresa (una nueva se crea al guardar).
        - Sistema y falla: de la lista o con sus palabras; si el cliente no sabe, queda «por confirmar» por el técnico.
        - Trabajo en alturas y acompañamiento SST: obligatorios (Sí / No).
        - Los rangos en que no puede atender deben ser válidos.
        """
        datos = super().clean()
        # Empresa: el cliente radica para la suya; los demás la escriben o la eligen de la lista
        if self.elige_empresa:
            empresa = (self.empresas.filter(pk=self._id("empresa")).first() if self._id("empresa")
                       else _por_nombre(self.empresas, "razon_social", datos.get("empresa_nombre")))
            if not empresa and "empresa_nombre" not in self.errors:
                escrito = (datos.get("empresa_nombre") or "").strip()
                self.add_error("empresa_nombre", f"No encontramos «{escrito}». Elíjala de la lista (escriba parte del nombre)."
                               if escrito else "Escriba la empresa.")
        else:
            empresa = self.usuario.empresa if self.usuario else None
        # Sede: de la lista o nueva
        self.sede_nueva = ""
        sede = None
        sedes = Sede.objects.filter(empresa=empresa) if empresa else Sede.objects.none()
        if self._id("sede"):
            sede = Sede.objects.filter(pk=self._id("sede")).first()
            if sede and empresa and sede.empresa_id != empresa.pk:
                self.add_error("sede_nombre", "Esa sede no pertenece a la empresa elegida.")
        elif (datos.get("sede_nombre") or "").strip():
            sede = _por_nombre(sedes, "nombre", datos["sede_nombre"])
            if not sede and empresa:
                self.sede_nueva = " ".join(datos["sede_nombre"].split())[:150]
        elif empresa and sedes.filter(activa=True).exists():
            self.add_error("sede_nombre", "Escriba o elija la sede.")
        # Sistema: de la lista o escrito
        categoria, self.sistema_texto, self.por_confirmar = None, "", []
        if self._id("categoria"):
            categoria = Categoria.objects.filter(pk=self._id("categoria")).first()
        elif (datos.get("sistema") or "").strip():
            if es_no_se(datos["sistema"]):
                categoria = Categoria.objects.filter(nombre=SISTEMA_DESCONOCIDO).first()
                self.sistema_texto = "" if categoria else "Por confirmar"
                self.por_confirmar = ["sistema"]
            else:
                categoria = (_por_nombre(Categoria.objects.all(), "nombre", datos["sistema"])
                             or sistema_por_alias(datos["sistema"], datos.get("tipo")))
                if not categoria:
                    self.sistema_texto = " ".join(datos["sistema"].split())[:150]
        elif self.instance.pk and self.instance.categoria_id and "sistema" not in self.data:
            categoria = self.instance.categoria
        else:
            self.add_error("sistema", "Escríbalo con sus palabras o escriba «No sé»." if self.modo_cliente
                           else "Escriba o elija el sistema a intervenir.")
        if categoria and categoria.nombre == SISTEMA_DESCONOCIDO and self.modo_cliente and "sistema" not in self.por_confirmar:
            self.por_confirmar.append("sistema")  # «Diagnóstico técnico»: la técnica confirma el sistema
        # Alturas y SST: obligatorios (Sí / No). Se aceptan también las casillas «on» de integraciones anteriores.
        for campo, anterior, etiqueta in (("alturas", "trabajo_alturas", "si hay trabajo en alturas"),
                                          ("sst", "acompanamiento_sst", "si requiere acompañamiento SST")):
            if not datos.get(campo):
                if anterior in self.data:
                    datos[campo] = "si" if self.data.get(anterior) in ("on", "true", "True", "1", "si") else "no"
                elif self.instance.pk:
                    datos[campo] = "si" if getattr(self.instance, anterior) else "no"
                else:
                    self.add_error(campo, f"Indique {etiqueta}.")
        # Falla: de la lista del sistema o escrita
        falla, self.falla_texto = None, ""
        if self._id("falla"):
            falla = TipoFalla.objects.filter(pk=self._id("falla")).first()
            if falla and categoria and falla.categoria_id != categoria.pk:
                self.add_error("falla_nombre", "Esa falla no corresponde al sistema elegido.")
        elif (datos.get("falla_nombre") or "").strip():
            falla = _por_nombre(TipoFalla.objects.filter(categoria=categoria), "nombre", datos["falla_nombre"]) if categoria else None
            if not falla:
                self.falla_texto = " ".join(datos["falla_nombre"].split())[:200]
        self.instance.no_disponible = leer_no_disponible(self, datos)
        if not self.sin_rangos and not self.rangos.is_valid():
            errores = [e for f in self.rangos.forms for e in f.non_field_errors()] + list(self.rangos.non_form_errors())
            for e in errores or ["Revise los rangos de fecha y hora sin disponibilidad."]:
                self.add_error(None, e)
        datos.update(empresa_final=empresa, sede_final=sede, categoria_final=categoria, falla_final=falla)
        return datos

    def rangos_validos(self):
        """Los rangos de fecha y hora en que el cliente no puede atender, ya validados (vacío si no marcó ninguno)."""
        if self.sin_rangos:
            return []
        return [(f.cleaned_data["desde"], f.cleaned_data["hasta"]) for f in self.rangos.forms
                if f.cleaned_data.get("desde") and f.cleaned_data.get("hasta")]

    def save(self, commit=True):
        """Guarda el ticket (y la sede nueva si se escribió una). El asunto se arma de la descripción."""
        from .models import RangoNoDisponible

        d = self.cleaned_data
        ticket = super().save(commit=False)
        ticket.empresa = d["empresa_final"]
        sede = d["sede_final"]
        if self.sede_nueva:  # la sede que escribió quien radica (la coordinación la puede ajustar en Configuración)
            sede, _ = Sede.objects.get_or_create(
                empresa=ticket.empresa, nombre=self.sede_nueva,
                defaults={"departamento": ticket.departamento, "ciudad": ticket.municipio, "direccion": ticket.direccion})
        ticket.sede = sede
        # La sede aprende su contacto: si aún no tiene y el de esta solicitud es del cliente, queda para la próxima vez
        if sede and not sede.contacto_email and ticket.contacto_email and not self.contacto_es_interno \
                and not ticket.empresa.es_del_grupo:
            sede.contacto_nombre = sede.contacto_nombre or ticket.contacto_nombre
            sede.contacto_telefono = sede.contacto_telefono or ticket.contacto_telefono
            sede.contacto_email = ticket.contacto_email
            if sede.pk:  # la vista guarda el ticket aparte (commit=False); la sede se guarda aquí
                sede.save(update_fields=["contacto_nombre", "contacto_telefono", "contacto_email"])
        categoria = d["categoria_final"]
        if not categoria:  # sistema escrito que no está en la lista
            categoria, _ = Categoria.objects.get_or_create(nombre="Otro", defaults={"activa": False, "orden": 99})
        ticket.categoria, ticket.sistema_texto = categoria, self.sistema_texto
        ticket.falla, ticket.falla_texto = d["falla_final"], self.falla_texto
        ticket.trabajo_alturas, ticket.acompanamiento_sst = d.get("alturas") == "si", d.get("sst") == "si"
        if d.get("latitud") is not None and d.get("longitud") is not None:
            ticket.latitud, ticket.longitud = d["latitud"], d["longitud"]
        if not self.instance.pk or self.modo_cliente:
            ticket.por_confirmar = self.por_confirmar
        # El asunto es la falla o necesidad (lo que eligió o escribió quien radica)
        falla = d["falla_final"].nombre if d.get("falla_final") else self.falla_texto
        if falla:
            ticket.asunto = falla[:200]
        elif not ticket.asunto:
            primera = " ".join(d["descripcion"].split())
            ticket.asunto = (primera[:117] + "…") if len(primera) > 120 else primera
        if self.define_urgencia and "impacto" in self.changed_data:
            ticket.asignar_prioridad_inicial()  # la prioridad arranca según la urgencia que definió el coordinador
        if commit:
            ticket.save()
            if self.sin_rangos:
                return ticket
            ticket.rangos_no_disponible.all().delete()
            for desde, hasta in self.rangos_validos():
                RangoNoDisponible.objects.create(ticket=ticket, desde=desde, hasta=hasta)
        return ticket


# Nombre anterior (lo usan las pruebas y la vista del cliente)
TicketClienteForm = RadicacionForm


class RespuestaClienteForm(BootstrapMixin, forms.Form):
    """Mensaje (con adjuntos) que el cliente agrega a su ticket."""
    mensaje = forms.CharField(
        label="Agregar información o responder",
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Escriba su mensaje para el área de soporte…"}),
    )
    archivos = MultipleFileField(label="Adjuntos")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["archivos"].help_text = ayuda_adjuntos()
        self._aplicar_estilos()


class CalificacionForm(BootstrapMixin, forms.Form):
    """Calificación de 1 a 10 que el cliente da a la atención al cerrar el ticket, con su comentario."""
    calificacion = forms.TypedChoiceField(
        label="¿Cómo califica la atención recibida?",
        choices=[(i, str(i)) for i in range(10, 0, -1)],
        coerce=int,
        widget=forms.RadioSelect,
    )
    comentario = forms.CharField(
        label="Comentario (opcional)", required=False, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()


# --- Personal JANOTEC -------------------------------------------------------
def personal_queryset():
    """Todo el personal JANOTEC: un ticket puede quedar en manos del área técnica, comercial, bodega, central o dirección."""
    return Usuario.objects.filter(is_active=True).exclude(rol=Usuario.Rol.CLIENTE, is_superuser=False) \
        .order_by("first_name", "last_name", "username")


class ResponsableField(forms.ModelChoiceField):
    """Cada persona con su área: «Freyder Gordillo · Encargado», «Alejandra Vargas · Administración y bodega»…"""

    def label_from_instance(self, u):
        """Cada responsable con su nombre y su rol corto."""
        return f"{u.get_full_name() or u.username} · {u.rol_corto}"


class GestionForm(BootstrapMixin, forms.Form):
    """Formulario del área de soporte para avanzar un ticket."""

    VISIBILIDAD = [
        ("publico", "Respuesta al cliente (el cliente la ve)"),
        ("interno", "Nota interna (solo personal JANOTEC)"),
    ]

    mensaje = forms.CharField(
        label="Mensaje",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "placeholder": "Avance, diagnóstico, visita programada…"}),
    )
    visibilidad = forms.ChoiceField(choices=VISIBILIDAD, initial="publico", widget=forms.RadioSelect)
    estado = forms.ChoiceField(label="Estado", choices=Ticket.Estado.choices)
    prioridad = forms.ChoiceField(label="Prioridad", choices=Ticket.Prioridad.choices)
    asignado_a = ResponsableField(
        label="Responsable", queryset=Usuario.objects.none(), required=False, empty_label="— Sin asignar —",
        help_text="Quien debe gestionarlo ahora: un técnico, el área comercial, bodega e inventario…",
    )
    archivos = MultipleFileField(label="Adjuntos")

    # Estados que puede usar el técnico al trabajar su ticket. Recepcionar, programar,
    # cerrar o cancelar es del área técnica (y confirmar la solución, del cliente).
    ESTADOS_ENCARGADO = [Ticket.Estado.VISITA_EJECUTADA, Ticket.Estado.PENDIENTE_VISITA]

    def __init__(self, *args, ticket=None, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ticket = ticket
        self.fields["asignado_a"].queryset = personal_queryset()
        if ticket is not None and not self.is_bound:
            self.initial.update(
                estado=ticket.estado, prioridad=ticket.prioridad, asignado_a=ticket.asignado_a_id
            )
        if usuario is not None and not usuario.es_supervisor:
            # Solo el área administrativa direcciona: asigna y fija la prioridad.
            del self.fields["asignado_a"]
            del self.fields["prioridad"]
            # El técnico cierra su visita; comercial, bodega o central solo dejan su gestión (el estado lo mueve el área técnica)
            permitidos = (set(self.ESTADOS_ENCARGADO) if usuario.es_personal else set()) | ({ticket.estado} if ticket else set())
            self.fields["estado"].choices = [c for c in Ticket.Estado.choices if c[0] in permitidos]
        elif ticket is not None and ticket.area == Ticket.Area.TECNICA:
            # «Propuesta enviada» la pone el área comercial. «En gestión comercial» sí la elige el coordinador:
            # al guardar, el ticket pasa de verdad al área comercial (ficha e informe), no solo cambia de estado.
            comerciales = {Ticket.Estado.PROPUESTA_ENVIADA, Ticket.Estado.EN_INVENTARIO} - {ticket.estado}
            ficha = getattr(ticket, "ficha", None)
            if ficha and ficha.etapa == "levantamiento" and ticket.estado != Ticket.Estado.EN_PROCESO:
                comerciales.add(Ticket.Estado.EN_PROCESO)  # el levantamiento vuelve con el informe del técnico
            # Paso a paso: solo los cambios que tienen sentido desde el estado actual (lo demás, con «Omitir paso»)
            from .diagnostico import SIGUIENTES
            validos = SIGUIENTES.get(ticket.estado, set()) | {ticket.estado}
            self.fields["estado"].choices = [
                (v, "En gestión comercial (pasa al área comercial)" if v == Ticket.Estado.EN_PROCESO and v != ticket.estado else l)
                for v, l in Ticket.Estado.choices if v not in comerciales and v in validos]
        ficha = getattr(ticket, "ficha", None) if ticket is not None else None
        if ficha and ficha.etapa == "levantamiento":
            # Levantamiento comercial: no se resuelve ni se cierra aquí; se devuelve al área comercial
            finales = (Ticket.Estado.VISITA_EJECUTADA, Ticket.Estado.CERRADO, Ticket.Estado.CANCELADO)
            self.fields["estado"].choices = [c for c in self.fields["estado"].choices if c[0] not in finales]
        self.fields["archivos"].help_text = ayuda_adjuntos()
        self._aplicar_estilos()

    def clean(self):
        """Una nota interna con adjuntos debe llevar mensaje, y la visita no se marca como ejecutada sin su acta
        (paso a paso), salvo que coordinación haya omitido el paso «Visita y acta».
        """
        datos = super().clean()
        if datos.get("visibilidad") == "interno" and not (datos.get("mensaje") or "").strip():
            if datos.get("archivos"):
                self.add_error("mensaje", "Escriba una nota para acompañar los adjuntos internos.")
        t = self.ticket
        con_visita = (Ticket.Estado.VISITA_EJECUTADA, Ticket.Estado.INFORME)
        if t is not None and datos.get("estado") in con_visita and datos["estado"] != t.estado and t.estado not in con_visita:
            # Paso a paso: la visita se da por hecha con su acta (o si coordinación omitió el paso)
            if not t.actas.exists() and not t.pasos_saltados.filter(paso="visita").exists():
                self.add_error("estado", "Para marcar la visita como ejecutada primero diligencie el acta de soporte "
                                         "(o coordinación debe omitir el paso «Visita y acta» con su motivo).")
        return datos


class FiltroTicketsForm(BootstrapMixin, forms.Form):
    """Filtros de la bandeja de tickets: texto, estado, tipo, prioridad, cliente y responsable."""
    q = forms.CharField(label="Buscar", required=False,
                        widget=forms.TextInput(attrs={"placeholder": "Radicado, asunto, descripción…"}))
    estado = forms.ChoiceField(
        required=False,
        choices=[("", "Todos los estados"), ("abiertos", "Pendientes (abiertos + visitas ejecutadas)"),
                 ("por_atender", "Programados por atender (sin gestionar)"),
                 ("por_programar", "Por programar (recepcionados y nueva visita)")] + list(Ticket.Estado.choices),
    )
    tipo = forms.ChoiceField(required=False, choices=[("", "Todo tipo")] + list(Ticket.Tipo.choices))
    prioridad = forms.ChoiceField(required=False, choices=[("", "Toda prioridad")] + list(Ticket.Prioridad.choices))
    empresa = forms.ModelChoiceField(required=False, queryset=Empresa.objects.all(), empty_label="Todas las empresas")
    asignado = forms.ChoiceField(required=False)

    def __init__(self, *args, para_personal=True, supervisor=True, **kwargs):
        super().__init__(*args, **kwargs)
        if para_personal and not supervisor:
            del self.fields["asignado"]  # el encargado solo ve lo suyo
        elif para_personal:
            opciones = [("", "Cualquier responsable"), ("mios", "Asignados a mí"), ("sin", "Sin asignar")]
            opciones += [(str(u.pk), str(u)) for u in personal_queryset()]
            self.fields["asignado"].choices = opciones
        else:
            for nombre in ("empresa", "asignado", "prioridad"):
                del self.fields[nombre]
        self._aplicar_estilos()


class FiltroActasForm(BootstrapMixin, forms.Form):
    """Filtros de la consulta de actas: texto, cliente, sede, sistema, resultado y año."""
    q = forms.CharField(label="Buscar", required=False,
                        widget=forms.TextInput(attrs={"placeholder": "N° de acta, diagnóstico, equipo, técnico…"}))
    empresa = forms.ModelChoiceField(required=False, queryset=Empresa.objects.none(), empty_label="Todos los clientes")
    sede = forms.ModelChoiceField(required=False, queryset=Sede.objects.none(), empty_label="Todas las sedes")
    sistema = forms.ChoiceField(required=False, choices=[("", "Todo sistema"), ("CCTV", "CCTV"), ("ALARMA", "Alarma"),
                                                          ("EQUIPOS INFORMATICOS", "Equipos informáticos"), ("GPS", "GPS")])
    resultado = forms.ChoiceField(required=False, choices=[("", "Todo resultado")] + list(ActaSoporte.Resultado.choices))
    anio = forms.ChoiceField(label="Año", required=False)

    def __init__(self, *args, actas=None, mostrar_empresa=True, **kwargs):
        super().__init__(*args, **kwargs)
        actas = actas if actas is not None else ActaSoporte.objects.none()
        # Min/Max en vez de .dates(): no depende de las tablas de zona horaria de MySQL
        from django.db.models import Max, Min
        rango = actas.aggregate(desde=Min("fecha"), hasta=Max("fecha"))
        anios = []
        if rango["desde"]:
            anios = list(range(timezone.localtime(rango["hasta"]).year, timezone.localtime(rango["desde"]).year - 1, -1))
        self.fields["anio"].choices = [("", "Todos los años")] + [(str(a), str(a)) for a in anios]
        sedes = Sede.objects.filter(actas__in=actas).distinct()
        if mostrar_empresa:
            self.fields["empresa"].queryset = Empresa.objects.filter(actas__in=actas).distinct().order_by("razon_social")
            # Las sedes aparecen al elegir el cliente (juntas serían cientos)
            empresa_id = self.data.get("empresa") if self.is_bound else None
            if empresa_id and str(empresa_id).isdigit():
                sedes = sedes.filter(empresa_id=empresa_id)
            else:
                del self.fields["sede"]
        else:
            del self.fields["empresa"]
        if "sede" in self.fields:
            self.fields["sede"].queryset = sedes.order_by("nombre")
        self._aplicar_estilos()


# --- Acta de soporte diligenciada por el técnico desde el ticket ---------------
FIRMA_MAX_BYTES = 600 * 1024


def leer_firma(valor, obligatoria, etiqueta):
    """Convierte la firma dibujada (data:image/png;base64,...) en un archivo PNG."""
    import base64
    import binascii

    from django.core.files.base import ContentFile

    valor = (valor or "").strip()
    if not valor:
        if obligatoria:
            raise forms.ValidationError(f"Falta la firma {etiqueta}.")
        return None
    prefijo = "data:image/png;base64,"
    if not valor.startswith(prefijo):
        raise forms.ValidationError(f"La firma {etiqueta} no es válida.")
    try:
        datos = base64.b64decode(valor[len(prefijo):], validate=True)
    except (binascii.Error, ValueError):
        raise forms.ValidationError(f"La firma {etiqueta} no es válida.")
    if not datos.startswith(b"\x89PNG") or len(datos) > FIRMA_MAX_BYTES:
        raise forms.ValidationError(f"La firma {etiqueta} no es válida.")
    return ContentFile(datos, name="firma.png")


EXTENSIONES_EVIDENCIA = ("jpg", "jpeg", "png", "webp", "heic", "mp4", "pdf")


def validar_evidencias(archivos):
    """Solo se aceptan como evidencia fotos, videos MP4 y PDF."""
    for f in archivos:
        if os.path.splitext(f.name)[1].lower().lstrip(".") not in EXTENSIONES_EVIDENCIA:
            raise forms.ValidationError(f"«{f.name}»: suba fotos, video mp4 o PDF.")
    return archivos


class EvidenciasActaForm(BootstrapMixin, forms.Form):
    """Agregar evidencias a un acta ya registrada (lo que no se alcanzó a subir en el sitio)."""

    etapa = forms.ChoiceField(label="¿De qué son?", choices=[("resultado", "Resultado final"), ("proceso", "Durante el trabajo")])
    archivos = MultipleFileField(label="Archivos", help_text="Hasta 5 a la vez: fotos, video mp4 o PDF.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["archivos"].widget.attrs["accept"] = "image/*,video/mp4,.pdf"
        self._aplicar_estilos()

    def clean_archivos(self):
        """Debe venir al menos un archivo y todos deben ser evidencias válidas."""
        archivos = validar_evidencias(self.cleaned_data["archivos"])
        if not archivos:
            raise forms.ValidationError("Elija al menos un archivo.")
        return archivos


class ActaForm(BootstrapMixin, forms.Form):
    """Mismos campos del formato «Acta de Soporte, Instalación, Desinstalación V1»."""

    ciudad = forms.CharField(max_length=100)
    modalidad = forms.ChoiceField(label="¿Cómo se atendió?", choices=ActaSoporte.Modalidad.choices, widget=forms.RadioSelect)
    actividad = forms.MultipleChoiceField(
        label="Actividad realizada", widget=forms.CheckboxSelectMultiple,
        choices=[(a, a) for a in ActaSoporte.ACTIVIDADES],
    )
    sistema = forms.CharField(label="Mantenimiento del sistema de", max_length=60,
                              help_text="Elija de la lista o escriba el sistema.")
    diagnostico = forms.CharField(label="Diagnóstico", widget=forms.Textarea(attrs={"rows": 3}))
    resultado = forms.ChoiceField(label="Resultado del mantenimiento", choices=ActaSoporte.Resultado.choices,
                                  widget=forms.RadioSelect)
    observaciones = forms.CharField(label="Observaciones del resultado del soporte", widget=forms.Textarea(attrs={"rows": 3}))
    evidencias_proceso = MultipleFileField(label="Evidencias durante el trabajo",
                                           help_text="Cómo se encontró y cómo se fue haciendo. Fotos, video corto (mp4) o PDF.")
    evidencias_resultado = MultipleFileField(label="Evidencias del resultado",
                                             help_text="Cómo quedó: el sistema funcionando, la imagen en el monitor, el tablero cerrado…")
    # Asistentes
    cargo_tecnico = forms.CharField(label="Cargo del técnico", max_length=150)
    firma_tecnico = forms.CharField(widget=forms.HiddenInput, required=False)
    acompanante_nombre = forms.CharField(label="Técnico encargado 2 (opcional)", max_length=150, required=False)
    acompanante_cargo = forms.CharField(label="Cargo del técnico encargado 2", max_length=150, required=False)
    firma_acompanante = forms.CharField(widget=forms.HiddenInput, required=False)
    # Cliente
    recibio = forms.CharField(label="Apellidos y nombres del representante del cliente", max_length=150)
    cargo_recibio = forms.CharField(label="Cargo del representante del cliente", max_length=150)
    firma_cliente = forms.CharField(widget=forms.HiddenInput, required=False)
    copia_correo = forms.CharField(label="Copia correo a", max_length=500, required=False,
                                   help_text="Separe varios correos con «;».")
    latitud = forms.DecimalField(max_digits=10, decimal_places=7, required=False, widget=forms.HiddenInput)
    longitud = forms.DecimalField(max_digits=10, decimal_places=7, required=False, widget=forms.HiddenInput)
    marcar_resuelto = forms.BooleanField(
        label="Marcar la visita como ejecutada (si quedó solucionado, el cliente lo confirmará; si no, queda pendiente de nueva visita)",
        required=False,
    )
    # Oportunidad comercial detectada en la visita (interna: el cliente no la ve)
    oportunidad_servicios = forms.MultipleChoiceField(
        label="Servicios que podría necesitar", required=False, widget=forms.CheckboxSelectMultiple,
        choices=OportunidadComercial.SERVICIOS,
    )
    oportunidad_nota = forms.CharField(
        label="¿Qué vio o qué le comentó el cliente?", required=False,
        widget=forms.Textarea(attrs={"rows": 2, "placeholder": "Ej.: pidió cotizar una cámara para el parqueadero; la bodega no tiene alarma"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["sistema"].widget = ConSugerencias([e for _, e in ActaSoporte.SISTEMAS],
                                                       attrs={"placeholder": "CCTV, Alarma, Control de acceso…"})
        if not self.is_bound and self.initial.get("sistema"):
            self.initial["sistema"] = dict(ActaSoporte.SISTEMAS).get(self.initial["sistema"], self.initial["sistema"])
        for campo in ("evidencias_proceso", "evidencias_resultado"):
            self.fields[campo].widget.attrs["accept"] = "image/*,video/mp4,.pdf"
        # Ayuda de redacción (static/js/redaccion.js): guía, frases rápidas y botón «Mejorar redacción»
        for campo, tipo in (("diagnostico", "diagnostico"), ("observaciones", "resultado"),
                            ("oportunidad_nota", "oportunidad")):
            self.fields[campo].widget.attrs["data-redaccion"] = tipo
        self._aplicar_estilos()

    def clean_evidencias_proceso(self):
        """Evidencias tomadas durante el trabajo: fotos, videos MP4 o PDF."""
        return validar_evidencias(self.cleaned_data["evidencias_proceso"])

    def clean_evidencias_resultado(self):
        """Evidencias del resultado final: fotos, videos MP4 o PDF."""
        return validar_evidencias(self.cleaned_data["evidencias_resultado"])

    def clean_sistema(self):
        """«Alarma» o «alarma» → el código del formato (ALARMA); otro sistema escrito se guarda tal cual."""
        texto = " ".join(self.cleaned_data["sistema"].split())
        for codigo, etiqueta in ActaSoporte.SISTEMAS:
            if texto.lower() in (codigo.lower(), etiqueta.lower()):
                return codigo
        return texto[:60]

    def clean_copia_correo(self):
        """Valida cada correo al que se envía copia del acta y los deja separados por «;»."""
        from django.core.validators import validate_email

        correos = [c.strip() for c in self.cleaned_data["copia_correo"].replace(",", ";").split(";") if c.strip()]
        for c in correos:
            try:
                validate_email(c)
            except forms.ValidationError:
                raise forms.ValidationError(f"«{c}» no es un correo válido.")
        return " ; ".join(correos)

    def clean(self):
        """Valida las firmas del acta.

        La del técnico siempre es obligatoria; la del cliente también, salvo en soporte virtual, donde no está presente
        (y tampoco se guarda la ubicación, que sería la del técnico). Si firma el técnico encargado 2 debe ir su nombre.
        """
        datos = super().clean()
        # En virtual el cliente no está para firmar, y la ubicación sería la del técnico, no la de la sede
        virtual = datos.get("modalidad") == ActaSoporte.Modalidad.VIRTUAL
        if virtual:
            datos.update(firma_cliente="", latitud=None, longitud=None)
        for campo, obligatoria, etiqueta in (("firma_tecnico", True, "del técnico"),
                                             ("firma_cliente", not virtual, "del representante del cliente"),
                                             ("firma_acompanante", False, "del técnico encargado 2")):
            try:
                datos[campo] = leer_firma(datos.get(campo), obligatoria, etiqueta)
            except forms.ValidationError as e:
                self.add_error(None, e)
        if datos.get("firma_acompanante") and not datos.get("acompanante_nombre"):
            self.add_error("acompanante_nombre", "Escriba el nombre del técnico encargado 2 que firma.")
        return datos


def _frecuentes(modelo, campo, limite=150):
    """Los valores más usados de un campo (para sugerirlos)."""
    from django.db.models import Count

    return list(modelo.objects.exclude(**{campo: ""}).values_list(campo, flat=True).annotate(n=Count("id"))
                .order_by("-n")[:limite])


class EquipoActaForm(BootstrapMixin, forms.Form):
    """Una fila de la «Relación de equipos»."""

    equipo = forms.CharField(label="Equipo", max_length=150)
    marca = forms.CharField(label="Marca del equipo", max_length=100, required=False)
    referencia = forms.CharField(label="Referencia o modelo", max_length=100, required=False,
                                 widget=forms.TextInput(attrs={"placeholder": "Ej.: VISTA 48LA, DS-2CD1043"}))
    serial = forms.CharField(label="Serial del equipo", max_length=100, required=False)
    cantidad = forms.CharField(label="Cantidad", max_length=20, required=False)
    observaciones = forms.CharField(label="Observaciones del equipo", required=False,
                                    widget=forms.Textarea(attrs={"rows": 2}))
    fotos = MultipleFileField(label="Foto del equipo")

    accion = forms.ChoiceField(label="¿Qué se hizo?", required=False, initial="revisado", choices=[
        ("revisado", "Revisado"), ("reparado", "Reparado"), ("configurado", "Configurado"), ("retirado", "Se retiró (desmonte)")])
    propiedad = forms.ChoiceField(label="¿De quién es?", required=False, choices=[
        ("", "— Elija —"), ("cliente", "Del cliente"), ("janotec", "De JANOTEC")],
        help_text="Define el seguimiento: lo de JANOTEC queda en la hoja de vida del equipo; si se retira, vuelve a bodega.")
    placa = forms.CharField(label="Placa de inventario", max_length=40, required=False)

    # Ejemplos que orientan al técnico según la lista
    EJEMPLOS = {"intervenido": ("Ej.: Cámara IP bala", "Ej.: Hikvision", "Qué se le hizo: revisión, ajuste, cambio de conector…"),
                "utilizado": ("Ej.: Router Ruijie", "Ej.: Ruijie", "Para qué se usó o dónde quedó instalado"),
                "retirado": ("Ej.: Panel de alarma", "Ej.: Honeywell", "Estado en que se retiró: funciona, dañado, batería mala…"),
                "requerido": ("Ej.: Fuente de poder 12 V 5 A", "Ej.: Hikvision", "Para qué se necesita o dónde va: zona, punto, cámara…")}

    def __init__(self, *args, clase="intervenido", **kwargs):
        super().__init__(*args, **kwargs)
        from .models import EquipoActa

        self.clase = clase
        if clase != "intervenido":
            del self.fields["accion"]  # solo un equipo intervenido puede haberse retirado
        if clase in ("utilizado", "requerido"):
            del self.fields["propiedad"]
            del self.fields["placa"]
            if clase == "requerido":  # aún no existe: no tiene serial
                del self.fields["serial"]
        else:
            self.fields["placa"].widget.attrs["placeholder"] = "Ej.: CAM-BAL-2MP002 (si es de JANOTEC)"

        equipo, marca, observaciones = self.EJEMPLOS[clase]
        self.fields["equipo"].widget = ConSugerencias(_frecuentes(EquipoActa, "equipo"), attrs={"placeholder": equipo})
        self.fields["marca"].widget = ConSugerencias(_frecuentes(EquipoActa, "marca"), attrs={"placeholder": marca})
        self.fields["observaciones"].widget.attrs["placeholder"] = observaciones
        self.fields["observaciones"].widget.attrs["data-redaccion"] = "equipo"  # ayuda de redacción
        if clase in ("utilizado", "requerido"):
            self.fields["equipo"].label = "Material o equipo"
        self.fields["fotos"].widget.attrs.update({"accept": "image/*", "capture": "environment"})
        self.fields["fotos"].help_text = "Puede tomar la foto con la cámara del celular."
        self._aplicar_estilos()

    @property
    def changed_data(self):
        # Una fila sin fotos no cuenta como diligenciada solo por el campo de archivos (llega como lista vacía)
        """Qué campos se diligenciaron. Una fila solo cuenta si se escribió el equipo: el campo de fotos vacío o
        haber elegido «qué se hizo» o «de quién es» no la vuelven una fila diligenciada.
        """
        cambios = super().changed_data
        if "fotos" in cambios and not self.files.getlist(self.add_prefix("fotos")):
            cambios = [c for c in cambios if c != "fotos"]
        # Elegir «qué se hizo» o «de quién es» sin escribir el equipo no convierte la fila en diligenciada
        if not set(cambios) - {"propiedad", "accion"}:
            cambios = []
        return cambios

    @property
    def es_retiro(self):
        """Si el equipo se está retirando (en la lista de retirados o marcado como retirado)."""
        return self.clase == "retirado" or (self.cleaned_data or {}).get("accion") == "retirado"

    def clean(self):
        """Cada equipo dice de quién es. Si es de JANOTEC lleva placa o serial: con ella se le hace seguimiento
        en la hoja de vida del activo y, si se retira, se devuelve al inventario.
        """
        datos = super().clean()
        if "propiedad" in self.fields and datos.get("equipo"):
            if not datos.get("propiedad"):
                self.add_error("propiedad", "Indique si el equipo es del cliente o de JANOTEC.")
            elif datos["propiedad"] == "janotec" and not (datos.get("placa") or datos.get("serial")):
                self.add_error("placa", "Para devolverlo al inventario escriba la placa (o el serial) del equipo de JANOTEC."
                               if self.es_retiro else "Para hacerle seguimiento escriba la placa (o el serial) del equipo de JANOTEC.")
        return datos

    def clean_fotos(self):
        """Las fotos del equipo deben ser imágenes (jpg, png, webp o heic)."""
        fotos = self.cleaned_data["fotos"]
        for f in fotos:
            if os.path.splitext(f.name)[1].lower().lstrip(".") not in ("jpg", "jpeg", "png", "webp", "heic"):
                raise forms.ValidationError(f"«{f.name}» no es una foto (jpg, png, webp).")
        return fotos


EquipoActaFormSet = forms.formset_factory(EquipoActaForm, extra=1, min_num=0, max_num=30, validate_max=True)


class AplazarForm(BootstrapMixin, forms.Form):
    """El coordinador aplaza una visita programada y deja claro el porqué."""

    motivo = forms.ChoiceField(label="¿Por qué se aplaza?", choices=[("", "— Elija el motivo —")] + list(Aplazamiento.Motivo.choices))
    detalle = forms.CharField(
        label="Explique qué pasó", min_length=20, max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Ej.: La administradora llamó a las 7 a. m.: hay un evento en la sede y "
                                                                "piden pasar la visita a la otra semana."}),
        help_text="Queda resaltado en el historial del ticket: escriba lo suficiente para que cualquiera entienda el aplazamiento.")
    informar_cliente = forms.BooleanField(label="Informar al cliente por correo", required=False, initial=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()


class InformeBorradorForm(BootstrapMixin, forms.ModelForm):
    """Corrección del borrador del informe técnico antes de pasarlo a limpio."""

    class Meta:
        model = InformeBorrador
        fields = ["destinatario", "asunto", "introduccion", "actividades", "justificacion", "conclusiones", "recomendaciones",
                  "suministros", "servicios", "garantia"]
        widgets = {"introduccion": forms.Textarea(attrs={"rows": 4}), "actividades": forms.Textarea(attrs={"rows": 6}),
                   "suministros": forms.Textarea(attrs={"rows": 8, "placeholder": "3 Cámara IP 4 MP bala | DS-2CD1043G0E-I | Hikvision\n"
                                                        "1 NVR 8 canales PoE | DS-7608NI-Q1/8P | Hikvision\n120 m Cable UTP cat 6 exterior | Hikvision"}),
                   "servicios": forms.Textarea(attrs={"rows": 3}), "garantia": forms.Textarea(attrs={"rows": 3}),
                   "justificacion": forms.Textarea(attrs={"rows": 3}), "conclusiones": forms.Textarea(attrs={"rows": 5}),
                   "recomendaciones": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Nombres y ayudas según las secciones del informe de JANOTEC
        self.fields["suministros"].label = "Tabla de equipos y materiales requeridos"
        self.fields["suministros"].help_text = ("Lo que se propone, uno por línea: cantidad descripción | referencia | marca. "
                                                "La imagen de referencia sale del catálogo de precios. Déjelo vacío si el "
                                                "informe es solo informativo: la sección no sale en el PDF.")
        self.fields["servicios"].label = "Servicios requeridos (mano de obra)"
        self.fields["servicios"].help_text = "Uno por línea. Van en la misma tabla, con marca JANOTEC. Ej.: 1 Instalación y configuración"
        self.fields["conclusiones"].help_text = ("Una por línea: qué se hizo y, si se propone algo, qué se propone y su alcance "
                                                 "(qué incluye y qué no).")
        self.fields["recomendaciones"].help_text = "Una por línea. Opcional; van dentro de las conclusiones."
        self.fields["garantia"].help_text = "Opcional; va al final de las conclusiones."
        self._aplicar_estilos()
