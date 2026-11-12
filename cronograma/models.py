"""Cronograma técnico semanal (reemplaza cronogramajanotec.netlify.app) y documentos del personal.

Los tickets «recepcionados» se arrastran al cronograma: cada uno queda como una actividad con fecha,
horario, técnicos, insumos y a quién notificar. Al cliente se le envía la autorización de ingreso
(con los documentos de los técnicos) y responde «aceptar» o «rechazar» desde el correo.
"""
import os
import secrets
import uuid
from datetime import datetime, timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone

from tickets.models import fuera_de_horario


def ruta_documento(instancia, nombre):
    ext = os.path.splitext(nombre)[1].lower()[:10]
    return f"personal/{instancia.usuario_id}/{instancia.tipo}-{uuid.uuid4().hex}{ext}"


class DocumentoPersonal(models.Model):
    """Documentos de cada técnico que se adjuntan a la autorización de ingreso.

    Cambian con frecuencia (la planilla de seguridad social es mensual): al subir uno nuevo, el anterior
    queda en el historial y deja de ser el «actual».
    """

    class Tipo(models.TextChoices):
        CEDULA = "cedula", "Documento de identidad"
        SEGURIDAD_SOCIAL = "seguridad_social", "Aportes a seguridad social"
        ALTURAS = "alturas", "Certificado de trabajo seguro en alturas"

    # Siempre van en la autorización; el de alturas solo si la actividad es en alturas
    TIPOS_SIEMPRE = [Tipo.CEDULA, Tipo.SEGURIDAD_SOCIAL]

    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="documentos")
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    archivo = models.FileField(upload_to=ruta_documento)
    nombre_original = models.CharField(max_length=255)
    vigente_hasta = models.DateField("vigente hasta", null=True, blank=True,
                                     help_text="Para la seguridad social: el último día del periodo pagado.")
    actual = models.BooleanField(default=True)
    subido = models.DateTimeField(auto_now_add=True)
    subido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")

    class Meta:
        ordering = ["usuario", "tipo", "-subido"]
        verbose_name = "documento del personal"
        verbose_name_plural = "documentos del personal"

    def __str__(self):
        return f"{self.get_tipo_display()} · {self.usuario}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.actual:
            DocumentoPersonal.objects.filter(usuario=self.usuario, tipo=self.tipo, actual=True).exclude(pk=self.pk) \
                .update(actual=False)

    @property
    def vencido(self):
        return bool(self.vigente_hasta and self.vigente_hasta < timezone.localdate())

    @property
    def por_vencer(self):
        hoy = timezone.localdate()
        return bool(self.vigente_hasta and hoy <= self.vigente_hasta <= hoy + timedelta(days=7))

    @classmethod
    def actuales(cls, usuario):
        return {d.tipo: d for d in cls.objects.filter(usuario=usuario, actual=True)}


class ProgramacionSemana(models.Model):
    """«Programar para el jueves»: la semana queda lista y el envío automático la despacha el jueves."""

    lunes = models.DateField(unique=True)
    programada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    programada_en = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "semana programada para el jueves"
        verbose_name_plural = "semanas programadas para el jueves"

    def __str__(self):
        return f"Semana del {self.lunes:%d/%m/%Y}"


class PublicacionSemana(models.Model):
    """Lo que se envió por correo de una semana: sirve para saber qué cambió después y avisarlo."""

    lunes = models.DateField(unique=True)
    enviada_en = models.DateTimeField()
    enviada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    automatica = models.BooleanField(default=False)
    # {id de actividad: {campo: valor}} tal como quedó en el último envío
    foto = models.JSONField(default=dict)

    class Meta:
        verbose_name = "envío de cronograma"
        verbose_name_plural = "envíos de cronograma"

    def __str__(self):
        return f"Semana del {self.lunes:%d/%m/%Y}"


