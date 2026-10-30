"""Ficha comercial de un ticket: la parte de la solicitud que gestiona el área comercial.

Un solo ticket (un solo número) acompaña al cliente de principio a fin:
- Nace en el área comercial (lo radica un asesor, el cliente o viene de una oportunidad del acta).
- Siempre hay un informe del requerimiento (con o sin visita de levantamiento): es la base de la propuesta.
- Si hace falta validar en sitio, pasa al área técnica para un levantamiento y vuelve a comercial con el informe.
- La propuesta puede tener varias versiones: el cliente aprueba, no aprueba o pide cambios (otra marca…).
- Con la propuesta aprobada, se traspasa al área técnica para la instalación y sigue el flujo técnico.

El ticket (app «tickets») guarda lo común: contacto, días en que no puede atender, visita, técnico,
historial y adjuntos. Aquí va lo propio de la venta.
"""
import re
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import reverse

from tickets.models import OportunidadComercial, Ticket


class FichaComercial(models.Model):
    class Etapa(models.TextChoices):
        GESTION = "gestion", "En gestión comercial"
        LEVANTAMIENTO = "levantamiento", "Visita técnica de levantamiento"
        ELABORANDO = "elaborando", "Elaborando propuesta"
        ENVIADA = "enviada", "Propuesta enviada"
        AJUSTES = "ajustes", "Ajustando propuesta (cambios del cliente)"
        APROBADA = "aprobada", "Propuesta aprobada"
        RECHAZADA = "rechazada", "Propuesta no aprobada"
        INSTALACION = "instalacion", "Traspasado al área técnica para instalación"
        CANCELADA = "cancelada", "Cancelada"

    class Origen(models.TextChoices):
        COMERCIAL = "comercial", "Asesor comercial"
        CLIENTE = "cliente", "Portal del cliente"
        OPORTUNIDAD = "oportunidad", "Oportunidad detectada por un técnico"

    class SiNo(models.TextChoices):
        SI = "si", "Sí"
        NO = "no", "No"
        NO_SABE = "no_sabe", "No sabe"

    class NivelDecision(models.TextChoices):
        DECIDE = "decide", "Decide (aprueba la compra)"
        INFLUYE = "influye", "Influye en la decisión"
        USUARIO = "usuario", "Usuario / informa"

    ETAPAS_ABIERTAS = [Etapa.GESTION, Etapa.LEVANTAMIENTO, Etapa.ELABORANDO, Etapa.ENVIADA, Etapa.AJUSTES,
                       Etapa.APROBADA]
    # Lo que ve el cliente, de principio a fin (la última parte es el flujo técnico de instalación)
    PASOS_CLIENTE = ["Solicitud recibida", "Informe del requerimiento", "Propuesta", "Aprobada", "Instalación", "Cerrado"]

    ticket = models.OneToOneField(Ticket, on_delete=models.CASCADE, related_name="ficha")
    etapa = models.CharField(max_length=20, choices=Etapa.choices, default=Etapa.GESTION)
    origen = models.CharField(max_length=20, choices=Origen.choices, default=Origen.COMERCIAL)
    comercial = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="fichas_comerciales", verbose_name="asesor comercial")
    # Contacto y ubicación (complementa los datos de contacto del ticket)
    contacto_cargo = models.CharField("cargo / rol del contacto", max_length=100, blank=True)
    municipio = models.CharField("municipio / ubicación", max_length=150, blank=True)
    direccion = models.CharField("dirección", max_length=200, blank=True)
    # Calificación técnica preliminar
    sistema_actual = models.CharField("¿sistema actual? ¿marca?", max_length=150, blank=True)
    energia = models.CharField("¿energía en el sitio?", max_length=10, choices=SiNo.choices, blank=True)
    internet = models.CharField("¿internet en el sitio?", max_length=10, choices=SiNo.choices, blank=True)
    tamano = models.CharField("tamaño / n.º de áreas o puntos", max_length=150, blank=True)
    # Oportunidad de negocio (interno del área comercial)
    servicios = models.JSONField("servicios de interés", default=list, blank=True)
    otras_necesidades = models.CharField("«¿y qué más le preocupa?» · otras necesidades", max_length=300, blank=True)
    nivel_decision = models.CharField("nivel de decisión del contacto", max_length=10, choices=NivelDecision.choices, blank=True)
    otras_sedes = models.BooleanField("¿otras sedes / fincas / locales?", default=False)
    presupuesto = models.CharField("presupuesto / tiempos mencionados", max_length=200, blank=True)
    observaciones = models.TextField("observaciones (interno comercial)", blank=True)
    fecha_max_propuesta = models.DateTimeField("fecha máxima para la propuesta", null=True, blank=True)
    # Lo que devuelve el área técnica tras el levantamiento
    levantamiento = models.TextField("resultado del levantamiento técnico", blank=True)
    # Propuesta económica
    valor_propuesta = models.DecimalField("valor de la propuesta (antes de IVA)", max_digits=14, decimal_places=0,
                                          null=True, blank=True)
    fecha_envio = models.DateTimeField(null=True, blank=True)
    fecha_respuesta = models.DateTimeField(null=True, blank=True)
    motivo_rechazo = models.CharField(max_length=300, blank=True)
    oportunidad = models.OneToOneField(OportunidadComercial, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="ficha")
    referencia_externa = models.CharField("radicado anterior (ficha Netlify)", max_length=60, blank=True, db_index=True)
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "ficha comercial"
        verbose_name_plural = "fichas comerciales"
        indexes = [models.Index(fields=["etapa"])]

    def __str__(self):
        return f"Ficha comercial · {self.ticket.radicado}"

    def get_absolute_url(self):
        return reverse("comercial:detalle", args=[self.ticket.radicado])

    @property
    def abierta(self):
        return self.etapa in self.ETAPAS_ABIERTAS

    @property
    def servicios_display(self):
        nombres = dict(OportunidadComercial.SERVICIOS)
        return [nombres.get(s, s) for s in self.servicios]

    @property
    def color_etapa(self):
        return {"gestion": "secondary", "levantamiento": "info", "elaborando": "primary", "enviada": "warning",
                "ajustes": "primary", "aprobada": "success", "rechazada": "danger", "instalacion": "dark", "cancelada": "dark"}[self.etapa]

    @property
    def propuesta_vencida(self):
        from django.utils import timezone

        return bool(self.fecha_max_propuesta and self.etapa in (self.Etapa.GESTION, self.Etapa.LEVANTAMIENTO,
                                                                self.Etapa.ELABORANDO, self.Etapa.AJUSTES)
                    and self.fecha_max_propuesta < timezone.now())

    @property
    def paso_cliente(self):
        """Índice en PASOS_CLIENTE (-1 si no aprobó o se canceló)."""
        if self.etapa in (self.Etapa.RECHAZADA, self.Etapa.CANCELADA):
            return -1
        if self.etapa == self.Etapa.INSTALACION:
            return 5 if self.ticket.estado == Ticket.Estado.CERRADO else 4
        return {"gestion": 0, "levantamiento": 1, "elaborando": 2, "enviada": 2, "ajustes": 2, "aprobada": 3}[self.etapa]

    @property
    def informe_listo(self):
        return InformeRequerimiento.objects.filter(ficha=self).exists()

    @property
    def version_actual(self):
        """La última versión enviada de la propuesta (o None)."""
        return self.versiones.order_by("-numero").first()


