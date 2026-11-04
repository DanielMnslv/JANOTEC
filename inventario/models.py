"""Inventario de JANOTEC (antes en el Google Sheets «INVENTARIO»).

Dos clases de elementos:
- Activos: equipos con PLACA única (cámaras, switches, UPS…). Se siguen uno por uno.
- Insumos: material por cantidad con CÓDIGO (cable, conectores, baterías…).

Todo se mueve entre lugares: bodega principal → técnico → cliente (o de vuelta). La bodega entrega al técnico
con la modalidad (venta, alquiler, reinversión…) y el técnico descuenta de lo que tiene en su poder al
diligenciar el acta. Así se sabe en tiempo real qué hay en bodega, qué lleva cada técnico y qué quedó en cada cliente.
"""
import re
import unicodedata
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils.timezone import now as timezone_now


def normalizar(texto):
    """«Hikvisión » → «HIKVISION»: para comparar nombres escritos distinto."""
    texto = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ]", " ", texto)).strip().upper()


class Lugar(models.TextChoices):
    """Dónde está físicamente un activo o material: bodega, con el técnico o en el cliente."""
    BODEGA = "bodega", "Bodega principal"
    TECNICO = "tecnico", "En poder del técnico"
    CLIENTE = "cliente", "Cliente"
    PROVEEDOR = "proveedor", "Proveedor"
    FUERA = "fuera", "Fuera del inventario"


class Modalidad(models.TextChoices):
    """Cómo sale el material de bodega: venta, alquiler, préstamo, reinversión, mantenimiento, uso del personal o stock."""
    VENTA = "venta", "Venta"
    ALQUILER = "alquiler", "Alquiler"
    PRESTAMO = "prestamo", "Préstamo"
    REINVERSION = "reinversion", "Reinversión"
    MANTENIMIENTO = "mantenimiento", "Mantenimiento / soporte"
    ASIGNACION = "asignacion", "Uso del personal (herramienta)"
    STOCK = "stock", "Stock de bodega"


class Marca(models.Model):
    """Catálogo central de marcas: «HIKVISION» y «Hikvisión» son la misma (como marcas de Inventario_Global)."""

    nombre = models.CharField(max_length=120, unique=True)

    class Meta:
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class CategoriaActivo(models.Model):
    """Categorías de los activos (CCTV, ALARMA, ENERGÍA, TI, REDES…)."""

    nombre = models.CharField(max_length=60, unique=True)
    descripcion = models.CharField("descripción", max_length=200, blank=True)

    class Meta:
        ordering = ["nombre"]
        verbose_name = "categoría de activo"
        verbose_name_plural = "categorías de activo"

    def __str__(self):
        return self.nombre


class Proveedor(models.Model):
    """Proveedor al que se le cotiza y compra; puede tener asesor propio y marcas que maneja."""
    nit = models.CharField("NIT", max_length=30, blank=True, db_index=True)
    razon_social = models.CharField("razón social", max_length=200, unique=True)
    correo = models.EmailField(blank=True)
    telefono = models.CharField("teléfono", max_length=40, blank=True)
    direccion = models.CharField("dirección", max_length=255, blank=True)
    notas = models.TextField(blank=True)
    marcas = models.ManyToManyField(Marca, blank=True, related_name="proveedores", verbose_name="marcas que maneja",
                                    help_text="Marque también las que aún no nos ha vendido. Las compradas salen solas en la ficha.")

    class Meta:
        ordering = ["razon_social"]
        verbose_name_plural = "proveedores"

    def __str__(self):
        return self.razon_social

    @property
    def asesor(self):
        """A quién llamar hoy (el primero que sigue activo)."""
        return next((a for a in self.asesores.all() if a.activo), None)


class AsesorProveedor(models.Model):
    """La persona que el proveedor nos asigna. Si la cambian, la anterior queda inactiva y se conserva el historial."""

    proveedor = models.ForeignKey(Proveedor, on_delete=models.CASCADE, related_name="asesores")
    nombre = models.CharField("nombre", max_length=150)
    cargo = models.CharField(max_length=100, blank=True)
    celular = models.CharField(max_length=40, blank=True)
    correo = models.EmailField(blank=True)
    atiende = models.CharField("qué nos atiende", max_length=200, blank=True)
    activo = models.BooleanField("sigue atendiéndonos", default=True)

    class Meta:
        ordering = ["-activo", "nombre"]
        verbose_name = "asesor del proveedor"
        verbose_name_plural = "asesores de proveedores"

    def __str__(self):
        return f"{self.nombre} · {self.proveedor}"


class CodigoActivo(models.Model):
    """Prefijo de placa: CAM-BAL-2MP → las placas CAM-BAL-2MP001, CAM-BAL-2MP002…"""

    codigo = models.CharField("código (prefijo de placa)", max_length=40, unique=True)
    descripcion = models.CharField("descripción", max_length=250)
    marca = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["codigo"]
        verbose_name = "código de activo"
        verbose_name_plural = "códigos de activo"

    def __str__(self):
        return f"{self.codigo} · {self.descripcion}"