class Actividad(models.Model):
    """Una fila del cronograma semanal (las mismas columnas de la plataforma anterior)."""

    class Tipo(models.TextChoices):
        MANTENIMIENTO = "mantenimiento", "Mantenimiento"
        SOPORTE = "soporte", "Soporte técnico"
        INSTALACION = "instalacion", "Instalación"
        VISITA = "visita", "Visita técnica"
        INTERNA = "interna", "Actividad interna"  # sin ticket: recoger demos, viajes, capacitaciones…

    class Autorizacion(models.TextChoices):
        SIN_ENVIAR = "sin_enviar", "Autorización sin enviar"
        ENVIADA = "enviada", "Esperando respuesta del cliente"
        ACEPTADA = "aceptada", "Aceptada por el cliente"
        RECHAZADA = "rechazada", "Rechazada por el cliente"

    LINEAS = ["CCTV / videovigilancia", "Control de acceso", "Alarmas / intrusión", "Fibra óptica", "Radiocomunicaciones",
              "Energía fotovoltaica", "Automatización", "Cableado estructurado", "Otro"]
    DURACIONES_SST = ["medio día", "día completo", "dos días"]

    ticket = models.ForeignKey("tickets.Ticket", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="actividades")
    fecha = models.DateField()
    hora_inicio = models.TimeField("inicio")
    hora_fin = models.TimeField("fin")
    cliente = models.CharField(max_length=200)
    sede = models.CharField(max_length=200, blank=True)
    actividad = models.TextField()
    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.SOPORTE)
    # LINEAS son sugerencias: se puede escribir otra línea o solución
    linea = models.CharField("línea / solución", max_length=60, default="Otro")
    encargados = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name="actividades_encargado",
                                        verbose_name="personal técnico encargado")
    insumos = models.TextField("insumos y equipos", blank=True)
    notificar_a = models.CharField("notificar a", max_length=500, blank=True,
                                   help_text="Correos del cliente que reciben la autorización de ingreso (separados por «;»).")
    trabajo_alturas = models.BooleanField("trabajo en alturas", default=False)
    sst = models.BooleanField("acompañamiento SST (HSE)", default=False)
    sst_persona = models.CharField("persona HSE solicitada", max_length=150, blank=True)
    sst_trabajo = models.CharField("tipo de trabajo (justificación HSE)", max_length=200, blank=True)
    sst_duracion = models.CharField("duración prevista", max_length=20, default="medio día",
                                    choices=[(x, x) for x in DURACIONES_SST])
    sst_enviada = models.DateTimeField("solicitud SST enviada", null=True, blank=True)
    vehiculo = models.BooleanField("requiere vehículo", default=False)
    viaticos = models.BooleanField("requiere viáticos", default=False)
    # Autorización de ingreso
    autorizacion = models.CharField(max_length=20, choices=Autorizacion.choices, default=Autorizacion.SIN_ENVIAR)
    autorizacion_enviada = models.DateTimeField(null=True, blank=True)
    autorizacion_respuesta = models.DateTimeField(null=True, blank=True)
    motivo_rechazo = models.CharField("motivo del rechazo", max_length=300, blank=True)
    # Quién respondió la autorización: cualquiera de los correos notificados (principal o copia) puede hacerlo
    respondio_correo = models.EmailField("correo de quien respondió", blank=True)
    respondio_nombre = models.CharField("nombre de quien respondió", max_length=150, blank=True)
    # Sube cuando se reprograma: los enlaces de correos anteriores dejan de valer
    version_enlace = models.PositiveIntegerField(default=1)
    choque = models.CharField("cruce con la disponibilidad del cliente", max_length=300, blank=True)
    creado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["fecha", "hora_inicio", "id"]
        verbose_name = "actividad del cronograma"
        verbose_name_plural = "actividades del cronograma"
        indexes = [models.Index(fields=["fecha"])]

    def __str__(self):
        return f"{self.fecha:%d/%m/%Y} {self.hora_inicio:%H:%M} · {self.cliente}"

    # --- Fechas -------------------------------------------------------------------
    @property
    def inicio(self):
        return timezone.make_aware(datetime.combine(self.fecha, self.hora_inicio))

    @property
    def fin(self):
        return timezone.make_aware(datetime.combine(self.fecha, self.hora_fin))

    @staticmethod
    def lunes_de(dia):
        return dia - timedelta(days=dia.weekday())

    def calcular_choque(self):
        """Explica si el horario cae cuando el cliente no puede recibir la atención, o cuando el experto SST
        ya tiene otra actividad a esa hora (no puede estar en dos sitios)."""
        choque = (self.ticket.choque_con_restricciones(self.inicio, self.fin) if self.ticket_id
                  else fuera_de_horario(self.inicio, self.fin))
        if not choque and self.sst:
            otra = self.cruce_sst()
            if otra:
                choque = (f"El experto SST{f' ({self.sst_persona})' if self.sst_persona else ''} ya tiene otra actividad: "
                          f"{otra.cliente} de {otra.hora_inicio:%H:%M} a {otra.hora_fin:%H:%M}.")
        self.choque = choque[:300]
        return self.choque

    def cruce_sst(self):
        """La otra actividad con SST que se cruza en el mismo horario (misma persona HSE, o sin persona definida)."""
        otras = Actividad.objects.filter(fecha=self.fecha, sst=True, hora_inicio__lt=self.hora_fin,
                                         hora_fin__gt=self.hora_inicio).exclude(pk=self.pk)
        if self.sst_persona:
            otras = otras.filter(models.Q(sst_persona__iexact=self.sst_persona) | models.Q(sst_persona=""))
        return otras.first()

    # --- Presentación ---------------------------------------------------------------
    @property
    def es_interna(self):
        """Trabajo interno de JANOTEC (sin ticket, o de una empresa del grupo): sin autorización ni avisos a un cliente."""
        return not self.ticket_id or self.ticket.empresa.es_del_grupo

    @property
    def correos(self):
        if self.es_interna:
            return []
        return [c.strip() for c in self.notificar_a.replace(",", ";").split(";") if c.strip()]

    @property
    def quien_respondio(self):
        """«Nombre (correo)» de quien aceptó o rechazó la visita, para que coordinación sepa quién fue."""
        if not self.respondio_correo:
            return ""
        return f"{self.respondio_nombre} ({self.respondio_correo})" if self.respondio_nombre else self.respondio_correo

    @property
    def en_rojo(self):
        """Como en la plataforma anterior: se resalta lo que necesita atención del coordinador."""
        return (bool(self.choque) or self.autorizacion == self.Autorizacion.RECHAZADA
                or (not self.correos and not self.es_interna))

    @property
    def motivo_rojo(self):
        if self.autorizacion == self.Autorizacion.RECHAZADA:
            return "El cliente rechazó la visita" + (f": {self.motivo_rechazo}" if self.motivo_rechazo else ".")
        if self.choque:
            return self.choque
        if not self.correos and not self.es_interna:
            return "Sin correo para notificar al cliente."
        return ""

    @property
    def color_autorizacion(self):
        return {"sin_enviar": "secondary", "enviada": "warning", "aceptada": "success", "rechazada": "danger"}[self.autorizacion]

    def tecnicos(self):
        """El personal técnico encargado de la actividad."""
        return list(self.encargados.all())

    # --- Valores iniciales desde el ticket -----------------------------------------
    TIPO_POR_TICKET = {"mantenimiento": Tipo.MANTENIMIENTO, "soporte": Tipo.SOPORTE, "requerimiento": Tipo.VISITA,
                       "instalacion": Tipo.INSTALACION}

    @classmethod
    def linea_para(cls, categoria):
        nombre = (str(categoria) if categoria else "").lower()
        for clave, linea in (("cctv", "CCTV / videovigilancia"), ("alarma", "Alarmas / intrusión"),
                             ("acceso", "Control de acceso"), ("solar", "Energía fotovoltaica"),
                             ("fotovolt", "Energía fotovoltaica"), ("redes", "Cableado estructurado"),
                             ("fibra", "Fibra óptica"), ("radio", "Radiocomunicaciones")):
            if clave in nombre:
                return linea
        return "Otro"

    @staticmethod
    def clase_visita(ticket):
        """«levantamiento» (1.ª visita, antes de la propuesta), «instalacion» (propuesta aprobada) o «servicio»."""
        ficha = getattr(ticket, "ficha", None) if ticket else None
        if ficha is None:
            return "servicio"
        return "instalacion" if ficha.etapa == "instalacion" else "levantamiento"

    @property
    def material_de_bodega(self):
        """Instalación con material alistado por bodega: los insumos son lo entregado, no se escriben ni se piden."""
        if self.tipo != self.Tipo.INSTALACION or not self.ticket_id:
            return None
        from inventario.solicitudes import material_para_instalacion
        material = material_para_instalacion(self.ticket)
        return material if material["hay"] else None

    @classmethod
    def valores_desde_ticket(cls, ticket):
        """Lo que se llena solo al arrastrar el ticket al cronograma."""
        ficha = getattr(ticket, "ficha", None)
        if ficha is not None:
            tipo = cls.Tipo.INSTALACION if ficha.etapa == "instalacion" else cls.Tipo.VISITA
        else:
            tipo = cls.TIPO_POR_TICKET.get(ticket.tipo, cls.Tipo.SOPORTE)
        valores = {
            "cliente": ticket.empresa.razon_social, "sede": ticket.sede.nombre if ticket.sede else "",
            "actividad": ticket.descripcion, "tipo": tipo, "linea": cls.linea_para(ticket.categoria),
            "notificar_a": " ; ".join(ticket.correos_respuesta), "trabajo_alturas": ticket.trabajo_alturas,
            "sst": ticket.acompanamiento_sst, "sst_trabajo": "trabajos en alturas" if ticket.trabajo_alturas else "",
            # La jornada que pidió el cliente (la tarde no aplica el sábado)
            "hora_inicio": "14:00" if ticket.jornada == "tarde" else "08:00",
            "hora_fin": "17:00" if ticket.jornada == "tarde" else "12:00",
        }
        if tipo == cls.Tipo.INSTALACION:
            from inventario.solicitudes import material_para_instalacion
            material = material_para_instalacion(ticket)
            if material["hay"]:
                valores.update(insumos=material["texto"], bodega=True)
        return valores


