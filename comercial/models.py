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
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse

from tickets.models import OportunidadComercial, Ticket


class FichaComercial(models.Model):
    """La ficha del requerimiento comercial: va pegada al ticket y lo acompaña de principio a fin.

    Guarda lo que necesita el cliente (contacto, sitio, servicios, presupuesto), la etapa de la propuesta y el
    asesor. El mismo ticket pasa a técnica para el levantamiento y, aprobada la propuesta, a bodega e instalación.
    """
    class Etapa(models.TextChoices):
        """Etapas de la propuesta, de la gestión inicial a la instalación (o rechazo / cancelación)."""
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
        """De dónde salió el requerimiento: el asesor, el portal del cliente o una oportunidad del técnico."""
        COMERCIAL = "comercial", "Asesor comercial"
        CLIENTE = "cliente", "Portal del cliente"
        OPORTUNIDAD = "oportunidad", "Oportunidad detectada por un técnico"

    class SiNo(models.TextChoices):
        """Respuesta de sí / no / no sabe para las preguntas del sitio (energía, internet…)."""
        SI = "si", "Sí"
        NO = "no", "No"
        NO_SABE = "no_sabe", "No sabe"

    class NivelDecision(models.TextChoices):
        """Qué tanto decide el contacto sobre la compra."""
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
        """La ficha en el área comercial."""
        return reverse("comercial:detalle", args=[self.ticket.radicado])

    @property
    def abierta(self):
        """True mientras la propuesta sigue en curso (no rechazada, cancelada ni traspasada)."""
        return self.etapa in self.ETAPAS_ABIERTAS

    @property
    def servicios_display(self):
        """Los servicios de interés con su nombre para mostrar."""
        nombres = dict(OportunidadComercial.SERVICIOS)
        return [nombres.get(s, s) for s in self.servicios]

    @property
    def color_etapa(self):
        """Color del distintivo de la etapa (clases de Bootstrap)."""
        return {"gestion": "secondary", "levantamiento": "info", "elaborando": "primary", "enviada": "warning",
                "ajustes": "primary", "aprobada": "success", "rechazada": "danger", "instalacion": "dark", "cancelada": "dark"}[self.etapa]

    @property
    def propuesta_vencida(self):
        """True si pasó la fecha máxima que pidió el cliente y la propuesta aún no se ha enviado."""
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
        """True si ya se registró el informe del requerimiento (la base de la propuesta)."""
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
        """Qué respondió el cliente a esta versión."""
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
    # Entrega por medio de comercial: con algunos clientes el asesor la lleva en persona en vez de que salga por correo
    por_comercial = models.BooleanField(
        "se entrega por medio de comercial", default=False,
        help_text="Dirección la aprobó para que el asesor comercial se la entregue al cliente (no sale por correo).",
    )
    entregada_cliente = models.DateField("entregada al cliente", null=True, blank=True)
    entregada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="+", verbose_name="entregada por")

    class Meta:
        ordering = ["numero"]
        unique_together = [("ficha", "numero")]
        verbose_name = "versión de la propuesta"
        verbose_name_plural = "versiones de la propuesta"

    def __str__(self):
        return f"Propuesta v{self.numero} · {self.ficha.ticket.radicado}"

    @property
    def color(self):
        """Color del distintivo del estado (clases de Bootstrap)."""
        return {"enviada": "warning", "cambios": "primary", "aprobada": "success", "rechazada": "danger"}[self.estado]

    @property
    def por_entregar(self):
        """Aprobada para entrega por comercial y el asesor aún no la ha entregado al cliente."""
        return self.por_comercial and not self.entregada_cliente and self.estado == self.Estado.ENVIADA


def ruta_archivo(instancia, nombre):
    """Se conserva solo porque la migración 0001 la referencia (el modelo de archivos ya no existe)."""
    import os
    import uuid

    from django.utils import timezone

    return f"comercial/{timezone.localdate():%Y/%m}/{uuid.uuid4().hex}{os.path.splitext(nombre)[1].lower()}"