class Activo(models.Model):
    """Equipo con placa (cámara, DVR…): su historia completa queda en sus movimientos."""
    class Estado(models.TextChoices):
        """Estado del activo según dónde está y para qué."""
        DISPONIBLE = "disponible", "Disponible en bodega"
        EN_TECNICO = "en_tecnico", "En poder del técnico"
        ASIGNADO = "asignado", "Asignado (uso del personal)"
        ALQUILADO = "alquilado", "Alquilado"
        PRESTADO = "prestado", "Prestado"
        VENDIDO = "vendido", "Vendido"
        MANTENIMIENTO = "mantenimiento", "En mantenimiento"
        GARANTIA = "garantia", "En garantía"
        BAJA = "baja", "Dado de baja"
        NO_ENCONTRADO = "no_encontrado", "No encontrado"

    ESTADOS_EN_JANOTEC = [Estado.DISPONIBLE, Estado.EN_TECNICO, Estado.ASIGNADO, Estado.MANTENIMIENTO, Estado.GARANTIA]

    placa = models.CharField(max_length=40, unique=True)
    serial = models.CharField(max_length=80, blank=True, db_index=True)
    categoria = models.CharField("categoría", max_length=40, blank=True)
    tipo = models.CharField(max_length=80, blank=True)
    subtipo = models.CharField(max_length=80, blank=True)
    marca = models.CharField(max_length=80, blank=True)
    referencia = models.CharField(max_length=80, blank=True)
    descripcion = models.CharField("descripción", max_length=300, blank=True)
    # Compra
    fecha_compra = models.DateField("fecha de compra", null=True, blank=True)
    proveedor = models.CharField(max_length=200, blank=True)
    factura = models.CharField(max_length=60, blank=True)
    valor = models.DecimalField("valor de adquisición sin IVA", max_digits=14, decimal_places=2, null=True, blank=True)
    iva = models.DecimalField("% IVA", max_digits=5, decimal_places=2, default=Decimal("19"))
    responsable_compra = models.CharField("responsable de la compra", max_length=150, blank=True)
    # Dónde está ahora (lo actualiza cada movimiento)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.DISPONIBLE, db_index=True)
    lugar = models.CharField(max_length=20, choices=Lugar.choices, default=Lugar.BODEGA, db_index=True)
    tecnico = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="activos_en_poder", verbose_name="responsable actual")
    tecnico_nombre = models.CharField("responsable actual", max_length=150, blank=True)
    empresa = models.ForeignKey("accounts.Empresa", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="activos_instalados")
    cliente_texto = models.CharField("cliente", max_length=200, blank=True)
    sede = models.ForeignKey("accounts.Sede", on_delete=models.SET_NULL, null=True, blank=True, related_name="activos")
    modalidad = models.CharField(max_length=20, choices=Modalidad.choices, blank=True)
    fecha_devolucion = models.DateField("fecha prevista de devolución", null=True, blank=True,
                                        help_text="Préstamos y alquileres: cuándo debe volver a bodega.")
    # La bodega se lo entregó al técnico para este ticket (se sugiere en el acta)
    ticket_destino = models.ForeignKey("tickets.Ticket", on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="activos_reservados")
    fecha_ultimo_movimiento = models.DateField(null=True, blank=True)
    observaciones = models.CharField(max_length=500, blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["placa"]

    def __str__(self):
        return f"{self.placa} · {self.descripcion_corta}"

    def get_absolute_url(self):
        """Ficha del activo por su placa."""
        return reverse("inventario:activo", args=[self.placa])

    @property
    def descripcion_corta(self):
        """Tipo, subtipo, marca y referencia; si no hay, el comienzo de la descripción."""
        partes = [self.tipo, self.subtipo, self.marca, self.referencia]
        return " ".join(p for p in partes if p) or self.descripcion[:60]

    @property
    def valor_con_iva(self):
        """Valor de compra con IVA, o None si no se registró."""
        return (self.valor * (1 + self.iva / 100)).quantize(Decimal("0.01")) if self.valor is not None else None

    @property
    def ubicacion(self):
        """Texto de dónde está: cliente (y sede), «Con …» el técnico, o bodega."""
        if self.lugar == Lugar.CLIENTE:
            return str(self.empresa or self.cliente_texto) + (f" · {self.sede.nombre}" if self.sede else "")
        if self.lugar == Lugar.TECNICO:
            return f"Con {self.tecnico or self.tecnico_nombre}"
        return self.get_lugar_display()

    @property
    def color_estado(self):
        """Color de la etiqueta del estado (clases de Bootstrap)."""
        return {"disponible": "success", "en_tecnico": "warning", "asignado": "info", "alquilado": "primary",
                "prestado": "primary", "vendido": "secondary", "baja": "dark", "no_encontrado": "danger"}.get(self.estado, "light")

    @property
    def dias_para_devolver(self):
        """Días que faltan (negativo = vencido) para que vuelva un equipo prestado o alquilado."""
        from django.utils import timezone
        if not self.fecha_devolucion or self.estado not in (self.Estado.ALQUILADO, self.Estado.PRESTADO):
            return None
        return (self.fecha_devolucion - timezone.localdate()).days

    @property
    def vencido(self):
        """True si es un préstamo o alquiler cuya fecha de devolución ya pasó."""
        dias = self.dias_para_devolver
        return dias is not None and dias < 0

    @staticmethod
    def prefijo(placa):
        """Parte de la placa sin el consecutivo final: CAM-BAL-2MP010 → CAM-BAL-2MP."""
        m = re.match(r"^(.*?)(\d+)$", placa or "")
        return m.group(1) if m else placa

    @classmethod
    def siguiente_placa(cls, prefijo):
        """CAM-BAL-2MP → CAM-BAL-2MP010 si la última es la 009 (consecutivo de 3 dígitos, como en el Sheets)."""
        prefijo = (prefijo or "").strip().upper()
        mayor = 0
        for placa in cls.objects.filter(placa__startswith=prefijo).values_list("placa", flat=True):
            resto = placa[len(prefijo):]
            if resto.isdigit():
                mayor = max(mayor, int(resto))
        return f"{prefijo}{mayor + 1:03d}"


class Insumo(models.Model):
    """Material por cantidad (como INVENTARIO_INSUMO). El stock sale de los movimientos."""

    class Unidad(models.TextChoices):
        """Unidad en que se cuenta el insumo."""
        UNIDAD = "Unidad", "Unidad"
        METRO = "Metro", "Metro"

    codigo = models.CharField("código", max_length=40, unique=True)
    descripcion = models.CharField("descripción", max_length=250)
    unidad = models.CharField("unidad de medida", max_length=20, choices=Unidad.choices, default=Unidad.UNIDAD)
    marca = models.CharField(max_length=80, blank=True)
    stock_minimo = models.DecimalField("stock mínimo (alerta)", max_digits=12, decimal_places=2, default=1,
                                       help_text="Se avisa para reponer cuando en bodega queda menos de esto.")
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["codigo"]

    def __str__(self):
        return f"{self.codigo} · {self.descripcion}"

    def get_absolute_url(self):
        """Ficha del insumo por su código."""
        return reverse("inventario:insumo", args=[self.codigo])


class MovimientoBase(models.Model):
    """Lo común a los movimientos de activos e insumos: de dónde, a dónde, quién, para qué cliente y por qué."""

    class Tipo(models.TextChoices):
        """Tipos de movimiento del inventario (compra, entrega, instalación, salida, devolución, traslado, garantía, baja, ajuste…)."""
        COMPRA = "compra", "Compra (ingreso a bodega)"
        ENTREGA = "entrega", "Entrega de bodega al técnico"
        INSTALACION = "instalacion", "Instalado / usado en el cliente (acta)"
        SALIDA = "salida", "Salida de bodega al cliente"
        DEVOLUCION = "devolucion", "Devolución a bodega"
        TRASLADO = "traslado", "Traslado"
        MANTENIMIENTO = "mantenimiento", "Mantenimiento / reparación"
        GARANTIA = "garantia", "Garantía"
        BAJA = "baja", "Baja"
        AJUSTE = "ajuste", "Ajuste de inventario"

    fecha = models.DateField(db_index=True)
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    origen = models.CharField(max_length=20, choices=Lugar.choices)
    destino = models.CharField(max_length=20, choices=Lugar.choices)
    # El técnico que lo recibe, lo tiene o lo instaló
    tecnico = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="+", verbose_name="técnico / responsable")
    tecnico_nombre = models.CharField("técnico / responsable", max_length=150, blank=True)
    tecnico_documento = models.CharField(max_length=20, blank=True, db_index=True)
    # Cliente (destino de la venta, alquiler o servicio)
    empresa = models.ForeignKey("accounts.Empresa", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cliente_texto = models.CharField("cliente", max_length=200, blank=True)
    sede = models.ForeignKey("accounts.Sede", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    modalidad = models.CharField(max_length=20, choices=Modalidad.choices, blank=True)
    fecha_devolucion = models.DateField("fecha prevista de devolución", null=True, blank=True)
    ticket = models.ForeignKey("tickets.Ticket", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    acta = models.ForeignKey("tickets.ActaSoporte", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # La visita del cronograma para la que bodega preparó la entrega
    actividad = models.ForeignKey("cronograma.Actividad", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    solicitud = models.ForeignKey("inventario.SolicitudMaterial", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+")
    proveedor = models.CharField(max_length=200, blank=True)
    factura = models.CharField(max_length=60, blank=True)
    observaciones = models.CharField(max_length=500, blank=True)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="+")
    registrado_por_nombre = models.CharField("registrado por", max_length=150, blank=True)
    importado_de = models.CharField("origen de la importación", max_length=80, blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["-fecha", "-creado"]

    @property
    def cliente(self):
        """Nombre del cliente del movimiento (empresa registrada o texto libre)."""
        return str(self.empresa) if self.empresa else self.cliente_texto

    @property
    def responsable(self):
        """Técnico responsable (usuario registrado o nombre escrito)."""
        return str(self.tecnico) if self.tecnico else self.tecnico_nombre


class MovimientoActivo(MovimientoBase):
    """Movimiento de un activo con placa y el estado en que queda."""
    activo = models.ForeignKey(Activo, on_delete=models.CASCADE, related_name="movimientos")
    estado = models.CharField("estado resultante", max_length=20, choices=Activo.Estado.choices)

    class Meta(MovimientoBase.Meta):
        verbose_name = "movimiento de activo"
        verbose_name_plural = "movimientos de activos"

    def __str__(self):
        return f"{self.fecha:%d/%m/%Y} {self.activo.placa} → {self.get_estado_display()}"


class MovimientoInsumo(MovimientoBase):
    """Movimiento de un insumo por cantidad (entra o sale) y su valor unitario."""
    insumo = models.ForeignKey(Insumo, on_delete=models.PROTECT, related_name="movimientos")
    cantidad = models.DecimalField(max_digits=12, decimal_places=2)
    valor_unitario = models.DecimalField("valor unitario sin IVA", max_digits=14, decimal_places=4, null=True, blank=True)
    iva = models.DecimalField("% IVA", max_digits=5, decimal_places=2, null=True, blank=True)

    class Meta(MovimientoBase.Meta):
        verbose_name = "movimiento de insumo"
        verbose_name_plural = "movimientos de insumos"

    def __str__(self):
        return f"{self.fecha:%d/%m/%Y} {self.insumo.codigo} × {self.cantidad} ({self.get_tipo_display()})"

    @property
    def costo_unitario(self):
        """Con IVA, como «VALOR TOTAL UNITARIO» del Sheets."""
        if self.valor_unitario is None:
            return None
        return self.valor_unitario * (1 + (self.iva or Decimal("0")) / 100)


class SolicitudMaterial(models.Model):
    """Pedido de material a bodega, con validación de existencias, compra, propuesta al cliente y firmas.

    Firman bodega (al validar) y una autorización: el coordinador TIC o dirección, el que esté (el jefe no
    siempre está y no se debe frenar la salida por eso). Si falta material, bodega cotiza y compra directamente.
    Si el material corre por cuenta del cliente (p. ej. una cámara dañada que no cubre JANOTEC), primero va al
    área comercial para la propuesta y solo con la propuesta aprobada sigue el proceso.
    Bodega solo entrega material para clientes con la solicitud aprobada (las herramientas del personal no).
    """

    class Origen(models.TextChoices):
        """Quién pide el material: comercial (propuesta aprobada) o técnica (visita)."""
        COMERCIAL = "comercial", "Área comercial (propuesta aprobada)"
        TECNICA = "tecnica", "Área técnica (visita programada)"

    class Estado(models.TextChoices):
        """Pasos de la solicitud de material, de borrador a entregada."""
        BORRADOR = "borrador", "Por completar"
        VALIDACION = "validacion", "Validación de bodega"
        PROPUESTA = "propuesta", "Esperando aprobación del cliente (propuesta)"
        COMPRA = "compra", "Solicitud de compra"
        APROBACION = "aprobacion", "Esperando firmas de aprobación"
        APROBADA = "aprobada", "Aprobada · por entregar"
        ENTREGADA = "entregada", "Entregada al técnico"
        CANCELADA = "cancelada", "Cancelada"

    class Cargo(models.TextChoices):
        """Quién paga el material: JANOTEC (garantía, contrato) o el cliente (con propuesta aprobada)."""
        POR_DEFINIR = "", "Por definir"
        JANOTEC = "janotec", "A cargo de JANOTEC (garantía, contrato, mantenimiento)"
        CLIENTE = "cliente", "A cargo del cliente (requiere propuesta aprobada)"

    ESTADOS_ABIERTOS = [Estado.BORRADOR, Estado.VALIDACION, Estado.PROPUESTA, Estado.COMPRA, Estado.APROBACION,
                        Estado.APROBADA]

    ticket = models.ForeignKey("tickets.Ticket", on_delete=models.CASCADE, related_name="solicitudes_material")
    actividad = models.ForeignKey("cronograma.Actividad", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="solicitudes_material")
    origen = models.CharField(max_length=20, choices=Origen.choices)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.BORRADOR, db_index=True)
    cargo = models.CharField("¿a cargo de quién?", max_length=20, choices=Cargo.choices, blank=True)
    cliente_aprobo = models.BooleanField("el cliente aprobó la propuesta", default=False)
    modalidad = models.CharField(max_length=20, choices=Modalidad.choices, blank=True)
    fecha_devolucion = models.DateField("fecha prevista de devolución", null=True, blank=True,
                                        help_text="Préstamo o alquiler: cuándo deben volver los equipos.")
    tecnico = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="solicitudes_material", verbose_name="técnico que recibe")
    nota = models.TextField("para qué es / observaciones", blank=True)
    motivo_devolucion = models.CharField("por qué se devolvió", max_length=500, blank=True)
    ronda = models.PositiveIntegerField("ronda de firmas", default=1)
    solicitado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "solicitud de material"
        verbose_name_plural = "solicitudes de material"

    def __str__(self):
        return f"{self.numero} · {self.ticket.radicado}"

    def get_absolute_url(self):
        """Ficha de la solicitud de material."""
        return reverse("inventario:solicitud", args=[self.pk])

    @property
    def numero(self):
        """Consecutivo visible: SM-00012."""
        return f"SM-{self.pk:05d}" if self.pk else "SM-nueva"

    @property
    def roles_requeridos(self):
        """Quién debe firmar: bodega y la autorización (casilla «dirección», que también firma el coordinador TIC)."""
        return [FirmaSolicitud.Rol.BODEGA, FirmaSolicitud.Rol.DIRECCION]

    @property
    def firmas_ronda(self):
        """Firmas de aprobación de la ronda actual (si se devuelve, empieza otra)."""
        return [f for f in self.firmas.all() if f.ronda == self.ronda]

    @property
    def color_estado(self):
        """Color de la etiqueta del estado (clases de Bootstrap)."""
        return {"borrador": "secondary", "validacion": "info", "propuesta": "warning", "compra": "danger",
                "aprobacion": "warning", "aprobada": "success", "entregada": "primary", "cancelada": "dark"}[self.estado]


class ItemSolicitud(models.Model):
    """Renglón de una solicitud de material: del catálogo (insumo/activo) o escrito a mano."""
    class Seccion(models.TextChoices):
        """Las dos secciones de la lista: lo aprobado por el cliente y los adicionales para instalar."""
        APROBADO = "aprobado", "Aprobado por el cliente"
        ADICIONAL = "adicional", "Adicionales para instalar"

    solicitud = models.ForeignKey(SolicitudMaterial, on_delete=models.CASCADE, related_name="items")
    seccion = models.CharField("sección de la lista", max_length=10, choices=Seccion.choices, default=Seccion.APROBADO,
                               help_text="Lo que aprobó el cliente, o lo adicional que se necesita para poder instalarlo.")
    insumo = models.ForeignKey(Insumo, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    referencia = models.CharField("referencia del equipo", max_length=80, blank=True,
                                  help_text="Para activos: se entrega cualquier placa disponible de esta referencia.")
    descripcion = models.CharField(max_length=300)
    cantidad = models.DecimalField(max_digits=12, decimal_places=2)
    adicional = models.DecimalField("adicional (holgura de consumibles)", max_digits=12, decimal_places=2, default=0,
                                    help_text="Bodega suele dar un poco más de consumibles; el sobrante se devuelve.")
    a_mano_listo = models.BooleanField(
        "bodega lo tiene", default=False,
        help_text="Material escrito a mano (no está en el inventario): bodega confirma que lo tiene o que ya lo compró.")

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.cantidad:g} {self.descripcion}"

    @property
    def es_activo(self):
        """True si es un equipo con placa (tiene referencia y no es insumo)."""
        return bool(self.referencia) and self.insumo_id is None

    @property
    def total(self):
        """Cantidad más adicional."""
        return self.cantidad + (self.adicional or 0)


class SolicitudCotizacion(models.Model):
    """Lo que se le pregunta a uno o varios proveedores: cuánto cuesta y cuándo llega. No compromete a JANOTEC.

    Cada proveedor responde con su cotización; la que se elija se convierte en la orden de compra, que es el compromiso.
    """

    class Estado(models.TextChoices):
        """Estado de la solicitud de cotización (SC) a proveedores."""
        BORRADOR = "borrador", "En preparación"
        ENVIADA = "enviada", "Esperando cotizaciones"
        CERRADA = "cerrada", "Orden de compra generada"
        CANCELADA = "cancelada", "Cancelada"

    solicitud = models.ForeignKey(SolicitudMaterial, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="cotizaciones", help_text="Para qué solicitud de material (ticket) es.")
    # Antes de que el cliente apruebe, bodega ya puede cotizar los equipos y materiales de la propuesta
    propuesta = models.ForeignKey("comercial.FichaComercial", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="solicitudes_cotizacion",
                                  help_text="La propuesta (aún sin aprobar) de la que salen los elementos.")
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.BORRADOR, db_index=True)
    fecha_limite = models.DateField("responder antes del", null=True, blank=True)
    notas = models.TextField("observaciones para el proveedor", blank=True)
    creado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "solicitud de cotización"
        verbose_name_plural = "solicitudes de cotización"

    def __str__(self):
        return self.numero

    def get_absolute_url(self):
        """Ficha de la solicitud de cotización."""
        return reverse("inventario:cotizacion", args=[self.pk])

    @property
    def numero(self):
        """Consecutivo visible: SC-00012."""
        return f"SC-{self.pk:05d}" if self.pk else "SC-nueva"

    @property
    def color_estado(self):
        """Color de la etiqueta del estado (clases de Bootstrap)."""
        return {"borrador": "secondary", "enviada": "warning", "cerrada": "success", "cancelada": "dark"}[self.estado]

    def lineas_para(self, proveedor):
        """Lo que se le pregunta a un proveedor: solo las líneas en las que se eligió a ese proveedor.

        Las solicitudes anteriores al proveedor por línea (ninguna línea lo tiene) se le preguntaban completas a todos.
        """
        lineas = list(self.lineas.all())
        if not any(l.proveedor_id for l in lineas):
            return lineas
        return [l for l in lineas if l.proveedor_id == proveedor.pk]

    def por_ordenar(self):
        """Las líneas que aún no están en una orden de compra vigente."""
        ordenadas = LineaOrdenCompra.objects.filter(linea_cotizacion__solicitud=self).exclude(
            orden__estado=OrdenCompra.Estado.CANCELADA).values_list("linea_cotizacion_id", flat=True)
        return [l for l in self.lineas.all() if l.pk not in set(ordenadas)]


class LineaSolicitudCotizacion(models.Model):
    """Lo que se pregunta: lo que pide el ticket más lo adicional que bodega quiere tener en inventario."""

    class Tipo(models.TextChoices):
        """Si el renglón es un equipo o un material."""
        EQUIPO = "equipo", "Equipo"
        MATERIAL = "material", "Material"

    solicitud = models.ForeignKey(SolicitudCotizacion, on_delete=models.CASCADE, related_name="lineas")
    tipo = models.CharField(max_length=10, choices=Tipo.choices, default=Tipo.MATERIAL)
    # El proveedor que se sabe que maneja el elemento y su último precio (referencia interna, no sale en el PDF)
    proveedor = models.ForeignKey(Proveedor, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                  help_text="A quién se le pregunta por este elemento; vacío = a todos los proveedores.")
    precio_referencia = models.DecimalField("último precio conocido", max_digits=14, decimal_places=2, null=True, blank=True)
    item = models.ForeignKey("inventario.ItemSolicitud", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                             help_text="El ítem de la solicitud de material (ticket) del que sale la línea.")
    descripcion = models.CharField("descripción", max_length=300)
    cantidad = models.DecimalField("para el ticket", max_digits=12, decimal_places=2, default=0)
    adicional = models.DecimalField("adicional para inventario", max_digits=12, decimal_places=2, default=0,
                                    help_text="Lo que se cotiza de más para tener en bodega.")

    class Meta:
        ordering = ["id"]

    @property
    def total(self):
        """Cantidad más adicional."""
        return self.cantidad + self.adicional


class CotizacionProveedor(models.Model):
    """A quién se le preguntó y qué respondió: mientras no tenga `recibida_en`, seguimos esperando su cotización."""

    solicitud = models.ForeignKey(SolicitudCotizacion, on_delete=models.CASCADE, related_name="cotizaciones")
    proveedor = models.ForeignKey(Proveedor, on_delete=models.PROTECT, related_name="cotizaciones")
    referencia = models.CharField("n.º de cotización del proveedor", max_length=60, blank=True)
    fecha = models.DateField("fecha de la cotización", null=True, blank=True)
    fecha_entrega = models.DateField("fecha estimada de entrega", null=True, blank=True)
    condiciones = models.TextField("condiciones", blank=True, help_text="Forma de pago, validez de la oferta, garantía…")
    archivo = models.FileField("cotización (PDF)", upload_to="cotizaciones/%Y/%m/", blank=True)
    recibida_en = models.DateTimeField(null=True, blank=True)
    recibida_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["id"]
        verbose_name = "cotización del proveedor"
        verbose_name_plural = "cotizaciones de proveedores"
        constraints = [models.UniqueConstraint(fields=["solicitud", "proveedor"], name="una_cotizacion_por_proveedor")]

    def __str__(self):
        return f"{self.solicitud.numero} · {self.proveedor}"

    def get_absolute_url(self):
        """Pantalla para registrar la respuesta de este proveedor."""
        return reverse("inventario:cotizacion_respuesta", args=[self.solicitud_id, self.pk])


class PrecioCotizado(models.Model):
    """El precio que dio un proveedor por una línea. Si no cotizó esa línea, no hay fila."""

    cotizacion = models.ForeignKey(CotizacionProveedor, on_delete=models.CASCADE, related_name="precios")
    linea = models.ForeignKey(LineaSolicitudCotizacion, on_delete=models.CASCADE, related_name="precios")
    valor_unitario = models.DecimalField("valor unitario (antes de IVA)", max_digits=14, decimal_places=2)
    iva = models.DecimalField("IVA %", max_digits=5, decimal_places=2, default=19)

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["cotizacion", "linea"], name="un_precio_por_linea")]

    @property
    def subtotal(self):
        """Total del renglón según el precio del proveedor (sin IVA)."""
        return self.linea.total * self.valor_unitario


class OrdenCompra(models.Model):
    """Lo que ya se decidió comprar: proveedor, costo, fecha estimada de llegada y soporte.

    El camino de una orden:
    1. Bodega la arma (en preparación) y la envía a dirección; dirección la aprueba o la devuelve con observaciones.
       Si es urgente y dirección no puede, bodega la pasa con un motivo y dirección recibe el aviso.
    2. Pago anticipado: financiera la paga y sube el soporte (por pagar → lista). A crédito: queda lista de una vez y
       financiera la paga después, contra la factura, al vencer el plazo.
    3. Bodega se la envía al proveedor (con el soporte de pago si lo hay) y recibe lo que llega.
    4. La factura llega a financiera, que la registra y la compara con la orden. Cerrada = recibida, pagada y facturada.
    """

    class Estado(models.TextChoices):
        """Camino de la orden de compra:

            borrador → revisión de dirección → por pagar (financiera, si es anticipado) → lista →
            enviada al proveedor → recibida en bodega. Con crédito se salta «por pagar».
        """
        BORRADOR = "borrador", "En preparación"
        REVISION = "revision", "En revisión de dirección"
        POR_PAGAR = "por_pagar", "Por pagar (financiera)"
        LISTA = "lista", "Lista para enviar al proveedor"
        ENVIADA = "enviada", "Enviada al proveedor"
        RECIBIDA = "recibida", "Recibida en bodega"
        CANCELADA = "cancelada", "Cancelada"

    class FormaPago(models.TextChoices):
        """Anticipado: financiera paga antes del despacho. Crédito: se paga contra la factura."""
        ANTICIPADO = "anticipado", "Pago anticipado (antes del despacho)"
        CREDITO = "credito", "Crédito (se paga contra factura)"

    proveedor = models.ForeignKey(Proveedor, on_delete=models.SET_NULL, null=True, blank=True, related_name="ordenes")
    solicitud = models.ForeignKey(SolicitudMaterial, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="ordenes_compra", help_text="Para qué solicitud de material (ticket) es.")
    cotizacion = models.ForeignKey(CotizacionProveedor, on_delete=models.SET_NULL, null=True, blank=True, related_name="ordenes",
                                   help_text="La cotización del proveedor de la que salió esta orden.")
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.BORRADOR, db_index=True)
    fecha_estimada = models.DateField("fecha estimada de llegada", null=True, blank=True)
    notas = models.TextField("condiciones y notas", blank=True)
    soporte = models.FileField("cotización o factura (PDF)", upload_to="compras/%Y/%m/", blank=True)
    # Al recibir la factura del proveedor: todo entra a bodega de una vez
    factura = models.CharField("n.º de factura", max_length=60, blank=True)
    fecha_factura = models.DateField("fecha de la factura", null=True, blank=True)
    recibida_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    recibida_en = models.DateTimeField(null=True, blank=True)
    forma_pago = models.CharField("forma de pago", max_length=12, choices=FormaPago.choices, default=FormaPago.ANTICIPADO)
    dias_credito = models.PositiveSmallIntegerField("días de crédito", null=True, blank=True,
                                                    help_text="Solo a crédito: cuántos días después de la factura se paga.")
    # Aprobación de dirección (o paso urgente con motivo)
    aprobada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+", verbose_name="aprobada por")
    aprobada_en = models.DateTimeField(null=True, blank=True)
    observaciones_direccion = models.TextField("observaciones de dirección", blank=True)
    motivo_urgencia = models.CharField("motivo del paso urgente", max_length=500, blank=True)
    # Pago (financiera)
    pagada_en = models.DateField("fecha del pago", null=True, blank=True)
    valor_pagado = models.DecimalField("valor pagado", max_digits=14, decimal_places=2, null=True, blank=True)
    medio_pago = models.CharField("medio de pago", max_length=80, blank=True, help_text="Transferencia, consignación…")
    soporte_pago = models.FileField("soporte de pago", upload_to="compras/pagos/%Y/%m/", blank=True)
    pago_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    enviada_proveedor_en = models.DateTimeField("enviada al proveedor", null=True, blank=True)
    # Factura del proveedor: llega a financiera, que la compara con la orden
    factura_valor = models.DecimalField("valor total de la factura (con IVA)", max_digits=14, decimal_places=2, null=True, blank=True)
    factura_pdf = models.FileField("factura (PDF)", upload_to="compras/facturas/%Y/%m/", blank=True)
    factura_nota = models.CharField("nota de la factura", max_length=300, blank=True)
    factura_validada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                             related_name="+")
    factura_validada_en = models.DateTimeField(null=True, blank=True)
    creado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "orden de compra"
        verbose_name_plural = "órdenes de compra"

    def __str__(self):
        return self.numero

    def get_absolute_url(self):
        """Ficha de la orden de compra."""
        return reverse("inventario:orden", args=[self.pk])

    @property
    def numero(self):
        """Consecutivo visible: OC-00012."""
        return f"OC-{self.pk:05d}" if self.pk else "OC-nueva"

    @property
    def color_estado(self):
        """Color de la etiqueta del estado (clases de Bootstrap)."""
        return {"borrador": "secondary", "revision": "info", "por_pagar": "danger", "lista": "primary", "enviada": "warning",
                "recibida": "success", "cancelada": "dark"}[self.estado]

    # --- Pago y factura ---------------------------------------------------------------------
    @property
    def a_credito(self):
        """True si la orden se paga a crédito, contra factura."""
        return self.forma_pago == self.FormaPago.CREDITO

    @property
    def pagada(self):
        """True si financiera ya registró el pago."""
        return bool(self.pagada_en)

    @property
    def vence(self):
        """A crédito: fecha en que hay que pagarla (factura + días de crédito)."""
        from datetime import timedelta
        if self.a_credito and self.fecha_factura:
            return self.fecha_factura + timedelta(days=self.dias_credito or 0)
        return None

    @property
    def factura_registrada(self):
        """True si financiera ya registró la factura del proveedor."""
        return self.factura_valor is not None

    @property
    def diferencia_factura(self):
        """Factura menos orden (con IVA). Positiva: facturaron de más."""
        if self.factura_valor is None:
            return None
        return self.factura_valor - self.totales()["total"]

    @property
    def factura_cuadra(self):
        """La factura coincide con la orden (con un margen de $1.000 por redondeos)."""
        d = self.diferencia_factura
        return d is not None and abs(d) <= 1000

    @property
    def cerrada(self):
        """Compra terminada: recibida en bodega, pagada y con la factura validada por financiera."""
        return self.estado == self.Estado.RECIBIDA and self.pagada and bool(self.factura_validada_en)

    @property
    def atrasada(self):
        """True si se espera la mercancía y la fecha estimada de llegada ya pasó."""
        from django.utils import timezone
        return (self.estado in (self.Estado.LISTA, self.Estado.ENVIADA) and self.fecha_estimada
                and self.fecha_estimada < timezone.localdate())

    def totales(self):
        """Base, IVA y total de la orden: {'base', 'iva', 'total'}."""
        from decimal import Decimal
        base = sum((l.subtotal for l in self.lineas.all()), Decimal(0))
        iva = sum((l.subtotal * l.iva / 100 for l in self.lineas.all()), Decimal(0))
        return {"base": base, "iva": iva, "total": base + iva}

    @property
    def por_recibir(self):
        """Las líneas a las que aún les falta llegar algo."""
        return [l for l in self.lineas.all() if l.pendiente > 0]