class Contacto(models.Model):
    """Directorio de la plataforma anterior: a quién se le envía qué (los técnicos son los usuarios)."""

    class Tipo(models.TextChoices):
        ADMINISTRATIVA = "administrativa", "Administrativa (recibe el consolidado de todas las sedes)"
        SST = "sst", "SST (recibe las solicitudes de acompañamiento HSE)"
        CONTACTO = "contacto", "Contacto (cliente o almacén: se le avisa de sus visitas)"

    nombre = models.CharField("nombre / referencia", max_length=150)
    email = models.EmailField("correo electrónico", blank=True)
    tipo = models.CharField(max_length=20, choices=Tipo.choices, default=Tipo.CONTACTO)
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["tipo", "nombre"]
        verbose_name = "contacto del directorio"
        verbose_name_plural = "directorio"

    def __str__(self):
        return self.nombre

    @classmethod
    def correo_de(cls, tipo, respaldo=""):
        """El correo del primer contacto activo de ese tipo; si no hay, el de la configuración (.env)."""
        c = cls.objects.filter(tipo=tipo, activo=True).exclude(email="").first()
        return c.email if c else respaldo


def nuevo_token_enlace():
    return secrets.token_urlsafe(32)


class EnlaceCronograma(models.Model):
    """Enlace permanente de solo lectura para quien no usa la plataforma (dirección administrativa, gerencia…).

    Hay uno activo a la vez: si se filtra se revoca y se crea otro.
    """

    token = models.CharField(max_length=64, unique=True, default=nuevo_token_enlace, editable=False)
    activo = models.BooleanField(default=True)
    creado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)
    ultimo_acceso = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "enlace del cronograma"
        verbose_name_plural = "enlaces del cronograma"

    def __str__(self):
        return f"Enlace del {self.creado:%d/%m/%Y}"

    def registrar_acceso(self):
        """La página se refresca sola: se anota cada 5 minutos para no escribir en cada consulta."""
        ahora = timezone.now()
        if not self.ultimo_acceso or ahora - self.ultimo_acceso > timedelta(minutes=5):
            EnlaceCronograma.objects.filter(pk=self.pk).update(ultimo_acceso=ahora)