class InformeRequerimiento(models.Model):
    """Lo que realmente quiere y necesita el cliente, con o sin visita de levantamiento.

    Es obligatorio: con él el área comercial cotiza los materiales y equipos de la propuesta.
    Con visita lo diligencia el técnico; sin visita, el asesor comercial o el área técnica.
    """

    ficha = models.OneToOneField(FichaComercial, on_delete=models.CASCADE, related_name="informe")
    con_visita = models.BooleanField("se hizo visita de levantamiento", default=False)
    necesidad = models.TextField("qué quiere y necesita el cliente")
    solucion = models.TextField("solución recomendada", blank=True)
    servicios = models.TextField("servicios a cotizar", blank=True,
                                 help_text="Uno por línea (desmonte, instalación, mantenimiento…): se cotizan, no pasan por bodega.")
    materiales = models.TextField("materiales y equipos a suministrar", blank=True,
                                  help_text="Uno por línea, con cantidad: es lo que bodega alista si el cliente aprueba.")
    observaciones = models.TextField("condiciones del sitio y observaciones", blank=True)
    elaborado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "informe del requerimiento"
        verbose_name_plural = "informes del requerimiento"

    def __str__(self):
        return f"Informe · {self.ficha.ticket.radicado}"


class PropuestaVersion(models.Model):
    """Cada propuesta enviada al cliente. Si pide cambios (otra marca, otro alcance) se envía una versión nueva."""

    class Estado(models.TextChoices):
        ENVIADA = "enviada", "Enviada, esperando respuesta"
        CAMBIOS = "cambios", "El cliente pidió cambios"
        APROBADA = "aprobada", "Aprobada"
        RECHAZADA = "rechazada", "No aprobada"

    ficha = models.ForeignKey(FichaComercial, on_delete=models.CASCADE, related_name="versiones")
    numero = models.PositiveSmallIntegerField("versión")
    codigo = models.CharField("código de la propuesta", max_length=40, blank=True, db_index=True,
                              help_text="PE-AAAA-MM-DD-Vn, como en las propuestas en papel.")
    valor = models.DecimalField("valor (antes de IVA)", max_digits=14, decimal_places=0, null=True, blank=True)
    archivo = models.ForeignKey("tickets.Adjunto", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.ENVIADA)
    cambios = models.TextField("cambios que pidió el cliente", blank=True)
    motivo = models.CharField("motivo si no se aprobó", max_length=300, blank=True)
    enviada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    enviada = models.DateTimeField()
    respondida = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["numero"]
        unique_together = [("ficha", "numero")]
        verbose_name = "versión de la propuesta"
        verbose_name_plural = "versiones de la propuesta"

    def __str__(self):
        return f"Propuesta v{self.numero} · {self.ficha.ticket.radicado}"

    @property
    def color(self):
        return {"enviada": "warning", "cambios": "primary", "aprobada": "success", "rechazada": "danger"}[self.estado]