class LineaOrdenCompra(models.Model):
    """Lo que se compra: lo que pide el ticket más lo adicional que bodega quiere tener en inventario."""

    orden = models.ForeignKey(OrdenCompra, on_delete=models.CASCADE, related_name="lineas")
    item = models.ForeignKey("inventario.ItemSolicitud", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                             help_text="El ítem de la solicitud de material (ticket) del que sale la línea.")
    linea_cotizacion = models.ForeignKey(LineaSolicitudCotizacion, on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="+", help_text="La línea de la solicitud de cotización de la que sale.")
    descripcion = models.CharField("descripción", max_length=300)
    cantidad = models.DecimalField("para el ticket", max_digits=12, decimal_places=2, default=0)
    adicional = models.DecimalField("adicional para inventario", max_digits=12, decimal_places=2, default=0,
                                    help_text="Lo que se compra de más para tener en bodega.")
    valor_unitario = models.DecimalField("valor unitario (antes de IVA)", max_digits=14, decimal_places=2, default=0)
    iva = models.DecimalField("IVA %", max_digits=5, decimal_places=2, default=19)
    recibido = models.DecimalField("ya recibido", max_digits=12, decimal_places=2, default=0)

    class Meta:
        ordering = ["id"]

    @property
    def total(self):
        """Cantidad más adicional que se compra."""
        return self.cantidad + self.adicional

    @property
    def pendiente(self):
        """Lo que falta por recibir en bodega (nunca negativo)."""
        return max(self.total - self.recibido, Decimal(0))

    @property
    def subtotal(self):
        """Total del renglón sin IVA."""
        return self.total * self.valor_unitario