class ConfiguracionEnvio(models.Model):
    """Configuración del envío (antes protegida con clave en la plataforma anterior; aquí solo el área técnica)."""

    class Semana(models.TextChoices):
        SIGUIENTE = "siguiente", "La semana siguiente"
        ACTUAL = "actual", "La semana en curso"

    HORAS = [("jue-14", "Jueves 2:00 p. m."), ("jue-16", "Jueves 4:00 p. m."), ("jue-17", "Jueves 5:00 p. m.")]

    envio_automatico = models.BooleanField("envío automático del jueves", default=True,
                                           help_text="Apagado: el comando programado no envía nada.")
    hora = models.CharField("día y hora programada", max_length=10, choices=HORAS, default="jue-16",
                            help_text="Informativo: la hora real la fija la tarea programada en el servidor.")
    semana_objetivo = models.CharField("¿qué semana se envía?", max_length=10, choices=Semana.choices,
                                       default=Semana.SIGUIENTE)
    remitente = models.CharField("remite como", max_length=150, default="Dirección TIC · JANOTEC S.A.S.")
    avisar_contactos = models.BooleanField(
        "incluir aviso a contactos/clientes en el envío", default=False,
        help_text="Los clientes ya reciben la autorización de ingreso de cada visita; actívelo si también deben "
                  "recibir el resumen semanal de sus visitas.")

    class Meta:
        verbose_name = "configuración del envío"
        verbose_name_plural = "configuración del envío"

    def __str__(self):
        return "Configuración del envío"

    @classmethod
    def actual(cls):
        return cls.objects.get_or_create(pk=1)[0]


