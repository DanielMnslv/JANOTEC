"""Formularios del área comercial.

Por temas: radicación (FichaForm del asesor, CotizacionClienteForm del portal del cliente), informe del
requerimiento (InformeForm), gestión de la propuesta (GestionComercialForm) y, al final, cotizador y catálogo
de precios (líneas, cotización e ítems del catálogo).
"""

from django import forms
from django.db.models import Q
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario
from tickets.forms import (
    BootstrapMixin, ConSugerencias, MultipleFileField, _por_nombre, agregar_dias_no_disponible, ayuda_adjuntos,
    dias_para_plantilla, leer_no_disponible, municipios_sugeridos,
)
from tickets.models import OportunidadComercial, Ticket

from .models import FichaComercial

F = FichaComercial
FECHA_HORA = {"type": "datetime-local"}
FORMATO_FECHA_HORA = ["%Y-%m-%dT%H:%M"]


def _opciones(choices, vacio="— Sin definir —"):
    """Las opciones de un campo con una primera opción vacía («— Sin definir —»)."""
    return [("", vacio)] + list(choices)


class DiasNoDisponibleMixin:
    """Los mismos selectores por día de la radicación de tickets: cuándo NO puede atender."""

    def _agregar_dias(self):
        """Agrega al formulario los selectores de los días y horas en que NO puede atender."""
        agregar_dias_no_disponible(self)

    def dias_no_disponible(self):
        """Los selectores por día listos para dibujarlos en la plantilla."""
        return dias_para_plantilla(self)

    def no_disponible(self):
        """Lo que se leyó al validar: los días y horas en que no puede atender."""
        return self._no_disponible

    def full_clean(self):
        """Valida como siempre y, si quedó cleaned_data, lee los días no disponibles."""
        super().full_clean()
        if hasattr(self, "cleaned_data"):
            self._no_disponible = leer_no_disponible(self, self.cleaned_data)


def _fecha_futura(valor):
    """Valida que una fecha (opcional) no esté en el pasado."""
    if valor and valor < timezone.now():
        raise forms.ValidationError("La fecha debe ser futura.")
    return valor