class MinimoModelo(models.Model):
    """Stock mínimo de un modelo de equipo (p. ej. tener siempre 2 cámaras DS-2CD1027G2): como el de los insumos."""

    referencia = models.CharField("modelo / referencia", max_length=80, unique=True)
    minimo = models.PositiveIntegerField("stock mínimo", default=1)

    class Meta:
        verbose_name = "stock mínimo de un modelo"
        verbose_name_plural = "stock mínimo de los modelos"

    def __str__(self):
        return f"{self.referencia}: {self.minimo}"


class GarantiaProveedor(models.Model):
    """Un equipo que se manda al proveedor por garantía o devolución, y cómo se resolvió."""

    class Estado(models.TextChoices):
        """Cómo va la garantía con el proveedor."""
        EN_PROVEEDOR = "en_proveedor", "En el proveedor"
        REPARADO = "reparado", "Volvió reparado"
        REEMPLAZADO = "reemplazado", "Lo cambiaron por otro"
        NOTA_CREDITO = "nota_credito", "Devolvieron el dinero (nota crédito)"
        RECHAZADA = "rechazada", "No aplicó la garantía"

    activo = models.ForeignKey(Activo, on_delete=models.PROTECT, related_name="garantias")
    proveedor = models.ForeignKey(Proveedor, on_delete=models.SET_NULL, null=True, blank=True, related_name="garantias")
    factura = models.CharField("factura de compra", max_length=60, blank=True)
    motivo = models.TextField("qué falla")
    enviada = models.DateField("enviado al proveedor")
    radicado = models.CharField("radicado o guía del proveedor", max_length=80, blank=True)
    ticket = models.ForeignKey("tickets.Ticket", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                               help_text="El ticket donde se encontró la falla, si lo hay.")
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.EN_PROVEEDOR, db_index=True)
    resuelta = models.DateField(null=True, blank=True)
    resultado = models.CharField("cómo se resolvió", max_length=300, blank=True)
    serial_anterior = models.CharField(max_length=80, blank=True, help_text="Si lo cambiaron, el serial del que se fue.")
    valor_nota = models.DecimalField("valor de la nota crédito", max_digits=14, decimal_places=2, null=True, blank=True)
    creado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-enviada", "-pk"]
        verbose_name = "garantía con proveedor"
        verbose_name_plural = "garantías con proveedores"

    def __str__(self):
        return f"{self.numero} · {self.activo.placa}"

    @property
    def numero(self):
        """Consecutivo visible: GAR-0012."""
        return f"GAR-{self.pk:04d}" if self.pk else "GAR-nueva"

    @property
    def abierta(self):
        """True mientras el equipo sigue en el proveedor."""
        return self.estado == self.Estado.EN_PROVEEDOR

    @property
    def dias(self):
        """Días que lleva (o llevó) el equipo en garantía."""
        from django.utils import timezone
        return ((self.resuelta or timezone.localdate()) - self.enviada).days

    @property
    def color_estado(self):
        """Color de la etiqueta del estado (clases de Bootstrap)."""
        return {"en_proveedor": "warning", "reparado": "success", "reemplazado": "success", "nota_credito": "info",
                "rechazada": "danger"}[self.estado]


