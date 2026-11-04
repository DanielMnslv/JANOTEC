from django.contrib.auth.models import AbstractUser
from django.db import models


class Empresa(models.Model):
    """Cliente corporativo de JANOTEC (ej. Servicios Postales 4-72)."""

    class Sector(models.TextChoices):
        PRIVADO = "privado", "Privado"
        PUBLICO = "publico", "Público"

    codigo = models.SlugField(
        "código", max_length=40, unique=True, null=True, blank=True,
        help_text="Identificador corto usado por la importación de datos maestros (ej. llanogas).",
    )
    razon_social = models.CharField("razón social", max_length=200)
    nit = models.CharField("NIT", max_length=30, unique=True, null=True, blank=True)
    sector = models.CharField(max_length=10, choices=Sector.choices, default=Sector.PRIVADO)
    ciudad = models.CharField(max_length=100, blank=True)
    direccion = models.CharField("dirección", max_length=200, blank=True)
    telefono = models.CharField("teléfono", max_length=50, blank=True)
    email_contacto = models.EmailField("correo de contacto", blank=True)
    activa = models.BooleanField(default=True)
    es_del_grupo = models.BooleanField(
        "empresa del grupo JANOTEC", default=False,
        help_text="Seguridad Jano, Jano Tec…: su trabajo es interno (sin autorización de ingreso ni contacto externo).",
    )
    es_prospecto = models.BooleanField(
        "prospecto", default=False,
        help_text="Aún no es cliente: se registró desde un requerimiento comercial. Pasa a cliente si gana una propuesta.",
    )
    creada = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["razon_social"]
        verbose_name = "empresa cliente"
        verbose_name_plural = "empresas cliente"

    def __str__(self):
        return self.razon_social

    @property
    def encargado(self):
        """El encargado actual (el primero que sigue activo)."""
        return next((a for a in self.encargados.all() if a.activo), None)


class EncargadoCliente(models.Model):
    """La persona a cargo de JANOTEC en el cliente. Si cambia, la anterior queda inactiva y se conserva el historial.

    Es información interna: no recibe correos de la plataforma (esos van a los contactos del ticket y de la sede).
    """

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="encargados")
    nombre = models.CharField(max_length=150)
    cargo = models.CharField(max_length=100, blank=True)
    celular = models.CharField(max_length=40, blank=True)
    correo = models.EmailField(blank=True)
    notas = models.CharField(max_length=300, blank=True)
    activo = models.BooleanField("sigue siendo el encargado", default=True)

    class Meta:
        ordering = ["-activo", "nombre"]
        verbose_name = "encargado del cliente"
        verbose_name_plural = "encargados de clientes"

    def __str__(self):
        return f"{self.nombre} · {self.empresa}"


class Sede(models.Model):
    """Sede o punto del cliente donde está instalado el servicio."""

    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="sedes")
    nombre = models.CharField(max_length=150)
    departamento = models.CharField(max_length=100, blank=True, default="Meta")
    ciudad = models.CharField("municipio", max_length=100, blank=True)
    direccion = models.CharField("dirección", max_length=200, blank=True)
    abonado = models.CharField(
        "abonado de alarma", max_length=20, blank=True,
        help_text="Número de abonado del panel de alarma de la sede.",
    )
    # Quién recibe en la sede: se llena solo al radicar y recibe la autorización de ingreso
    contacto_nombre = models.CharField("contacto en la sede", max_length=150, blank=True)
    contacto_cargo = models.CharField("cargo del contacto", max_length=100, blank=True)
    contacto_telefono = models.CharField("teléfono del contacto", max_length=50, blank=True)
    contacto_email = models.CharField("correo del contacto", max_length=300, blank=True,
                                      help_text="Puede escribir varios, separados por «;».")
    activa = models.BooleanField(default=True)

    class Meta:
        ordering = ["empresa", "nombre"]
        unique_together = [("empresa", "nombre")]

    def __str__(self):
        if self.ciudad and self.ciudad.lower() not in self.nombre.lower():
            return f"{self.nombre} ({self.ciudad})"
        return self.nombre