def ruta_imagen_catalogo(instancia, nombre):
    """Ruta de la imagen de referencia en media/: carpeta del catálogo y nombre aleatorio."""
    import os
    import uuid

    return f"comercial/catalogo/{uuid.uuid4().hex}{os.path.splitext(nombre)[1].lower()}"


class ItemCatalogo(models.Model):
    """Precio de referencia de un material, equipo o servicio, para armar propuestas sin volver a digitarlo."""

    class Tipo(models.TextChoices):
        """Material o equipo (va a bodega) o servicio / mano de obra (solo se cotiza)."""
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
    # Foto del producto: sale en la tabla de equipos del informe de requerimientos técnicos
    imagen = models.ImageField("imagen de referencia", upload_to=ruta_imagen_catalogo, blank=True,
                               help_text="Foto del equipo (jpg o png). Sale en el informe de requerimientos técnicos.")
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
        """Forma de cobro de la propuesta: venta, alquiler o híbrido."""
        VENTA = "venta", "Venta (pago único)"
        ALQUILER = "alquiler", "Alquiler (mano de obra sumada al canon)"
        HIBRIDO = "hibrido", "Híbrido (alquiler de equipos y venta de mano de obra)"

    VIGENCIA = "Vigencia de la propuesta: siete (7) días hábiles."
    CONDICIONES = ("La garantía de los equipos es de un (1) año. Todas las garantías son nulas si los productos han sido mal "
                   "aplicados, mal usados, maltratados, dañados, o retirados del sitio original de la instalación. La Garantía "
                   "no cubre daños por fallas del fluido eléctrico, mal manejo o maltrato a los equipos por parte de los usuarios.")

    ficha = models.ForeignKey(FichaComercial, on_delete=models.CASCADE, related_name="cotizaciones")
    version = models.OneToOneField(PropuestaVersion, on_delete=models.SET_NULL, null=True, blank=True, related_name="cotizacion")
    alcance = models.TextField("alcance de la propuesta", blank=True,
                               help_text="Qué se va a hacer, en palabras del cliente (sale en el PDF).")
    condiciones = models.TextField(default=CONDICIONES)
    # Forma de cobro: venta (pago único), alquiler o híbrido (la mano de obra se cobra aparte)
    modalidad = models.CharField(max_length=10, choices=Modalidad.choices, default=Modalidad.VENTA)
    margen = models.DecimalField("margen de utilidad %", max_digits=5, decimal_places=2, null=True, blank=True,
                                 validators=[MinValueValidator(1), MaxValueValidator(99)],
                                 help_text="Porcentaje del precio de venta que queda de utilidad: precio = costo ÷ (1 − margen). "
                                           "Es obligatorio para enviar; el cliente solo ve el precio final.")
    tasa_mensual = models.DecimalField("tasa mensual %", max_digits=5, decimal_places=2, default=Decimal("2"),
                                       help_text="Interés del alquiler: el canon es una cuota fija a esta tasa.")
    recargo_alquiler = models.DecimalField("recargo del alquiler %", max_digits=5, decimal_places=2, default=Decimal("2"),
                                           help_text="Se suma al valor de los bienes antes de calcular el canon.")
    plazos = models.CharField("plazos en meses", max_length=30, default="6,12,24,36,48", help_text="Separados por coma.")
    opcion_compra = models.DecimalField("opción de compra %", max_digits=5, decimal_places=2, default=0,
                                        help_text="Lo que paga el cliente al final para quedarse con los equipos. 0 = sin opción de compra.")
    # Revisión de dirección: comercial la envía, dirección la aprueba (y sale al cliente) o la devuelve con observaciones.
    # Si es urgente, comercial la envía sin esperar y queda el motivo.
    en_revision = models.BooleanField("en revisión de dirección", default=False)
    observaciones_direccion = models.TextField("observaciones de dirección", blank=True)
    aprobada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+", verbose_name="aprobada por")
    motivo_urgencia = models.CharField("motivo del envío urgente", max_length=500, blank=True)
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "cotización"
        verbose_name_plural = "cotizaciones"

    def __str__(self):
        return f"Cotización · {self.ficha.ticket.radicado}"

    @property
    def es_alquiler(self):
        """True si se cobra con canon mensual (alquiler o híbrido)."""
        return self.modalidad in (self.Modalidad.ALQUILER, self.Modalidad.HIBRIDO)

    @property
    def mano_obra_en_canon(self):
        """True si la mano de obra va sumada al canon (solo en alquiler puro)."""
        return self.modalidad == self.Modalidad.ALQUILER

    @property
    def enviada(self):
        """True si ya salió como versión de la propuesta (queda fija)."""
        return self.version_id is not None

    def totales(self):
        """Bruto, descuento, base, IVA, total y ahorro de todas las líneas (ya con el margen)."""
        from decimal import Decimal
        t = {"bruto": Decimal(0), "descuento": Decimal(0), "base": Decimal(0), "iva": Decimal(0), "total": Decimal(0),
             "ahorro": Decimal(0)}
        for linea in self.lineas.all():
            for clave, valor in linea.calculo().items():
                t[clave] += valor
        return t

    def lista_plazos(self):
        """Los plazos del alquiler en meses, ordenados y sin repetir (de 1 a 120)."""
        return sorted({int(p) for p in re.findall(r"\d+", self.plazos) if 0 < int(p) <= 120})

    def canones(self):
        """Alquiler: canon mensual fijo por plazo y, si la hay, la opción de compra.

        La cuota sale de los bienes más el recargo, a la tasa mensual; el IVA se suma a cada canon. La mano de obra se
        cobra aparte, salvo que se incluya en el canon (así se calculó PE-2026-10-007: tasa 2 %, recargo 2 %).
        """
        from decimal import ROUND_HALF_UP

        dos = Decimal("0.01")
        bienes = iva_bienes = mano_obra = Decimal(0)
        for linea in self.lineas.all():
            c = linea.calculo()
            if linea.categoria_final == LineaCotizacion.Categoria.MANO_OBRA and not self.mano_obra_en_canon:
                mano_obra += c["total"]
            else:
                bienes += c["base"]
                iva_bienes += c["iva"]
        tasa_iva = iva_bienes / bienes if bienes else Decimal(0)
        financiado = bienes * (1 + self.recargo_alquiler / 100)
        r = self.tasa_mensual / 100
        filas = []
        for n in self.lista_plazos():
            antes_iva = (financiado / n if not r else financiado * r / (1 - (1 + r) ** -n)).quantize(dos, ROUND_HALF_UP)
            iva = (antes_iva * tasa_iva).quantize(dos, ROUND_HALF_UP)
            filas.append({"plazo": n, "antes_iva": antes_iva, "iva": iva, "canon": antes_iva + iva, "total": (antes_iva + iva) * n})
        valor = bienes + iva_bienes
        compra = (valor * self.opcion_compra / 100).quantize(dos, ROUND_HALF_UP)
        return {"valor": valor, "mano_obra": mano_obra, "filas": filas, "opcion_compra": compra}