class AvisoSemanal(models.Model):
    """Constancia de los avisos que se envían una vez por semana (p. ej. insumos por reponer)."""

    clave = models.CharField(max_length=40, unique=True)
    cantidad = models.PositiveIntegerField(default=0)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "aviso semanal"
        verbose_name_plural = "avisos semanales"

    def __str__(self):
        return self.clave


def ruta_firma(instancia, nombre):
    """Ruta de la imagen de una firma: por año y mes, con nombre aleatorio."""
    import uuid

    from django.utils import timezone
    return f"firmas/{timezone.localdate():%Y/%m}/{uuid.uuid4().hex}.png"


class ActaEntrega(models.Model):
    """Acta de entrega de materiales y equipos: lo que bodega entregó al técnico (solo lo disponible).
    Como el acta de soporte, la firman a mano quien entrega (bodega) y quien recibe (el técnico); queda en PDF en el ticket."""

    solicitud = models.ForeignKey(SolicitudMaterial, on_delete=models.CASCADE, related_name="actas_entrega")
    tecnico = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+",
                                verbose_name="técnico que recibe")
    entregado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    detalle = models.JSONField(default=list, help_text="[{seccion, cantidad, descripcion, codigo}] de lo entregado.")
    adjunto = models.ForeignKey("tickets.Adjunto", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    recibido_en = models.DateTimeField(null=True, blank=True)
    recibido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    firma_entrega = models.FileField("firma de quien entrega", upload_to=ruta_firma, blank=True)
    firma_recibe = models.FileField("firma de quien recibe", upload_to=ruta_firma, blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado"]
        verbose_name = "acta de entrega"
        verbose_name_plural = "actas de entrega"

    def __str__(self):
        return self.numero

    @property
    def numero(self):
        """Consecutivo visible: AE-00012."""
        return f"AE-{self.pk:05d}" if self.pk else "AE-nueva"


class FirmaSolicitud(models.Model):
    """Firma de aprobación (o rechazo) de una solicitud: quién, en qué rol, cuándo y con qué comentario."""

    class Rol(models.TextChoices):
        """Quién firma la aprobación del material: bodega, coordinador o dirección."""
        BODEGA = "bodega", "Encargado del inventario"
        COORDINADOR = "coordinador", "Coordinador técnico"
        DIRECCION = "direccion", "Dirección (jefe)"

    solicitud = models.ForeignKey(SolicitudMaterial, on_delete=models.CASCADE, related_name="firmas")
    ronda = models.PositiveIntegerField(default=1)
    rol = models.CharField(max_length=20, choices=Rol.choices)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    aprobada = models.BooleanField()
    comentario = models.CharField(max_length=500, blank=True)
    imagen = models.FileField("firma", upload_to=ruta_firma, blank=True, help_text="La firma a mano de quien aprueba.")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado"]
        unique_together = [("solicitud", "ronda", "rol")]


# --- Herramientas de trabajo por técnico (unificado de Inventario_Global) ---------------------
class Herramienta(models.Model):
    """Herramienta o equipo de trabajo asignado a un técnico (sin placa): sale y vuelve sin firmas."""

    class Estado(models.TextChoices):
        """Estado de la herramienta."""
        DISPONIBLE = "disponible", "Disponible"
        EN_USO = "en_uso", "En uso"
        EN_REPARACION = "en_reparacion", "En reparación"
        DANADA = "danada", "Dañada"
        BAJA = "baja", "Baja"

    nombre = models.CharField(max_length=180)
    descripcion = models.CharField("descripción", max_length=255, blank=True)
    marca = models.CharField(max_length=120, blank=True)
    serial = models.CharField(max_length=120, blank=True)
    cantidad = models.PositiveIntegerField(default=1)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.DISPONIBLE, db_index=True)
    tecnico = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="herramientas", verbose_name="responsable")
    tecnico_nombre = models.CharField("responsable (sin usuario)", max_length=180, blank=True,
                                      help_text="Del inventario anterior: se asocia al crear el usuario del técnico.")
    costo_compra = models.DecimalField("precio de compra", max_digits=14, decimal_places=2, default=0)
    importado_de = models.CharField(max_length=80, blank=True)
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["nombre", "pk"]

    def __str__(self):
        return f"{self.nombre}{' · ' + self.serial if self.serial else ''}"

    def get_absolute_url(self):
        """Ficha de la herramienta."""
        return reverse("inventario:herramienta", args=[self.pk])

    @property
    def responsable(self):
        """Técnico responsable (usuario registrado o nombre escrito)."""
        return str(self.tecnico) if self.tecnico else self.tecnico_nombre

    @property
    def valor(self):
        """Valor total: precio de compra por cantidad."""
        return (self.costo_compra or 0) * self.cantidad

    @property
    def color_estado(self):
        """Color de la etiqueta del estado (clases de Bootstrap)."""
        return {"disponible": "success", "en_uso": "primary", "en_reparacion": "warning", "danada": "danger",
                "baja": "dark"}[self.estado]