def ruta_evidencia_horas(instancia, nombre):
    """Las fotos de las horas extras van por mes: horas_extras/2026/11/<archivo>."""
    hoy = timezone.localdate()
    return f"horas_extras/{hoy:%Y/%m}/{nombre}"


class ReporteHorasExtra(models.Model):
    """Auto reporte de salida a campo (antes un Google Forms), en dos momentos y con una foto en cada uno:

    * **Inicio**: el conductor cuenta desde que le entregan las llaves para el alistamiento (pre operación) del vehículo;
      el acompañante, desde que llega a la sede del cliente e inicia la operación.
    * **Regreso**: la hora de llegada, con su foto. Hasta que no se cierre, el reporte no suma horas.
    """

    class Rol(models.TextChoices):
        CONDUCTOR = "conductor", "Conductor (desde la entrega de llaves y el alistamiento del vehículo)"
        ACOMPANANTE = "acompanante", "Acompañante (desde la llegada a la sede del cliente)"

    colaborador = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="horas_extras")
    fecha = models.DateField(default=timezone.localdate)
    rol = models.CharField("en esta salida fue", max_length=12, choices=Rol.choices, blank=True)
    hora_salida = models.TimeField("hora de inicio")
    evidencia_salida = models.ImageField("foto del inicio", upload_to=ruta_evidencia_horas, blank=True,
                                         help_text="Conductor: las llaves o el vehículo en alistamiento. Acompañante: la llegada a la sede.")
    hora_llegada = models.TimeField("hora de llegada", null=True, blank=True)
    evidencia_llegada = models.ImageField("foto del regreso", upload_to=ruta_evidencia_horas, blank=True)
    cerrado_en = models.DateTimeField("regreso registrado", null=True, blank=True)
    cliente = models.CharField("cliente al que se dirige", max_length=200)
    registrado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                       related_name="+")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-fecha", "-hora_salida"]
        verbose_name = "reporte de horas extras"
        verbose_name_plural = "reportes de horas extras"

    def __str__(self):
        return f"{self.colaborador} · {self.fecha:%d/%m/%Y} · {self.cliente}"

    @property
    def abierto(self):
        """Salió pero todavía no registró el regreso."""
        return self.hora_llegada is None

    @property
    def rol_corto(self):
        return {"conductor": "Conductor", "acompanante": "Acompañante"}.get(self.rol, "—")

    @property
    def minutos(self):
        """Minutos del inicio al regreso; 0 mientras esté abierto."""
        if self.hora_llegada is None:
            return 0
        salida = self.hora_salida.hour * 60 + self.hora_salida.minute
        llegada = self.hora_llegada.hour * 60 + self.hora_llegada.minute
        return (llegada - salida) % (24 * 60)  # si llega después de medianoche, sigue contando

    @property
    def duracion(self):
        return f"{self.minutos // 60}:{self.minutos % 60:02d}"