def ruta_archivo(instancia, nombre):
    """Se conserva solo porque la migración 0001 la referencia (el modelo de archivos ya no existe)."""
    import os
    import uuid

    from django.utils import timezone

    return f"comercial/{timezone.localdate():%Y/%m}/{uuid.uuid4().hex}{os.path.splitext(nombre)[1].lower()}"


class ItemCatalogo(models.Model):
    """Precio de referencia de un material, equipo o servicio, para armar propuestas sin volver a digitarlo."""

    class Tipo(models.TextChoices):
        MATERIAL = "material", "Material o equipo"
        SERVICIO = "servicio", "Servicio / mano de obra"

    descripcion = models.CharField("descripción", max_length=250, unique=True)
    tipo = models.CharField(max_length=10, choices=Tipo.choices, default=Tipo.MATERIAL)
    unidad = models.CharField(max_length=20, blank=True, help_text="und, m, global…")
    valor_unitario = models.DecimalField("valor unitario (antes de IVA)", max_digits=14, decimal_places=2)
    iva = models.DecimalField("IVA %", max_digits=5, decimal_places=2, default=19)
    fuente = models.CharField("de dónde salió el precio", max_length=120, blank=True,
                              help_text="Por ejemplo, la propuesta en la que se cotizó (PE-2026-07-08-V1).")
    activo = models.BooleanField(default=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["tipo", "descripcion"]
        verbose_name = "ítem del catálogo de precios"
        verbose_name_plural = "catálogo de precios"

    def __str__(self):
        return self.descripcion


class Cotizacion(models.Model):
    """Los ítems de una versión de la propuesta. Mientras no se envía es un borrador; al enviarse queda fija."""

    class Modalidad(models.TextChoices):
        VENTA = "venta", "Venta (pago único)"
        MONITOREO = "monitoreo", "Monitoreo mensual (AIU)"
        ALQUILER = "alquiler", "Alquiler con opción de compra"

    VIGENCIA = "Vigencia de la propuesta: siete (7) días hábiles."
    CONDICIONES = ("Valores en pesos colombianos. IVA del 19 % discriminado.\n"
                   "Garantía de equipos: un (1) año por defectos de fábrica.\n"
                   "La instalación se programa una vez aprobada la propuesta, según disponibilidad del cliente.")

    ficha = models.ForeignKey(FichaComercial, on_delete=models.CASCADE, related_name="cotizaciones")
    version = models.OneToOneField(PropuestaVersion, on_delete=models.SET_NULL, null=True, blank=True, related_name="cotizacion")
    alcance = models.TextField("alcance de la propuesta", blank=True,
                               help_text="Qué se va a hacer, en palabras del cliente (sale en el PDF).")
    condiciones = models.TextField(default=CONDICIONES)
    # Forma de cobro: venta (pago único), monitoreo mensual con AIU o alquiler con opción de compra
    modalidad = models.CharField(max_length=10, choices=Modalidad.choices, default=Modalidad.VENTA)
    aiu_utilidad = models.DecimalField("utilidad del AIU %", max_digits=5, decimal_places=2, default=Decimal("10"),
                                       help_text="En el monitoreo el IVA se cobra solo sobre la utilidad.")
    tasa_mensual = models.DecimalField("tasa mensual %", max_digits=5, decimal_places=2, default=Decimal("1.5"),
                                       help_text="Interés del alquiler: el canon es una cuota fija a esta tasa.")
    plazos = models.CharField("plazos en meses", max_length=30, default="12,24,36", help_text="Separados por coma.")
    opcion_compra = models.DecimalField("opción de compra %", max_digits=5, decimal_places=2, default=Decimal("1"),
                                        help_text="Lo que paga el cliente al final para quedarse con los equipos.")
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "cotización"
        verbose_name_plural = "cotizaciones"

    def __str__(self):
        return f"Cotización · {self.ficha.ticket.radicado}"

    @property
    def enviada(self):
        return self.version_id is not None

    def totales(self):
        from decimal import Decimal
        t = {"bruto": Decimal(0), "descuento": Decimal(0), "base": Decimal(0), "iva": Decimal(0), "total": Decimal(0),
             "ahorro": Decimal(0)}
        aiu = self.aiu_utilidad if self.modalidad == self.Modalidad.MONITOREO else None
        for linea in self.lineas.all():
            for clave, valor in linea.calculo(aiu).items():
                t[clave] += valor
        return t

    def lista_plazos(self):
        return sorted({int(p) for p in re.findall(r"\d+", self.plazos) if 0 < int(p) <= 120})

    def canones(self):
        """Alquiler: canon mensual fijo por plazo (cuota a la tasa mensual sobre el valor con IVA) y la opción de compra."""
        from decimal import ROUND_HALF_UP

        valor = self.totales()["total"]
        r = self.tasa_mensual / 100
        filas = []
        for n in self.lista_plazos():
            canon = valor / n if not r else valor * r / (1 - (1 + r) ** -n)
            canon = canon.quantize(Decimal("0.01"), ROUND_HALF_UP)
            filas.append({"plazo": n, "canon": canon, "total": canon * n})
        compra = (valor * self.opcion_compra / 100).quantize(Decimal("0.01"), ROUND_HALF_UP)
        return {"valor": valor, "filas": filas, "opcion_compra": compra}


class LineaCotizacion(models.Model):
    cotizacion = models.ForeignKey(Cotizacion, on_delete=models.CASCADE, related_name="lineas")
    item = models.ForeignKey(ItemCatalogo, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    descripcion = models.CharField("descripción", max_length=250)
    cantidad = models.DecimalField(max_digits=12, decimal_places=2, default=1)
    valor_unitario = models.DecimalField("valor unitario (antes de IVA)", max_digits=14, decimal_places=2)
    descuento = models.DecimalField("descuento %", max_digits=5, decimal_places=2, default=0)
    iva = models.DecimalField("IVA %", max_digits=5, decimal_places=2, default=19)
    orden = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["orden", "id"]

    def __str__(self):
        return f"{self.cantidad:g} {self.descripcion}"

    def calculo(self, aiu_utilidad=None):
        """Valores de la línea. Con AIU (monitoreo) el IVA va solo sobre la utilidad: base × utilidad % × IVA %."""
        from decimal import ROUND_HALF_UP, Decimal
        dos = Decimal("0.01")
        bruto = (self.cantidad * self.valor_unitario).quantize(dos, ROUND_HALF_UP)
        descuento = (bruto * self.descuento / 100).quantize(dos, ROUND_HALF_UP)
        base = bruto - descuento
        tasa = self.iva / 100 * (aiu_utilidad / 100 if aiu_utilidad is not None else 1)
        iva = (base * tasa).quantize(dos, ROUND_HALF_UP)
        ahorro = (descuento * (1 + tasa)).quantize(dos, ROUND_HALF_UP)  # lo que el cliente deja de pagar, con IVA
        return {"bruto": bruto, "descuento": descuento, "base": base, "iva": iva, "total": base + iva, "ahorro": ahorro}


class ServicioMonitoreo(models.Model):
    """Lo que el cliente paga cada mes por el monitoreo de una sede (contrato de monitoreo).

    El valor se ajusta una vez al año; cada ajuste queda en AjusteMonitoreo para saber de dónde salió el valor vigente.
    """

    DESCRIPCION = "Servicio de monitoreo de alarma 24 horas permanente"

    sede = models.ForeignKey("accounts.Sede", on_delete=models.CASCADE, related_name="monitoreos")
    descripcion = models.CharField("descripción", max_length=200, default=DESCRIPCION)
    cantidad = models.PositiveSmallIntegerField(default=1)
    valor_mensual = models.DecimalField("valor mensual unitario", max_digits=14, decimal_places=2)
    anio_valor = models.PositiveSmallIntegerField("año del valor", help_text="Año en que rige el valor mensual.")
    activo = models.BooleanField(default=True, help_text="Desmarque si la sede dejó de tener el servicio.")
    observaciones = models.CharField(max_length=300, blank=True)
    fuente = models.CharField("de dónde salió", max_length=150, blank=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sede__empresa__razon_social", "sede__nombre"]
        verbose_name = "servicio de monitoreo"
        verbose_name_plural = "contratos de monitoreo"

    def __str__(self):
        return f"{self.sede.empresa} · {self.sede.nombre}"

    @property
    def total_mensual(self):
        return self.valor_mensual * self.cantidad


class AjusteMonitoreo(models.Model):
    """Incremento anual aplicado al valor de un servicio de monitoreo."""

    servicio = models.ForeignKey(ServicioMonitoreo, on_delete=models.CASCADE, related_name="ajustes")
    anio = models.PositiveSmallIntegerField("año")
    porcentaje = models.DecimalField("incremento %", max_digits=6, decimal_places=2)
    valor_anterior = models.DecimalField(max_digits=14, decimal_places=2)
    valor_nuevo = models.DecimalField(max_digits=14, decimal_places=2)
    nota = models.CharField(max_length=200, blank=True)
    aplicado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    aplicado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-anio", "-aplicado"]
        verbose_name = "ajuste anual de monitoreo"
        verbose_name_plural = "ajustes anuales de monitoreo"