class EntregaHerramienta(models.Model):
    """Historial de quién ha tenido cada herramienta (la foto del nombre no cambia aunque se edite)."""

    class Tipo(models.TextChoices):
        """Si la herramienta se entregó al técnico o volvió a bodega."""
        ENTREGA = "entrega", "Entregada al técnico"
        DEVOLUCION = "devolucion", "Devuelta a bodega"

    herramienta = models.ForeignKey(Herramienta, on_delete=models.SET_NULL, null=True, related_name="entregas")
    herramienta_nombre = models.CharField(max_length=180)
    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.ENTREGA)
    tecnico = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    tecnico_nombre = models.CharField(max_length=180)
    fecha = models.DateTimeField(default=timezone_now)
    registrado_por = models.CharField(max_length=180, blank=True)
    importado_de = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["-fecha", "-pk"]


class AuditoriaHerramientas(models.Model):
    """Revisión física de las herramientas de un técnico. Es un registro permanente: no se edita ni se borra."""

    class Resultado(models.TextChoices):
        """Resultado general de la auditoría de herramientas."""
        CONFORME = "conforme", "Conforme"
        CON_NOVEDADES = "con_novedades", "Con novedades"

    tecnico = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="auditorias_herramientas")
    tecnico_nombre = models.CharField(max_length=180)
    fecha = models.DateTimeField(default=timezone_now, db_index=True)
    auditor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    auditor_nombre = models.CharField(max_length=180, blank=True)
    resultado = models.CharField(max_length=20, choices=Resultado.choices)
    total_herramientas = models.PositiveIntegerField(default=0)
    unidades_registradas = models.PositiveIntegerField(default=0)
    unidades_encontradas = models.PositiveIntegerField(default=0)
    novedades = models.PositiveIntegerField(default=0)
    nota_general = models.TextField(blank=True)
    ip = models.CharField(max_length=45, blank=True)
    importado_de = models.CharField(max_length=80, blank=True)

    class Meta:
        ordering = ["-fecha", "-pk"]
        verbose_name = "auditoría de herramientas"
        verbose_name_plural = "auditorías de herramientas"

    def __str__(self):
        return f"Auditoría #{self.pk} · {self.tecnico_nombre}"

    def get_absolute_url(self):
        """Ficha de la auditoría."""
        return reverse("inventario:auditoria", args=[self.pk])