class SolicitudViaticos(models.Model):
    """Viáticos que necesita el personal para ir a la sede de un cliente (alimentación, transporte, gasolina u otros).
    Los gestiona por completo el área financiera: le llega el aviso, los entrega y la marca como gestionada.

    Se pide al programar la actividad en el cronograma (casillas «Viáticos SST/SGI» y «Viáticos técnicos») o desde el
    ticket. Puede ser para el personal SST/SGI, para el personal técnico o para los dos."""

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente de financiera"
        GESTIONADA = "gestionada", "Gestionada por financiera"
        CANCELADA = "cancelada", "Cancelada (ya no se necesitan)"

    CONCEPTOS = (("alimentacion", "Alimentación"), ("transporte", "Transporte"), ("gasolina", "Gasolina"), ("otros", "Otros"))

    ticket = models.ForeignKey("tickets.Ticket", on_delete=models.SET_NULL, null=True, blank=True, related_name="viaticos")
    actividad = models.OneToOneField(Actividad, on_delete=models.SET_NULL, null=True, blank=True, related_name="solicitud_viaticos")
    fecha = models.DateField("fecha del desplazamiento")
    destino = models.CharField("cliente y sede", max_length=300)
    para_sst = models.BooleanField("personal SST / SGI", default=False)
    para_tecnicos = models.BooleanField("personal técnico", default=False)
    personas = models.CharField("quiénes van", max_length=500, blank=True)
    alimentacion = models.BooleanField("alimentación", default=False)
    transporte = models.BooleanField("transporte", default=False)
    gasolina = models.BooleanField("gasolina", default=False)
    otros = models.BooleanField("otros", default=False)
    detalle = models.CharField("detalle u otros conceptos", max_length=300, blank=True)
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.PENDIENTE, db_index=True)
    solicitada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)
    # Lo que registra financiera al gestionarla
    valor_entregado = models.DecimalField("valor entregado", max_digits=12, decimal_places=2, null=True, blank=True)
    nota_financiera = models.CharField("nota de financiera", max_length=300, blank=True)
    soporte = models.FileField("soporte (opcional)", upload_to="viaticos/%Y/%m/", blank=True)
    gestionada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    gestionada_en = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["fecha", "pk"]
        verbose_name = "solicitud de viáticos"
        verbose_name_plural = "solicitudes de viáticos"

    def __str__(self):
        return f"Viáticos {self.numero} · {self.destino}"

    @property
    def numero(self):
        return f"VI-{self.pk:05d}" if self.pk else "VI-nueva"

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("cronograma:viaticos") + f"#vi-{self.pk}"

    @property
    def para_texto(self):
        """«Personal SST/SGI y personal técnico», «Personal técnico»…"""
        partes = [x for x, si in (("personal SST/SGI", self.para_sst), ("personal técnico", self.para_tecnicos)) if si]
        texto = " y ".join(partes)
        return (texto[0].upper() + texto[1:]) if texto else "Sin definir"

    @property
    def conceptos_texto(self):
        nombres = [n for c, n in self.CONCEPTOS if getattr(self, c)]
        texto = ", ".join(nombres) or "Por definir"
        return texto + (f" ({self.detalle})" if self.detalle else "")