class Usuario(AbstractUser):
    class Rol(models.TextChoices):
        CLIENTE = "cliente", "Cliente"
        AGENTE = "agente", "Encargado (técnico)"
        SUPERVISOR = "supervisor", "Área técnica (coordinador)"
        COMERCIAL = "comercial", "Área comercial"
        DIRECCION = "direccion", "Dirección (ve todas las áreas)"
        CENTRAL = "central", "Central de atención"
        BODEGA = "bodega", "Administración y bodega (inventario)"
        ADMINISTRATIVO = "administrativo", "Área administrativa (propuestas)"
        FINANCIERA = "financiera", "Área financiera (pagos y facturas)"

    rol = models.CharField(max_length=20, choices=Rol.choices, default=Rol.CLIENTE)
    tambien_comercial = models.BooleanField(
        "también área comercial", default=False,
        help_text="Además de su rol (p. ej. bodega y logística), atiende los requerimientos comerciales: radica, "
                  "pide el levantamiento, hace seguimiento al cliente y registra su respuesta.",
    )
    tambien_administrativo = models.BooleanField(
        "también área administrativa", default=False,
        help_text="Además de su rol (p. ej. bodega y logística), arma las propuestas: cotizador, catálogo de precios, "
                  "cotización con proveedores y envío a dirección para su aprobación.",
    )
    tambien_financiera = models.BooleanField(
        "también área financiera", default=False,
        help_text="Además de su rol, paga las órdenes de compra (sube el soporte de pago) y valida las facturas de los proveedores.",
    )
    tambien_tecnico = models.BooleanField(
        "también técnico", default=False,
        help_text="Además de su rol (p. ej. dirección), va a visitas, recibe material y aparece en las listas de técnicos.",
    )
    empresa = models.ForeignKey(
        Empresa,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="usuarios",
        help_text="Obligatorio para usuarios con rol Cliente.",
    )
    telefono = models.CharField("teléfono", max_length=50, blank=True)
    cargo = models.CharField(max_length=100, blank=True)
    documento = models.CharField("cédula", max_length=20, blank=True, db_index=True,
                                 help_text="Para asociarle lo que el inventario registraba a su nombre.")
    area = models.CharField("área", max_length=60, blank=True, help_text="Técnica, administrativa, sistemas, comercial…")
    sede = models.CharField("sede de trabajo", max_length=100, blank=True, default="Principal")

    class Meta:
        verbose_name = "usuario"
        verbose_name_plural = "usuarios"

    def __str__(self):
        return self.get_full_name() or self.username

    @property
    def es_cliente(self):
        return self.rol == self.Rol.CLIENTE and not self.is_superuser

    @property
    def es_personal(self):
        """Personal del área técnica (técnicos, coordinador), dirección o superusuario."""
        return self.is_superuser or self.tambien_tecnico or self.rol in (self.Rol.AGENTE, self.Rol.SUPERVISOR, self.Rol.DIRECCION)

    @classmethod
    def tecnicos(cls):
        """Quienes pueden ir a una visita o recibir material: técnicos, coordinadores y quien tenga «también técnico»."""
        return cls.objects.filter(models.Q(rol__in=[cls.Rol.AGENTE, cls.Rol.SUPERVISOR]) | models.Q(tambien_tecnico=True), is_active=True)

    def save(self, *args, **kwargs):
        # El acceso a Configuración lo da el rol (los superusuarios siempre lo tienen)
        if not self.is_superuser:
            self.is_staff = self.rol in (self.Rol.SUPERVISOR, self.Rol.DIRECCION)
        super().save(*args, **kwargs)

    @property
    def es_direccion(self):
        """Ve y gestiona todas las áreas (técnica y comercial)."""
        return self.is_superuser or self.rol == self.Rol.DIRECCION

    @property
    def es_coordinador(self):
        """El coordinador del área técnica (Guana): solo él aplaza las visitas programadas."""
        return self.is_superuser or self.rol == self.Rol.SUPERVISOR

    @property
    def es_supervisor(self):
        """Dirige el área técnica: asigna técnicos, fija prioridad, programa visitas."""
        return self.is_superuser or self.rol in (self.Rol.SUPERVISOR, self.Rol.DIRECCION)

    # --- Comercial y administrativa: dos áreas sobre el mismo requerimiento ---------------------------
    # Comercial trae los requerimientos y les hace seguimiento (radica, pide el levantamiento, registra la respuesta
    # del cliente y lo pasa a instalación). Administrativa arma la propuesta (cotizador, catálogo, proveedores) y la
    # envía a dirección. Dirección hace las dos cosas (y cubre comercial mientras esa área no tenga usuario).
    @property
    def es_comercial(self):
        """Área comercial: por rol o con la opción «también área comercial»."""
        return (self.rol == self.Rol.COMERCIAL or self.tambien_comercial) and not self.is_superuser

    @property
    def es_administrativo(self):
        """Área administrativa (propuestas): por rol o con la opción «también área administrativa»."""
        return (self.rol == self.Rol.ADMINISTRATIVO or self.tambien_administrativo) and not self.is_superuser

    @property
    def ve_comercial(self):
        """Entra a los requerimientos comerciales: comercial, administrativa y dirección (no el coordinador técnico)."""
        return self.es_comercial or self.es_administrativo or self.es_direccion

    @property
    def gestiona_requerimientos(self):
        """Radica, pide el levantamiento, registra la respuesta del cliente y traspasa: comercial y dirección."""
        return self.es_comercial or self.es_direccion

    @property
    def arma_propuestas(self):
        """Cotizador, catálogo de precios y cotización con proveedores: administrativa y dirección."""
        return self.es_administrativo or self.es_direccion

    @property
    def es_financiera(self):
        """Área financiera: por rol o con la opción «también área financiera»."""
        return (self.rol == self.Rol.FINANCIERA or self.tambien_financiera) and not self.is_superuser

    @property
    def ve_financiera(self):
        """Paga las órdenes de compra y valida facturas: financiera y dirección (que la cubre mientras no tenga usuario)."""
        return self.es_financiera or self.es_direccion

    @property
    def es_bodega(self):
        return self.rol == self.Rol.BODEGA and not self.is_superuser

    @property
    def gestiona_inventario(self):
        """Bodega y dirección registran compras, entregas y devoluciones. Coordinación no entra al módulo: ve el
        avance del alistamiento en el ticket, autoriza la salida desde ahí y administra los funcionarios."""
        return self.is_superuser or self.rol in (self.Rol.BODEGA, self.Rol.DIRECCION)

    @property
    def ve_inventario(self):
        return self.gestiona_inventario or self.rol == self.Rol.AGENTE or self.tambien_tecnico

    @property
    def es_central(self):
        return self.rol == self.Rol.CENTRAL and not self.is_superuser

    @property
    def es_interno(self):
        """Cualquier persona de JANOTEC (no cliente): puede ser responsable de un ticket."""
        return self.is_superuser or self.rol != self.Rol.CLIENTE

    @property
    def radica_para_clientes(self):
        """Radica solicitudes a nombre de cualquier cliente: personal JANOTEC, central de atención y comercial."""
        return self.es_personal or self.es_central or self.es_comercial or self.es_administrativo

    @property
    def rol_corto(self):
        """Para la barra: «Dirección», «Área técnica», «Encargado»… (sin la aclaración entre paréntesis)."""
        if self.is_superuser and self.rol != self.Rol.SUPERVISOR:
            return "Admin"
        if self.rol == self.Rol.BODEGA and self.tambien_administrativo:
            return "Bodega y administrativa"
        if self.rol == self.Rol.BODEGA and self.tambien_comercial:
            return "Bodega y comercial"
        return self.get_rol_display().split(" (")[0] + (" y técnico" if self.tambien_tecnico else "")