class DetalleAuditoria(models.Model):
    """Una herramienta revisada: guarda la foto de lo registrado en ese momento (nombre, serial, cantidad, estado)."""

    class EstadoEncontrado(models.TextChoices):
        """Estado en que se encontró la herramienta al auditar."""
        EN_USO = "en_uso", "En uso"
        EN_REPARACION = "en_reparacion", "En reparación"
        DANADA = "danada", "Dañada"
        NO_ENCONTRADA = "no_encontrada", "No encontrada"

    class Resultado(models.TextChoices):
        """Resultado de cada herramienta auditada."""
        CONFORME = "conforme", "Conforme"
        CON_NOVEDAD = "con_novedad", "Con novedad"
        NO_ENCONTRADA = "no_encontrada", "No encontrada"
        DADA_DE_BAJA = "dada_de_baja", "Dada de baja"

    auditoria = models.ForeignKey(AuditoriaHerramientas, on_delete=models.CASCADE, related_name="detalles")
    herramienta = models.ForeignKey(Herramienta, on_delete=models.SET_NULL, null=True, related_name="auditorias")
    herramienta_nombre = models.CharField(max_length=180)
    marca = models.CharField(max_length=120, blank=True)
    serial = models.CharField(max_length=120, blank=True)
    cantidad_registrada = models.PositiveIntegerField(default=0)
    cantidad_encontrada = models.PositiveIntegerField(default=0)
    estado_registrado = models.CharField(max_length=20, blank=True)
    estado_encontrado = models.CharField(max_length=20, choices=EstadoEncontrado.choices)
    resultado = models.CharField(max_length=20, choices=Resultado.choices)
    nota = models.CharField(max_length=500, blank=True)

    class Meta:
        ordering = ["pk"]

    @property
    def estado_registrado_display(self):
        """Nombre legible del estado que tenía registrado la herramienta."""
        return dict(Herramienta.Estado.choices).get(self.estado_registrado, self.estado_registrado)