class LineaCotizacion(models.Model):
    """Un ítem de la cotización. Guarda el costo; el precio al cliente sale del costo con el margen."""
    class Categoria(models.TextChoices):
        """Grupo con el que la línea sale en el PDF de la propuesta (y que define si es mano de obra)."""
        EQUIPOS = "equipos", "Equipos"
        ENERGIA = "energia", "Energía"
        LICENCIAS = "licencias", "Licencias"
        CONECTIVIDAD = "conectividad", "Conectividad y comunicaciones"
        ACCESORIOS = "accesorios", "Accesorios"
        MANO_OBRA = "mano_obra", "Mano de obra"

    # Sin categoría elegida, se deduce de la descripción; lo que no coincida es un equipo
    PALABRAS_CATEGORIA = (
        ("mano_obra", ("servicio", "instalaci", "configuraci", "mano de obra", "traslado", "adecuaci", "mantenimiento", "soporte t")),
        ("energia", ("fuente", "bater", "ups", "energ", "regulador", "transformador")),
        ("licencias", ("licencia", "software", "suscripci")),
        ("conectividad", ("cable", "switch", "router", "fibra", "conector", "patch", "antena")),
        ("accesorios", ("bot[oó]n", "caja", "canaleta", "tubo", "soporte", "accesorio", "tornillo", "chazo", "amarre")),
    )

    cotizacion = models.ForeignKey(Cotizacion, on_delete=models.CASCADE, related_name="lineas")
    item = models.ForeignKey(ItemCatalogo, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    descripcion = models.CharField("descripción", max_length=250)
    categoria = models.CharField("categoría", max_length=15, choices=Categoria.choices, blank=True)
    cantidad = models.DecimalField(max_digits=12, decimal_places=2, default=1)
    valor_unitario = models.DecimalField("valor unitario (antes de IVA)", max_digits=14, decimal_places=2)
    descuento = models.DecimalField("descuento %", max_digits=5, decimal_places=2, default=0)
    iva = models.DecimalField("IVA %", max_digits=5, decimal_places=2, default=19)
    orden = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["orden", "id"]

    def __str__(self):
        return f"{self.cantidad:g} {self.descripcion}"

    @property
    def categoria_final(self):
        """La categoría elegida o, si quedó en «Automática», la que sugieren las palabras de la descripción."""
        if self.categoria:
            return self.categoria
        texto = self.descripcion.lower()
        for categoria, palabras in self.PALABRAS_CATEGORIA:
            if re.search(r"\b(" + "|".join(palabras) + ")", texto):
                return categoria
        return self.Categoria.EQUIPOS

    def _precio_exacto(self):
        """Precio sin redondear: costo ÷ (1 − margen). Sin margen válido, el valor tal cual."""
        margen = self.cotizacion.margen
        if not margen or margen >= 100:  # Fuera de rango llega solo mientras el formulario muestra el error
            return self.valor_unitario  # Las propuestas anteriores al margen conservan sus valores
        return self.valor_unitario / (1 - margen / 100)

    @property
    def precio_unitario(self):
        """Lo que ve el cliente: el margen es sobre la venta, como en las propuestas reales (597.462 con 25 % = 796.616)."""
        from decimal import ROUND_HALF_UP

        return self._precio_exacto().quantize(Decimal("0.01"), ROUND_HALF_UP)

    def calculo(self):
        """Valores de la línea: subtotal, descuento, base, IVA, total y lo que ahorra el cliente."""
        from decimal import ROUND_HALF_UP, Decimal
        dos = Decimal("0.01")
        bruto = (self.cantidad * self._precio_exacto()).quantize(dos, ROUND_HALF_UP)  # Sin redondear antes, para no descuadrar por cantidad
        descuento = (bruto * self.descuento / 100).quantize(dos, ROUND_HALF_UP)
        base = bruto - descuento
        tasa = self.iva / 100
        iva = (base * tasa).quantize(dos, ROUND_HALF_UP)
        ahorro = (descuento * (1 + tasa)).quantize(dos, ROUND_HALF_UP)  # lo que el cliente deja de pagar, con IVA
        return {"bruto": bruto, "descuento": descuento, "base": base, "iva": iva, "total": base + iva, "ahorro": ahorro}


class ServicioMonitoreo(models.Model):
    """ARCHIVADO: JANOTEC no presta monitoreo. Se conserva solo para no perder los registros importados; no se usa.

Lo que el cliente paga cada mes por el monitoreo de una sede (contrato de monitoreo).

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
        """Lo que se factura al mes: valor unitario por cantidad."""
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