class FichaForm(DiasNoDisponibleMixin, BootstrapMixin, forms.Form):
    """Ficha de requerimiento comercial (la diligencia el asesor). Crea el ticket y su ficha."""

    # 1. Contacto y horario de atención
    empresa_nombre = forms.CharField(label="Empresa / conjunto / entidad", max_length=200, required=False,
                                     help_text="Escriba el nombre: si ya es cliente se sugiere; si no, queda como prospecto nuevo.")
    sede_nombre = forms.CharField(label="Sede", max_length=150, required=False,
                                  help_text="Opcional. Si no está en la lista, escríbala y se registra.")
    # Compatibilidad (integraciones y pruebas que envían el id o el prospecto aparte)
    empresa = forms.ModelChoiceField(label="Empresa / conjunto / entidad", required=False, queryset=Empresa.objects.none(),
                                     empty_label="— Cliente existente —")
    nueva_empresa = forms.CharField(label="…o registre un prospecto nuevo", max_length=200, required=False,
                                    widget=forms.TextInput(attrs={"placeholder": "Nombre del sitio o razón social"}))
    nuevo_nit = forms.CharField(label="NIT del prospecto (opcional)", max_length=30, required=False)
    sede = forms.ModelChoiceField(label="Sede", required=False, queryset=Sede.objects.none(), empty_label="— Sede (opcional) —")
    comercial = forms.ModelChoiceField(label="Asesor comercial que reporta", required=False, queryset=Usuario.objects.none())
    contacto_nombre = forms.CharField(label="Nombre del contacto", max_length=150)
    contacto_cargo = forms.CharField(label="Cargo / rol", max_length=100, required=False)
    contacto_telefono = forms.CharField(label="Teléfono / WhatsApp", max_length=50)
    contacto_email = forms.EmailField(label="Correo del cliente", required=False)
    municipio = forms.CharField(label="Municipio / ubicación", max_length=150, required=False)
    direccion = forms.CharField(label="Dirección", max_length=200, required=False)
    restricciones = forms.CharField(label="Otras restricciones de atención", max_length=250, required=False,
                                    widget=forms.TextInput(attrs={"placeholder": "Ej.: festivos no hay personal"}))
    # Primera visita técnica: qué día y en qué jornada la quiere el cliente (o que no la necesita)
    fecha_primera_visita = forms.DateField(label="¿Qué día quiere el cliente la primera visita técnica?", required=False,
                                           widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    jornada_visita = forms.ChoiceField(label="Jornada de la visita", required=False,
                                       choices=[("", "Cualquier jornada")] + list(Ticket.Jornada.choices))
    sin_visita = forms.BooleanField(label="No necesita visita: se cotiza con lo que informó el cliente", required=False)
    # 2. Necesidad
    asunto = forms.CharField(label="¿Qué dijo el cliente? (con sus palabras)", max_length=200,
                             widget=forms.TextInput(attrs={"placeholder": "Ej.: «se me metieron al parqueadero y las cámaras no sirven»"}))
    descripcion = forms.CharField(label="Descripción / detalle", required=False, widget=forms.Textarea(
        attrs={"rows": 3, "placeholder": "Amplíe: qué pasó, dónde, qué espera lograr el cliente…"}))
    impacto = forms.ChoiceField(label="Urgencia", required=False, choices=_opciones(Ticket.Impacto.choices),
                                help_text="Alta: comercial y administrativa le dan prioridad (exige fecha máxima).")
    fecha_max_propuesta = forms.DateTimeField(label="Fecha máxima para la propuesta", required=False,
                                              input_formats=FORMATO_FECHA_HORA,
                                              widget=forms.DateTimeInput(attrs=FECHA_HORA, format="%Y-%m-%dT%H:%M"),
                                              help_text="Si es en 2 días o menos, la propuesta queda como prioritaria.")
    # 3. Calificación técnica preliminar
    sistema_actual = forms.CharField(label="¿Sistema actual? ¿Marca?", max_length=150, required=False,
                                     widget=forms.TextInput(attrs={"placeholder": "Ej.: Hikvision viejas / no tiene"}))
    energia = forms.ChoiceField(label="¿Energía en el sitio?", required=False, choices=_opciones(F.SiNo.choices))
    internet = forms.ChoiceField(label="¿Internet en el sitio?", required=False, choices=_opciones(F.SiNo.choices))
    tamano = forms.CharField(label="Tamaño / n.º de áreas o puntos", max_length=150, required=False,
                             widget=forms.TextInput(attrs={"placeholder": "Ej.: 6 áreas, 2 accesos"}))
    # 4. Oportunidad de negocio (interno)
    servicios = forms.MultipleChoiceField(label="Servicios de interés (marque todos los que apliquen)", required=False,
                                          choices=OportunidadComercial.SERVICIOS, widget=forms.CheckboxSelectMultiple)
    otras_necesidades = forms.CharField(label="«¿Y qué más le preocupa?» · otras necesidades", max_length=300, required=False,
                                        widget=forms.TextInput(attrs={"placeholder": "Lo que descubra preguntando de más = más negocio"}))
    nivel_decision = forms.ChoiceField(label="Nivel de decisión del contacto", required=False, choices=_opciones(F.NivelDecision.choices))
    otras_sedes = forms.BooleanField(label="Sí (más proyectos potenciales)", required=False)
    presupuesto = forms.CharField(label="Presupuesto / tiempos mencionados", max_length=200, required=False,
                                  widget=forms.TextInput(attrs={"placeholder": "Si dio alguna cifra o fecha"}))
    observaciones = forms.CharField(label="Observaciones adicionales (interno)", required=False,
                                    widget=forms.Textarea(attrs={"rows": 2}))
    # 5. Fotos y anexos
    fotos = MultipleFileField(label="Fotos del sitio")
    anexos = MultipleFileField(label="Anexos (PDF, Word, planos…)")

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        empresas = Empresa.objects.filter(activa=True).order_by("es_prospecto", "razon_social")
        self.fields["empresa"].queryset = empresas
        self.fields["sede"].queryset = Sede.objects.filter(activa=True)
        for nombre in ("empresa", "nueva_empresa", "sede"):
            self.fields[nombre].widget = forms.HiddenInput()
        self.fields["empresa_nombre"].widget = ConSugerencias(
            [(e.razon_social, "prospecto" if e.es_prospecto else (f"NIT {e.nit}" if e.nit else "cliente")) for e in empresas],
            attrs={"placeholder": "Escriba el nombre del cliente o del prospecto…"})
        self.fields["sede_nombre"].widget = ConSugerencias([], attrs={"placeholder": "Escriba o elija la sede…"})
        self.fields["municipio"].widget = ConSugerencias(municipios_sugeridos(), attrs={"placeholder": "Ej.: Villavicencio"})
        self.fields["comercial"].queryset = Usuario.objects.filter(
            Q(rol__in=[Usuario.Rol.COMERCIAL, Usuario.Rol.DIRECCION]) | Q(tambien_comercial=True), is_active=True).order_by("first_name")
        if usuario and usuario.es_comercial and not self.is_bound:
            self.initial.setdefault("comercial", usuario.pk)
        self._agregar_dias()
        self.fields["fotos"].help_text = ayuda_adjuntos()
        self.fields["anexos"].help_text = ayuda_adjuntos()
        self._aplicar_estilos()

    def sedes_por_empresa(self):
        """{empresa en minúscula: [sedes]}: las sedes sugeridas según la empresa escrita."""
        mapa = {}
        for s in self.fields["sede"].queryset.select_related("empresa").order_by("nombre"):
            mapa.setdefault(s.empresa.razon_social.lower(), []).append(s.nombre)
        return mapa

    def clean_fecha_max_propuesta(self):
        """La fecha máxima para la propuesta, si se da, debe ser futura."""
        return _fecha_futura(self.cleaned_data.get("fecha_max_propuesta"))

    def clean(self):
        """Resuelve la empresa escrita: si ya es cliente se usa; si no, queda como prospecto nuevo. Igual con la sede."""
        datos = super().clean()
        escrita = " ".join((datos.get("empresa_nombre") or "").split())
        if escrita and not datos.get("empresa") and not (datos.get("nueva_empresa") or "").strip():
            # Si ya existe se usa; si no, es un prospecto nuevo
            datos["empresa"] = _por_nombre(self.fields["empresa"].queryset, "razon_social", escrita)
            if not datos["empresa"]:
                datos["nueva_empresa"] = escrita
        empresa, nueva = datos.get("empresa"), (datos.get("nueva_empresa") or "").strip()
        datos["sede_texto"] = ""
        sede_escrita = " ".join((datos.get("sede_nombre") or "").split())
        if sede_escrita and not datos.get("sede"):
            datos["sede"] = _por_nombre(Sede.objects.filter(empresa=empresa), "nombre", sede_escrita) if empresa else None
            if not datos["sede"]:
                datos["sede_texto"] = sede_escrita[:150]
        if not empresa and not nueva:
            self.add_error("empresa_nombre", "Escriba el nombre del cliente o del prospecto.")
        if empresa and nueva:
            self.add_error("empresa_nombre", "Elija un cliente existente o registre uno nuevo, no ambos.")
        nit = (datos.get("nuevo_nit") or "").strip()
        if nueva and nit and Empresa.objects.filter(nit=nit).exists():
            self.add_error("nuevo_nit", "Ya existe una empresa con ese NIT: elíjala en la lista.")
        sede = datos.get("sede")
        if sede and empresa and sede.empresa_id != empresa.pk:
            self.add_error("sede_nombre", "La sede no pertenece a esa empresa.")
        if nueva:
            datos["sede"] = None
        self._validar_visita(datos)
        if datos.get("impacto") == Ticket.Impacto.ALTA and not datos.get("fecha_max_propuesta"):
            self.add_error("fecha_max_propuesta", "Con urgencia alta, indique para cuándo necesita el cliente la propuesta.")
        return datos

    def _validar_visita(self, datos):
        """La fecha de la primera visita es obligatoria (salvo «no necesita visita») y debe caer cuando se puede atender:
        desde hoy, no domingo, el sábado solo en la mañana y no en un día o jornada en que el cliente dijo que no puede."""
        from tickets.models import DIAS_SEMANA

        if datos.get("sin_visita"):
            datos["fecha_primera_visita"], datos["jornada_visita"] = None, ""
            return
        fecha, jornada = datos.get("fecha_primera_visita"), datos.get("jornada_visita") or ""
        if not fecha:
            if "fecha_primera_visita" not in self.errors:
                self.add_error("fecha_primera_visita", "Indique qué día quiere el cliente la primera visita técnica, "
                                                       "o marque que no necesita visita.")
            return
        if fecha < timezone.localdate():
            self.add_error("fecha_primera_visita", "La fecha de la visita no puede ser anterior a hoy.")
            return
        clave, dia = DIAS_SEMANA[fecha.weekday()]
        if clave == "domingo":
            self.add_error("fecha_primera_visita", "Los domingos no se hacen visitas: elija otro día.")
        elif clave == "sabado" and jornada == Ticket.Jornada.TARDE:
            self.add_error("jornada_visita", "El sábado solo se atiende en la mañana.")
        no_puede = datos.get(f"nd_{clave}") or ""
        if no_puede == "todo":
            self.add_error("fecha_primera_visita", f"Arriba dice que el cliente no puede atender el {dia.lower()}.")
        elif no_puede and no_puede == jornada:
            self.add_error("jornada_visita", f"Arriba dice que el {dia.lower()} el cliente no puede en esa jornada.")

    @staticmethod
    def sede_final(empresa, datos):
        """La sede elegida, o la nueva que se escribió (se registra para esa empresa)."""
        if datos.get("sede") or not datos.get("sede_texto"):
            return datos.get("sede")
        return Sede.objects.get_or_create(empresa=empresa, nombre=datos["sede_texto"],
                                          defaults={"ciudad": (datos.get("municipio") or "")[:100],
                                                    "direccion": (datos.get("direccion") or "")[:200]})[0]


class CotizacionClienteForm(DiasNoDisponibleMixin, BootstrapMixin, forms.Form):
    """Solicitud de cotización desde el portal del cliente (sin los datos internos de la ficha)."""

    sede_nombre = forms.CharField(label="Sede", max_length=150, required=False,
                                  help_text="Si aplica. Si no está en la lista, escríbala.")
    asunto = forms.CharField(label="¿Qué necesita?", max_length=200,
                             widget=forms.TextInput(attrs={"placeholder": "Ej.: cámaras para el parqueadero nuevo"}))
    descripcion = forms.CharField(label="Cuéntenos más", required=False, widget=forms.Textarea(
        attrs={"rows": 4, "placeholder": "Dónde, cuántas áreas o puntos, qué quiere lograr…"}))
    servicios = forms.MultipleChoiceField(label="¿Qué le interesa?", required=False, choices=OportunidadComercial.SERVICIOS,
                                          widget=forms.CheckboxSelectMultiple)
    sistema_actual = forms.CharField(label="¿Tiene algún sistema hoy? ¿De qué marca?", max_length=150, required=False)
    tamano = forms.CharField(label="Tamaño / n.º de áreas o puntos", max_length=150, required=False,
                             widget=forms.TextInput(attrs={"placeholder": "Ej.: 2 bodegas y 1 portería"}))
    fecha_max_propuesta = forms.DateTimeField(label="¿Para cuándo necesita la propuesta? (opcional)", required=False,
                                              input_formats=FORMATO_FECHA_HORA,
                                              widget=forms.DateTimeInput(attrs=FECHA_HORA, format="%Y-%m-%dT%H:%M"))
    contacto_nombre = forms.CharField(label="Persona de contacto", max_length=150)
    contacto_cargo = forms.CharField(label="Cargo", max_length=100, required=False)
    contacto_telefono = forms.CharField(label="Teléfono / celular", max_length=50)
    contacto_email = forms.EmailField(label="Correo de contacto")
    restricciones = forms.CharField(label="Otras restricciones (opcional)", max_length=250, required=False)
    fotos = MultipleFileField(label="Fotos del lugar (opcional)")
    anexos = MultipleFileField(label="Planos u otros documentos (opcional)")

    def __init__(self, *args, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.usuario = usuario
        self.sedes = Sede.objects.filter(empresa=usuario.empresa, activa=True) if usuario else Sede.objects.none()
        self.fields["sede_nombre"].widget = ConSugerencias([s.nombre for s in self.sedes.order_by("nombre")],
                                                           attrs={"placeholder": "Escriba o elija la sede…"})
        if usuario and not self.is_bound:
            self.initial.update(contacto_nombre=usuario.get_full_name() or usuario.username, contacto_cargo=usuario.cargo,
                                contacto_telefono=usuario.telefono, contacto_email=usuario.email)
        self._agregar_dias()
        self.fields["fotos"].help_text = ayuda_adjuntos()
        self.fields["anexos"].help_text = ayuda_adjuntos()
        self._aplicar_estilos()

    def clean_fecha_max_propuesta(self):
        """La fecha para la que necesita la propuesta, si la da, debe ser futura."""
        return _fecha_futura(self.cleaned_data.get("fecha_max_propuesta"))

    def clean(self):
        """Resuelve la sede: por su id o por el nombre escrito (si no existe, se registra en su empresa)."""
        datos = super().clean()
        escrita = " ".join((datos.get("sede_nombre") or "").split())
        sede_id = str(self.data.get("sede", "")).strip()
        if sede_id.isdigit():  # compatibilidad con quien envía el id
            datos["sede"] = self.sedes.filter(pk=int(sede_id)).first()
        elif escrita and self.usuario and self.usuario.empresa_id:
            datos["sede"] = _por_nombre(self.sedes, "nombre", escrita) or Sede.objects.get_or_create(
                empresa=self.usuario.empresa, nombre=escrita[:150], defaults={"ciudad": self.usuario.empresa.ciudad})[0]
        else:
            datos["sede"] = None
        return datos


class InformeForm(BootstrapMixin, forms.Form):
    """Informe del requerimiento: lo que realmente quiere y necesita el cliente (base de la propuesta)."""

    necesidad = forms.CharField(label="¿Qué quiere y necesita el cliente?", widget=forms.Textarea(attrs={
        "rows": 3, "placeholder": "Ej.: vigilar el parqueadero y la entrada principal de noche, ver las cámaras desde el celular"}))
    solucion = forms.CharField(label="Solución recomendada", required=False, widget=forms.Textarea(attrs={
        "rows": 2, "placeholder": "Ej.: 3 cámaras bala IP con visión nocturna y un NVR de 4 canales"}))
    servicios = forms.CharField(label="Servicios a cotizar (uno por línea)", required=False, widget=forms.Textarea(attrs={
        "rows": 2, "placeholder": "1 Instalación y configuración de cámaras\n1 Desmonte del sistema de alarma"}))
    materiales = forms.CharField(label="Materiales y equipos a suministrar (uno por línea, con cantidad)", required=False,
                                 help_text="Lo que bodega alista si el cliente aprueba. Déjelo vacío si solo hay servicios.",
                                 widget=forms.Textarea(attrs={
        "rows": 4, "placeholder": "3 Cámara bala IP 4 MP\n1 NVR 4 canales PoE\n1 Disco duro 2 TB\n80 m Cable UTP cat 6"}))
    observaciones = forms.CharField(label="Condiciones del sitio y observaciones", required=False, widget=forms.Textarea(attrs={
        "rows": 2, "placeholder": "Energía, internet, alturas, marca que prefiere el cliente…"}))
    archivos = MultipleFileField(label="Fotos o documentos (opcional)")

    def __init__(self, *args, informe=None, **kwargs):
        super().__init__(*args, **kwargs)
        if informe is not None and not self.is_bound:
            self.initial.update(necesidad=informe.necesidad, solucion=informe.solucion, servicios=informe.servicios,
                                materiales=informe.materiales, observaciones=informe.observaciones)
        self._aplicar_estilos()

    def clean(self):
        """El informe debe traer qué cotizar: servicios, materiales y equipos, o ambos."""
        datos = super().clean()
        if not (datos.get("servicios") or "").strip() and not (datos.get("materiales") or "").strip():
            raise forms.ValidationError("Escriba los servicios a cotizar, los materiales y equipos a suministrar, o ambos.")
        return datos


class GestionComercialForm(BootstrapMixin, forms.Form):
    """Lo que el área comercial avanza mientras tiene la solicitud."""

    ETAPAS_COMERCIALES = [F.Etapa.GESTION, F.Etapa.ELABORANDO, F.Etapa.ENVIADA, F.Etapa.AJUSTES, F.Etapa.APROBADA,
                          F.Etapa.RECHAZADA, F.Etapa.CANCELADA]

    etapa = forms.ChoiceField(label="Etapa")
    comercial = forms.ModelChoiceField(label="Asesor comercial", required=False, queryset=Usuario.objects.none(),
                                       empty_label="— Sin asesor —")
    valor_propuesta = forms.DecimalField(label="Valor de la propuesta (antes de IVA)", required=False, min_value=0,
                                         max_digits=14, decimal_places=0)
    propuesta = forms.FileField(label="Propuesta económica (PDF)", required=False)
    motivo_rechazo = forms.CharField(label="Motivo (si no se aprobó)", required=False, max_length=300)
    plazo = forms.TypedChoiceField(label="Plazo de alquiler que aprobó el cliente", required=False, coerce=int, empty_value=None)
    cambios = forms.CharField(label="Cambios que pidió el cliente", required=False, widget=forms.Textarea(attrs={
        "rows": 2, "placeholder": "Ej.: prefiere cámaras Hikvision en lugar de Dahua; quitar el disco de 4 TB"}))
    mensaje = forms.CharField(label="Mensaje", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    visibilidad = forms.ChoiceField(choices=[("publico", "Al cliente (lo ve)"), ("interno", "Nota interna")],
                                    initial="interno", widget=forms.RadioSelect)

    # Qué etapas mueve cada área: administrativa arma y envía la propuesta; comercial registra lo que responde el
    # cliente y cancela. Dirección hace todo.
    ETAPAS_ADMINISTRATIVA = {F.Etapa.GESTION, F.Etapa.ELABORANDO, F.Etapa.ENVIADA}
    ETAPAS_COMERCIAL = {F.Etapa.GESTION, F.Etapa.ELABORANDO, F.Etapa.AJUSTES, F.Etapa.APROBADA, F.Etapa.RECHAZADA,
                        F.Etapa.CANCELADA}

    def __init__(self, *args, ficha=None, usuario=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.ficha = ficha
        etapas = list(self.ETAPAS_COMERCIALES)
        if usuario is not None and not usuario.es_direccion:
            propias = (self.ETAPAS_COMERCIAL if usuario.es_comercial else set()) | (
                self.ETAPAS_ADMINISTRATIVA if usuario.es_administrativo else set())
            etapas = [e for e in etapas if e in propias]
        if usuario is not None and not usuario.arma_propuestas:  # comercial ve el valor, pero no sube ni cambia la propuesta
            del self.fields["propuesta"], self.fields["valor_propuesta"]
        if ficha is not None and ficha.etapa not in etapas:
            etapas.insert(0, ficha.etapa)  # etapa actual (p. ej. levantamiento) aunque no se elija desde aquí
        if ficha is not None:  # paso a paso: solo la etapa actual y las que siguen
            from .pasos import etapas_permitidas
            permitidas = etapas_permitidas(ficha.etapa)
            etapas = [e for e in etapas if e in permitidas]
        self.fields["etapa"].choices = [(e, F.Etapa(e).label) for e in etapas]
        self.fields["comercial"].queryset = Usuario.objects.filter(
            Q(rol__in=[Usuario.Rol.COMERCIAL, Usuario.Rol.DIRECCION]) | Q(tambien_comercial=True), is_active=True)
        if ficha is not None and not self.is_bound:
            self.initial.update(etapa=ficha.etapa, comercial=ficha.comercial_id, valor_propuesta=ficha.valor_propuesta)
        # Alquiler o híbrido: al registrar «Aprobada» hay que decir el plazo que eligió el cliente
        self.opciones_alquiler = ficha.version_actual.opciones_alquiler() if ficha is not None and ficha.version_actual else []
        if self.opciones_alquiler:
            self.fields["plazo"].choices = [("", "— Elija el plazo —")] + [
                (f["plazo"], f"{f['plazo']} meses · canon ${f['canon']:,.0f} con IVA".replace(",", ".")) for f in self.opciones_alquiler]
        else:
            del self.fields["plazo"]
        self._aplicar_estilos()

    def clean_propuesta(self):
        """La propuesta adjunta debe ser un PDF."""
        f = self.cleaned_data.get("propuesta")
        if f and not f.name.lower().endswith(".pdf"):
            raise forms.ValidationError("La propuesta debe ser un PDF.")
        return f

    def clean(self):
        """Reglas de las etapas: sin informe no hay propuesta, enviarla exige el PDF, los cambios se
        registran sobre una propuesta enviada y la no aprobada lleva el motivo.
        """
        datos = super().clean()
        etapa, ficha = datos.get("etapa"), self.ficha
        cambia = ficha is not None and etapa != ficha.etapa
        if cambia and etapa in (F.Etapa.ELABORANDO, F.Etapa.ENVIADA) and not ficha.informe_listo:
            self.add_error("etapa", "Primero registre el informe del requerimiento: es la base de la propuesta.")
        if cambia and etapa == F.Etapa.ENVIADA and not datos.get("propuesta"):
            self.add_error("propuesta", "Adjunte el PDF de esta versión de la propuesta para enviarla al cliente.")
        if cambia and etapa == F.Etapa.AJUSTES:
            if ficha.etapa != F.Etapa.ENVIADA:
                self.add_error("etapa", "Los cambios se registran sobre una propuesta enviada.")
            elif not (datos.get("cambios") or "").strip():
                self.add_error("cambios", "Escriba los cambios que pidió el cliente.")
        if cambia and etapa == F.Etapa.APROBADA and self.opciones_alquiler and not datos.get("plazo"):
            self.add_error("plazo", "La propuesta es de alquiler: elija a cuántos meses la aprobó el cliente.")
        if etapa == F.Etapa.RECHAZADA and not datos.get("motivo_rechazo") and not (ficha and ficha.motivo_rechazo):
            self.add_error("motivo_rechazo", "Indique por qué no se aprobó (sirve para mejorar las próximas).")
        return datos


# --- Cotizador y catálogo de precios ---------------------------------------------------
class LineaCotizacionForm(BootstrapMixin, forms.ModelForm):
    """Una línea del cotizador: descripción (con el catálogo sugerido), categoría, cantidad, costo, descuento e IVA."""
    class Meta:
        from .models import LineaCotizacion
        model = LineaCotizacion
        fields = ["descripcion", "categoria", "cantidad", "valor_unitario", "descuento", "iva", "proveedor", "marca", "referencia"]
        widgets = {
            "descripcion": forms.TextInput(attrs={"list": "catalogo-precios", "placeholder": "Busque en el catálogo o escriba…",
                                                  "class": "descripcion"}),
            "marca": forms.TextInput(attrs={"placeholder": "Marca", "class": "marca"}),
            "referencia": forms.TextInput(attrs={"placeholder": "Referencia", "class": "referencia"}),
            "cantidad": forms.NumberInput(attrs={"step": "any", "min": "0", "class": "cantidad"}),
            "valor_unitario": forms.NumberInput(attrs={"step": "any", "min": "0", "class": "valor"}),
            "descuento": forms.NumberInput(attrs={"step": "any", "min": "0", "max": "100", "class": "descuento"}),
            "iva": forms.NumberInput(attrs={"step": "any", "min": "0", "max": "100", "class": "iva"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()
        self.fields["categoria"].choices = [("", "Automática")] + list(self.fields["categoria"].choices)[1:]
        self.fields["categoria"].widget.attrs["class"] = "categoria form-select"
        self.fields["proveedor"].empty_label = "Proveedor…"
        self.fields["proveedor"].widget.attrs["class"] = "proveedor form-select"
        self.fields["proveedor"].queryset = self.fields["proveedor"].queryset.order_by("razon_social")
        for campo in self.fields.values():
            campo.widget.attrs["class"] = (campo.widget.attrs.get("class", "") + " form-control-sm").strip()


def formset_lineas():
    """El conjunto de líneas de una cotización (se agregan y se quitan desde el cotizador)."""
    from django.forms import inlineformset_factory

    from .models import Cotizacion, LineaCotizacion
    return inlineformset_factory(Cotizacion, LineaCotizacion, form=LineaCotizacionForm, extra=0, can_delete=True)


class CotizacionForm(BootstrapMixin, forms.ModelForm):
    """Los datos generales de la propuesta: alcance, condiciones, modalidad, margen y los del alquiler."""
    class Meta:
        from .models import Cotizacion
        model = Cotizacion
        fields = ["alcance", "condiciones", "modalidad", "margen", "tasa_mensual", "recargo_alquiler", "plazos",
                  "opcion_compra", "consecutivo"]
        widgets = {"alcance": forms.Textarea(attrs={"rows": 2}), "condiciones": forms.Textarea(attrs={"rows": 3}),
                   "margen": forms.NumberInput(attrs={"min": "1", "max": "99", "step": "any"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()
        self.fields["consecutivo"].label = "Consecutivo (NNN)"
        self.fields["consecutivo"].widget.attrs.update({"min": "1", "max": "999"})
        if self.instance.pk and self.instance.ficha.versiones.exclude(codigo="").exists():
            # Ya salió la primera versión: el código queda fijo
            self.fields["consecutivo"].disabled = True

    def clean_consecutivo(self):
        """Del 1 al 999 y sin repetir el de otra propuesta del año."""
        from django.utils import timezone

        from .cotizador import consecutivos_usados

        n = self.cleaned_data.get("consecutivo")
        if n is None or self.fields["consecutivo"].disabled:
            return n
        if not 1 <= n <= 999:
            raise forms.ValidationError("Escriba un número del 1 al 999.")
        if n in consecutivos_usados(timezone.localdate().year, excepto=self.instance.ficha):
            raise forms.ValidationError(f"El {n:03d} ya lo usa otra propuesta de este año.")
        return n


class ItemCatalogoForm(BootstrapMixin, forms.ModelForm):
    """Un ítem del catálogo de precios, con su imagen de referencia (sale en el informe de requerimientos)."""
    class Meta:
        from .models import ItemCatalogo
        model = ItemCatalogo
        fields = ["descripcion", "tipo", "unidad", "valor_unitario", "iva", "marca", "referencia", "proveedor", "imagen", "activo"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._aplicar_estilos()
