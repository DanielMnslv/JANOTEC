"""Modelos de la plataforma de tickets.

Organización del archivo:
- Catálogos: sistemas a intervenir (Categoria) y sus fallas (TipoFalla).
- Disponibilidad del cliente: franjas horarias y horario laboral.
- Ticket: la solicitud del cliente, con su radicado consecutivo, estados, aplazamientos, etapas visibles al
  cliente, rangos en que no puede atender, historial (Seguimiento) y adjuntos.
- Acta de soporte: lo que se hizo en la visita, con equipos, fotos, evidencias y firmas.
- Informe al cliente: borrador del informe de visita o de estudio, por zonas con fotos.
- Oportunidades comerciales y hallazgos detectados en campo.
- Pasos del área técnica que coordinación decide omitir.
"""

import os
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Sede


# --- Catálogos: sistemas a intervenir y fallas ----------------------------------------------------
class Categoria(models.Model):
    """Sistema a intervenir que el cliente elige al radicar (CCTV, alarma, control de acceso…).

    Las inactivas no se ofrecen al radicar, pero se conservan por los tickets y actas que las usan.
    """
    nombre = models.CharField(max_length=100, unique=True)
    descripcion = models.CharField("descripción", max_length=255, blank=True)
    activa = models.BooleanField(default=True)
    orden = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["orden", "nombre"]
        verbose_name = "categoría"
        verbose_name_plural = "categorías"

    def __str__(self):
        return self.nombre


class TipoFalla(models.Model):
    """Fallas o necesidades típicas de cada servicio (lista que ve el cliente al radicar)."""

    categoria = models.ForeignKey(Categoria, on_delete=models.CASCADE, related_name="fallas",
                                  verbose_name="servicio")
    nombre = models.CharField(max_length=120)
    activa = models.BooleanField(default=True)
    orden = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["categoria", "orden", "nombre"]
        unique_together = [("categoria", "nombre")]
        verbose_name = "tipo de falla"
        verbose_name_plural = "tipos de falla"

    def __str__(self):
        return self.nombre


DIAS_SEMANA = [("lunes", "Lunes"), ("martes", "Martes"), ("miercoles", "Miércoles"), ("jueves", "Jueves"),
               ("viernes", "Viernes"), ("sabado", "Sábado"), ("domingo", "Domingo")]
# Domingo no se trabaja; el sábado solo de 8:00 a. m. a 12:00 m. (por eso el cliente elige mañana o tarde, no horas:
# a las 4 p. m. ya no alcanza para un procedimiento completo).
DIAS_LABORALES = DIAS_SEMANA[:6]
FRANJAS_NO_DISPONIBLE = [("manana", "No en la mañana"), ("tarde", "No en la tarde"), ("todo", "No todo el día")]
FRANJAS_SABADO = [("todo", "No puede el sábado")]


# --- Disponibilidad del cliente y horario laboral -------------------------------------------------
def fuera_de_horario(inicio, fin=None):
    """Explica si la actividad cae fuera del horario de trabajo de JANOTEC."""
    if not inicio:
        return ""
    local, local_fin = timezone.localtime(inicio), timezone.localtime(fin or inicio)
    if local.weekday() == 6:
        return "Los domingos no se trabaja."
    if local.weekday() == 5 and (local.hour >= 12 or (local_fin.hour, local_fin.minute) > (12, 0)):
        return "El sábado solo se trabaja de 8:00 a. m. a 12:00 m."
    return ""


def leer_franja(valor):
    """Lo guardado para un día → (tipo, desde, hasta). Las horas se guardan como «08:00-10:00»."""
    valor = valor or ""
    if ":" in valor and "-" in valor:
        desde, hasta = valor.split("-", 1)
        return "horas", desde.strip(), hasta.strip()
    return valor, "", ""


def texto_franja(valor):
    """La franja guardada en palabras para el cliente: «mañana», «tarde», «todo el día» o «de 08:00 a 10:00»."""
    tipo, desde, hasta = leer_franja(valor)
    return {"manana": "mañana", "tarde": "tarde", "todo": "todo el día"}.get(tipo, f"de {desde} a {hasta}" if desde else "")


# --- Ticket: radicado, estados, historial y adjuntos ----------------------------------------------
class ContadorRadicado(models.Model):
    """Lleva el consecutivo de radicados por año (se reinicia cada año)."""

    anio = models.PositiveIntegerField(primary_key=True)
    ultimo = models.PositiveIntegerField(default=0)

    class Meta:
        verbose_name = "contador de radicados"
        verbose_name_plural = "contadores de radicados"

    @classmethod
    def siguiente(cls, anio=None):
        """Reserva el siguiente número de radicado del año, bloqueando la fila para que dos tickets
        simultáneos nunca reciban el mismo. Devuelve (año, número).
        """
        anio = anio or timezone.localdate().year
        with transaction.atomic():
            cls.objects.get_or_create(anio=anio)
            # select_for_update bloquea la fila: dos tickets simultáneos
            # nunca reciben el mismo número.
            contador = cls.objects.select_for_update().get(anio=anio)
            contador.ultimo += 1
            contador.save(update_fields=["ultimo"])
            return anio, contador.ultimo


class Ticket(models.Model):
    """Solicitud del cliente (soporte, mantenimiento, instalación, requerimiento o cotización).

    Es el hilo de todo el servicio: un solo radicado acompaña al cliente desde que radica hasta el cierre, pasando por
    el área técnica, el área comercial y bodega. Su estado define la bandeja en que aparece y lo que se puede hacer.
    """
    class Estado(models.TextChoices):
        # Flujo técnico: generado → recepcionado → en cronograma → autorizado → visita ejecutada → cerrado
        """Estados del ticket. El flujo técnico es: generado → recepcionado → en cronograma → autorizado → visita
        ejecutada → cerrado; los demás cubren el informe, la propuesta, bodega, aplazamientos y cancelación.
        """
        GENERADO = "generado", "Generado"
        RECEPCIONADO = "recepcionado", "Recepcionado"
        EN_CRONOGRAMA = "en_cronograma", "En cronograma"
        AUTORIZADO = "autorizado", "Autorizado"
        VISITA_EJECUTADA = "visita_ejecutada", "Visita técnica ejecutada"
        PENDIENTE_VISITA = "pendiente_visita", "Pendiente nueva visita"
        INFORME = "informe", "Informe en elaboración"
        # Mientras el área comercial lo trabaja (propuesta, negociación)
        EN_PROCESO = "en_proceso", "En gestión comercial"
        PROPUESTA_ENVIADA = "propuesta_enviada", "Propuesta enviada"
        # Propuesta aprobada: bodega alista los materiales y equipos; coordinación decide cuándo pasa a recepcionado
        EN_INVENTARIO = "en_inventario", "En inventario"
        CERRADO = "cerrado", "Cerrado"
        CANCELADO = "cancelado", "Cancelado"

    class Prioridad(models.TextChoices):
        """Urgencia con que el área técnica atiende el ticket."""
        BAJA = "baja", "Baja"
        MEDIA = "media", "Media"
        ALTA = "alta", "Alta"
        CRITICA = "critica", "Crítica"

    class Tipo(models.TextChoices):
        """Qué pide el cliente; define el área que lo atiende y las etapas que ve."""
        MANTENIMIENTO = "mantenimiento", "Mantenimiento"
        SOPORTE = "soporte", "Servicio técnico"
        INSTALACION = "instalacion", "Instalación"
        REQUERIMIENTO = "requerimiento", "Toma de requerimientos"
        COTIZACION = "cotizacion", "Cotización / propuesta económica"  # la radica el área comercial

    # Lo que se explica al radicar; ayuda a elegir el tipo correcto.
    AYUDA_TIPO = {
        "mantenimiento": "Revisión o mantenimiento de algo que ya funciona.",
        "soporte": "Algo dejó de funcionar: una cámara, la alarma, la red, una puerta, un equipo.",
        "instalacion": "Instalar un sistema nuevo o ampliar uno que ya tiene (cámaras, alarma, control de acceso…).",
        "requerimiento": "No sabe bien qué necesita: un técnico visita el sitio y define qué se requiere.",
        "cotizacion": "Pedir una propuesta para instalar o ampliar un sistema.",
    }

    class Impacto(models.TextChoices):
        """Nivel de urgencia: lo define el coordinador técnico al recepcionar (el cliente no lo elige)."""

        BAJA = "baja", "Baja"
        MEDIA = "media", "Media"
        ALTA = "alta", "Alta"

    class Jornada(models.TextChoices):
        """En qué jornada quiere el cliente la visita (por horas no: el procedimiento necesita la jornada completa)."""

        MANANA = "manana", "Mañana"
        TARDE = "tarde", "Tarde"

    # Lo que se le explica al cliente debajo de cada opción
    AYUDA_IMPACTO = {
        "baja": "Puede esperar: es una consulta o una molestia menor.",
        "media": "Afecta parte del servicio (ej. una cámara o una zona).",
        "alta": "Afecta gravemente la operación o la seguridad, o el servicio está caído.",
    }

    # Prioridad inicial sugerida; el área de soporte la ajusta al revisar.
    PRIORIDAD_POR_IMPACTO = {"baja": "baja", "media": "media", "alta": "alta"}
    PRIORIDAD_POR_TIPO = {"mantenimiento": "media", "requerimiento": "baja", "instalacion": "media"}

    class Area(models.TextChoices):
        """Quién tiene la solicitud en este momento. Un mismo ticket puede pasar de un área a otra."""
        TECNICA = "tecnica", "Área técnica"
        COMERCIAL = "comercial", "Área comercial"

    # Estados en los que el ticket sigue "vivo" para el área de soporte
    ESTADOS_ABIERTOS = [
        Estado.GENERADO,
        Estado.RECEPCIONADO,
        Estado.EN_CRONOGRAMA,
        Estado.AUTORIZADO,
        Estado.PENDIENTE_VISITA,
        Estado.INFORME,
        Estado.EN_PROCESO,
        Estado.PROPUESTA_ENVIADA,
        Estado.EN_INVENTARIO,
    ]
    ESTADOS_FINALES = [Estado.CERRADO, Estado.CANCELADO]
    # Los que se pueden arrastrar al cronograma semanal
    ESTADOS_PROGRAMABLES = [Estado.RECEPCIONADO, Estado.PENDIENTE_VISITA]
    # Mientras el técnico puede diligenciar el acta
    ESTADOS_EN_ATENCION = [Estado.EN_CRONOGRAMA, Estado.AUTORIZADO, Estado.PENDIENTE_VISITA, Estado.INFORME,
                           Estado.VISITA_EJECUTADA]

    # Pasos que ve el cliente en la barra de progreso
    PASOS_CLIENTE = [
        (Estado.GENERADO, "Generado"),
        (Estado.RECEPCIONADO, "Recepcionado"),
        (Estado.EN_CRONOGRAMA, "En cronograma"),
        (Estado.AUTORIZADO, "Autorizado"),
        (Estado.VISITA_EJECUTADA, "Visita ejecutada"),
        (Estado.CERRADO, "Cerrado"),
    ]

    radicado = models.CharField(max_length=30, unique=True, editable=False)
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="tickets")
    sede = models.ForeignKey(
        Sede, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    solicitante = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tickets_solicitados"
    )
    # «¿Quién lo está gestionando?»: quien radicó (cliente, personal JANOTEC, central de atención o comercial)
    radicado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets_radicados",
        verbose_name="gestionado por",
    )
    departamento = models.CharField(max_length=100, blank=True)
    municipio = models.CharField(max_length=100, blank=True)
    direccion = models.CharField("dirección", max_length=200, blank=True)
    trabajo_alturas = models.BooleanField(
        "trabajo seguro en alturas", default=False, help_text="La intervención supera los 2 metros a nivel del pie.",
    )
    acompanamiento_sst = models.BooleanField("acompañamiento SST", default=False)
    # Ubicación tomada del celular o computador de quien radica (ayuda al técnico a llegar)
    latitud = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitud = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    punto_referencia = models.CharField("punto de referencia", max_length=200, blank=True)
    # Lo que el cliente respondió «No sé» al radicar: el área técnica lo confirma al recepcionar
    por_confirmar = models.JSONField("datos por confirmar", default=list, blank=True)
    confirmado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name="+", verbose_name="datos confirmados por")
    confirmado_en = models.DateTimeField(null=True, blank=True)
    tipo = models.CharField("tipo de solicitud", max_length=20, choices=Tipo.choices, default=Tipo.SOPORTE)
    area = models.CharField("área a cargo", max_length=10, choices=Area.choices, default=Area.TECNICA, db_index=True)
    categoria = models.ForeignKey(Categoria, on_delete=models.PROTECT, verbose_name="sistema a intervenir")
    falla = models.ForeignKey(
        "TipoFalla", on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets",
        verbose_name="falla o necesidad",
    )
    # Lo que escribió quien radica cuando no estaba en la lista (no se limita a las opciones)
    sistema_texto = models.CharField("sistema (escrito)", max_length=150, blank=True)
    falla_texto = models.CharField("falla o necesidad (escrita)", max_length=200, blank=True)
    asunto = models.CharField(max_length=200)
    descripcion = models.TextField("descripción")
    impacto = models.CharField(
        "nivel de urgencia", max_length=10, choices=Impacto.choices, blank=True,
        help_text="Lo define el coordinador técnico; sin él el ticket no se recepciona.",
    )
    desde_cuando = models.DateField("¿desde cuándo ocurre?", null=True, blank=True)
    contacto_nombre = models.CharField("persona de contacto", max_length=150, blank=True)
    contacto_telefono = models.CharField("teléfono de contacto", max_length=50, blank=True)
    # Uno o varios correos separados por «;» (a todos se les envían las respuestas)
    contacto_email = models.CharField("correos para envío de respuestas", max_length=500, blank=True)
    # Cuándo el cliente NO puede recibir llamadas ni visitas: {"viernes": "tarde", ...}.
    # El cliente no agenda; esto lo consulta el área administrativa al programar.
    jornada = models.CharField("jornada para la visita", max_length=10, choices=Jornada.choices, blank=True)
    # Cuántas veces el coordinador aplazó la visita ya programada (el detalle está en Aplazamiento)
    veces_aplazado = models.PositiveIntegerField("veces aplazado", default=0)
    no_disponible = models.JSONField("días en que no puede atender", default=dict, blank=True)
    restricciones = models.CharField("otras restricciones de atención", max_length=250, blank=True)
    # La agenda la define el área administrativa (el cliente solo indica cuándo NO puede)
    visita_programada = models.DateTimeField("visita técnica programada", null=True, blank=True)
    prioridad = models.CharField(max_length=10, choices=Prioridad.choices, default=Prioridad.MEDIA)
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.GENERADO)
    asignado_a = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets_asignados",
        limit_choices_to=models.Q(rol__in=["agente", "supervisor"]) | models.Q(tambien_tecnico=True),
        verbose_name="técnico encargado",
    )
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)
    fecha_resolucion = models.DateTimeField(null=True, blank=True)
    fecha_cierre = models.DateTimeField(null=True, blank=True)
    ESCALA_CALIFICACION = 10
    calificacion = models.PositiveSmallIntegerField(
        "calificación (1 a 10)", null=True, blank=True, choices=[(i, str(i)) for i in range(1, ESCALA_CALIFICACION + 1)]
    )
    comentario_calificacion = models.TextField(blank=True)
    fecha_calificacion = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-creado"]
        indexes = [
            models.Index(fields=["estado"]),
            models.Index(fields=["empresa", "estado"]),
            models.Index(fields=["asignado_a", "estado"]),
        ]

    def __str__(self):
        return f"{self.radicado} · {self.asunto}"

    def save(self, *args, **kwargs):
        """Asigna el radicado (JNT-AAAA-NNNNNN) al crear y valida que la sede sea de la misma empresa."""
        if not self.radicado:
            anio, numero = ContadorRadicado.siguiente()
            self.radicado = f"{settings.RADICADO_PREFIJO}-{anio}-{numero:06d}"
        if self.sede_id and self.empresa_id and self.sede.empresa_id != self.empresa_id:
            raise ValidationError("La sede no pertenece a la empresa del ticket.")
        super().save(*args, **kwargs)

    def asignar_prioridad_inicial(self):
        """Prioridad de arranque según la urgencia reportada."""
        if self.impacto:
            self.prioridad = self.PRIORIDAD_POR_IMPACTO[self.impacto]
        else:
            self.prioridad = self.PRIORIDAD_POR_TIPO.get(self.tipo, self.Prioridad.MEDIA)

    def get_absolute_url(self):
        """Ficha del ticket en la plataforma."""
        return reverse("ticket_detalle", args=[self.radicado])

    @property
    def correos_respuesta(self):
        """Los correos (uno o varios) a los que se envían las respuestas de la solicitud."""
        return [c.strip() for c in self.contacto_email.replace(",", ";").split(";") if c.strip()]

    POR_CONFIRMAR = {"sistema": "Sistema a intervenir", "alturas": "Trabajo en alturas", "tipo": "Tipo de ticket"}

    @property
    def por_confirmar_display(self):
        """Los datos que el cliente marcó como «por confirmar», en palabras."""
        return [self.POR_CONFIRMAR.get(c, c) for c in (self.por_confirmar or [])]

    @property
    def sistema_display(self):
        """El sistema a intervenir: el escrito a mano o, si no, la categoría elegida."""
        return self.sistema_texto or (str(self.categoria) if self.categoria_id else "")

    @property
    def falla_display(self):
        """La falla elegida de la lista o, si no, la que escribió el cliente."""
        return str(self.falla) if self.falla_id else self.falla_texto

    @property
    def enlace_mapa(self):
        """Google Maps con las coordenadas o, si no hay, con la dirección escrita."""
        from urllib.parse import quote

        if self.latitud is not None and self.longitud is not None:
            return f"https://www.google.com/maps/search/?api=1&query={self.latitud},{self.longitud}"
        if self.direccion:
            return "https://www.google.com/maps/search/?api=1&query=" + quote(", ".join(
                x for x in (self.direccion, self.municipio, self.departamento, "Colombia") if x))
        return ""

    @property
    def ubicacion(self):
        """Dirección, municipio y departamento en una sola línea."""
        return " · ".join(x for x in (self.direccion, self.municipio, self.departamento) if x)

    # --- Cierre automático tras la visita ejecutada ----------------------------------
    @property
    def fecha_cierre_automatico(self):
        """Si el cliente no confirma ni reabre, el ticket con visita ejecutada se cierra solo en esta fecha."""
        if self.estado != self.Estado.VISITA_EJECUTADA or not self.fecha_resolucion:
            return None
        return self.fecha_resolucion + timedelta(days=settings.TICKET_DIAS_CIERRE_AUTOMATICO)

    @classmethod
    def cerrar_resueltos_vencidos(cls):
        """Cierra las visitas ejecutadas sin respuesta del cliente tras el plazo. Devuelve los tickets cerrados."""
        dias = settings.TICKET_DIAS_CIERRE_AUTOMATICO
        limite = timezone.now() - timedelta(days=dias)
        cerrados = []
        for ticket in cls.objects.filter(estado=cls.Estado.VISITA_EJECUTADA, fecha_resolucion__lte=limite):
            seg = ticket.cambiar_estado(
                cls.Estado.CERRADO, None,
                f"Cerrado automáticamente: pasaron {dias} días desde la visita técnica "
                "sin que el cliente reportara novedades. Si el problema vuelve a presentarse, radique una nueva solicitud.",
            )
            cerrados.append((ticket, seg))
        return cerrados

    @property
    def semana_atencion(self):
        """Los 7 días con lo que el cliente indicó: [(día, 'manana'|'tarde'|'todo'|'horas'|'', texto)]."""
        datos = self.no_disponible or {}
        return [(etiqueta, leer_franja(datos.get(clave))[0], texto_franja(datos.get(clave))) for clave, etiqueta in DIAS_LABORALES]

    @property
    def tiene_restricciones(self):
        """Si el cliente indicó días, horas o fechas en que no puede atender la visita."""
        return bool(self.no_disponible) or bool(self.restricciones) or self.rangos_no_disponible.exists()

    @property
    def resumen_no_disponible(self):
        """Texto corto: «Viernes tarde · Domingo todo el día · 24/10 8:00 a. m.–12:00 m.»."""
        partes = [f"{dia} {texto}" for dia, _, texto in self.semana_atencion if texto]
        if self.pk:
            partes += [str(r) for r in self.rangos_no_disponible.all()]
        return " · ".join(partes)

    def choque_con_restricciones(self, momento, fin=None):
        """Si la visita (de `momento` a `fin`) cae cuando el cliente dijo que NO puede atender, lo explica."""
        if not momento:
            return ""
        fin = fin or momento
        local, local_fin = timezone.localtime(momento), timezone.localtime(fin)
        horario = fuera_de_horario(momento, fin)
        if horario:
            return horario
        if self.jornada == self.Jornada.MANANA and (local.hour >= 12 or (local_fin.hour, local_fin.minute) > (12, 0)):
            return "El cliente pidió la visita en la jornada de la mañana."
        if self.jornada == self.Jornada.TARDE and local.hour < 12:
            return "El cliente pidió la visita en la jornada de la tarde."
        for rango in (self.rangos_no_disponible.all() if self.pk else []):
            if momento < rango.hasta and fin > rango.desde or rango.desde <= momento < rango.hasta:
                return f"El cliente indicó que no puede atender del {rango}."
        clave, dia = DIAS_SEMANA[local.weekday()]
        franja, desde, hasta = leer_franja((self.no_disponible or {}).get(clave))
        if franja == "horas":
            inicio, termina = local.strftime("%H:%M"), local_fin.strftime("%H:%M")
            if inicio < hasta and (termina > desde or inicio >= desde):
                return f"El cliente indicó que el {dia.lower()} no puede atender de {desde} a {hasta}."
            return ""
        if franja == "todo":
            return f"El cliente indicó que el {dia.lower()} no puede atender en todo el día."
        if franja == "manana" and local.hour < 12:
            return f"El cliente indicó que el {dia.lower()} no puede atender en la mañana (antes de 12 m)."
        if franja == "tarde" and (local.hour >= 12 or (local_fin.hour, local_fin.minute) > (12, 0)):
            return f"El cliente indicó que el {dia.lower()} no puede atender en la tarde (después de 12 m)."
        return ""

    # --- Ayudas de presentación ---------------------------------------------
    @property
    def abierto(self):
        """Si el ticket sigue en curso (no está cerrado ni cancelado)."""
        return self.estado not in self.ESTADOS_FINALES

    @property
    def paso_actual(self):
        """Índice del paso en la barra de progreso del cliente."""
        orden = [p[0] for p in self.PASOS_CLIENTE]
        if self.estado == self.Estado.CANCELADO:
            return -1
        if self.estado == self.Estado.PENDIENTE_VISITA:  # vuelve a esperar un espacio en el cronograma
            return orden.index(self.Estado.RECEPCIONADO)
        if self.estado in (self.Estado.EN_PROCESO, self.Estado.PROPUESTA_ENVIADA, self.Estado.EN_INVENTARIO):
            return 0
        if self.estado == self.Estado.INFORME:
            return orden.index(self.Estado.VISITA_EJECUTADA)
        return orden.index(self.estado)

    @property
    def color_estado(self):
        """Color de Bootstrap con que se muestra el estado en las bandejas."""
        return {
            "generado": "secondary",
            "recepcionado": "info",
            "en_cronograma": "primary",
            "autorizado": "primary",
            "informe": "info",
            "visita_ejecutada": "success",
            "pendiente_visita": "warning",
            "en_proceso": "primary",
            "propuesta_enviada": "info",
            "en_inventario": "warning",
            "cerrado": "dark",
            "cancelado": "danger",
        }[self.estado]

    @property
    def icono_tipo(self):
        """Icono de Bootstrap Icons que identifica el tipo de ticket."""
        return {
            "mantenimiento": "bi-wrench-adjustable",
            "soporte": "bi-tools",
            "requerimiento": "bi-clipboard-plus",
            "instalacion": "bi-hdd-network",
            "cotizacion": "bi-file-earmark-text",
        }[self.tipo]

    @property
    def color_prioridad(self):
        # Colores de Bootstrap equivalentes (los badges usan .prioridad-<valor> en janotec.css)
        """Color de Bootstrap equivalente a la prioridad."""
        return {"baja": "success", "media": "warning", "alta": "danger", "critica": "danger"}[self.prioridad]

    # --- Transiciones --------------------------------------------------------
    def reingresar(self, autor, mensaje):
        """Vuelve al área técnica ya recepcionado (visita aplazada o traspaso desde comercial o bodega): queda por
        programar en el cronograma. Es interno: el cliente ya recibió el correo «en proceso» y no se le repite."""
        return self.cambiar_estado(self.Estado.RECEPCIONADO, autor, mensaje, interno=True)

    def cambiar_estado(self, nuevo, autor, mensaje="", interno=False):
        """Cambia el estado y deja constancia en el historial."""
        if nuevo == self.estado:
            return None
        anterior = self.estado
        self.estado = nuevo
        ahora = timezone.now()
        if nuevo == self.Estado.VISITA_EJECUTADA:
            self.fecha_resolucion = ahora
        if nuevo in self.ESTADOS_FINALES:
            self.fecha_cierre = ahora
        elif anterior in self.ESTADOS_FINALES or anterior == self.Estado.VISITA_EJECUTADA:
            # Reapertura
            self.fecha_cierre = None
            if nuevo != self.Estado.VISITA_EJECUTADA:
                self.fecha_resolucion = None
        self.save()
        return Seguimiento.objects.create(
            ticket=self,
            autor=autor,
            tipo=Seguimiento.Tipo.CAMBIO_ESTADO,
            estado_anterior=anterior,
            estado_nuevo=nuevo,
            mensaje=mensaje,
            interno=interno,
        )


class Aplazamiento(models.Model):
    """La visita programada se aplazó: el ticket vuelve a «Por programar» y queda el porqué (trazabilidad)."""

    class Motivo(models.TextChoices):
        """Por qué se aplazó la visita; alimenta los indicadores de aplazamientos."""
        CLIENTE = "cliente", "Lo solicitó el cliente"
        TECNICO = "tecnico", "Inconveniente técnico"
        PERSONAL = "personal", "Disponibilidad del personal técnico"
        MATERIAL = "material", "Falta de material o equipos"
        SST = "sst", "Sin acompañamiento SST / permisos de ingreso"
        CLIMA = "clima", "Clima o acceso al sitio"
        OTRO = "otro", "Otro"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="aplazamientos")
    numero = models.PositiveIntegerField("aplazamiento n.º")
    motivo = models.CharField("motivo", max_length=20, choices=Motivo.choices)
    detalle = models.TextField("explicación del aplazamiento")
    visita_aplazada = models.DateTimeField("visita que estaba programada", null=True, blank=True)
    programacion = models.CharField("lo que estaba programado", max_length=500, blank=True)
    informado_cliente = models.BooleanField("se le informó al cliente", default=False)
    autor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-creado", "-id"]
        verbose_name = "aplazamiento"
        verbose_name_plural = "aplazamientos"

    def __str__(self):
        return f"{self.ticket.radicado} · aplazamiento {self.numero} · {self.get_motivo_display()}"


class EtapaCliente(models.Model):
    """Fecha estimada y nota visible al cliente de cada etapa del servicio (como en janotecsas.netlify.app)."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="etapas_cliente")
    clave = models.CharField(max_length=20)
    fecha = models.DateField("fecha estimada", null=True, blank=True)
    nota = models.CharField("nota visible al cliente", max_length=300, blank=True)

    class Meta:
        unique_together = [("ticket", "clave")]
        verbose_name = "etapa visible al cliente"
        verbose_name_plural = "etapas visibles al cliente"

    def __str__(self):
        return f"{self.ticket.radicado} · {self.clave}"


class RangoNoDisponible(models.Model):
    """Fechas y horas puntuales en las que el cliente NO puede recibir la atención (ej. 24/10 de 8 a 12)."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="rangos_no_disponible")
    desde = models.DateTimeField()
    hasta = models.DateTimeField()

    class Meta:
        ordering = ["desde"]
        verbose_name = "rango sin disponibilidad"
        verbose_name_plural = "rangos sin disponibilidad"

    def __str__(self):
        d, h = timezone.localtime(self.desde), timezone.localtime(self.hasta)
        if d.date() == h.date():
            return f"{d:%d/%m/%Y} de {d:%H:%M} a {h:%H:%M}"
        return f"{d:%d/%m/%Y %H:%M} al {h:%d/%m/%Y %H:%M}"


class Seguimiento(models.Model):
    """Cada movimiento del ticket: respuestas, notas internas, cambios."""

    class Tipo(models.TextChoices):
        """Clase de registro en el historial del ticket."""
        CREACION = "creacion", "Creación"
        COMENTARIO = "comentario", "Comentario"
        NOTA_INTERNA = "nota_interna", "Nota interna"
        CAMBIO_ESTADO = "cambio_estado", "Cambio de estado"
        ASIGNACION = "asignacion", "Asignación"
        PRIORIDAD = "prioridad", "Cambio de prioridad"
        CALIFICACION = "calificacion", "Calificación"
        APLAZAMIENTO = "aplazamiento", "Aplazamiento de la visita"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="seguimientos")
    autor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    mensaje = models.TextField(blank=True)
    estado_anterior = models.CharField(max_length=20, blank=True)
    estado_nuevo = models.CharField(max_length=20, blank=True)
    interno = models.BooleanField(
        default=False, help_text="Las notas internas nunca se muestran al cliente."
    )
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado", "id"]

    def __str__(self):
        return f"{self.ticket.radicado} · {self.get_tipo_display()}"

    @property
    def estado_anterior_display(self):
        """El estado anterior en palabras (también para estados que ya no existen)."""
        return dict(Ticket.Estado.choices).get(self.estado_anterior, self.estado_anterior)

    @property
    def estado_nuevo_display(self):
        """El estado nuevo en palabras (también para estados que ya no existen)."""
        return dict(Ticket.Estado.choices).get(self.estado_nuevo, self.estado_nuevo)


def ruta_adjunto(instancia, nombre):
    """Ruta del adjunto en media/: carpeta por año y mes y nombre aleatorio (el original se guarda aparte)."""
    ext = os.path.splitext(nombre)[1].lower()
    hoy = timezone.localdate()
    return f"adjuntos/{hoy:%Y/%m}/{uuid.uuid4().hex}{ext}"


class Adjunto(models.Model):
    """Archivo adjunto al ticket o a una respuesta del historial; si la respuesta es interna, el cliente no lo ve."""
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="adjuntos")
    seguimiento = models.ForeignKey(
        Seguimiento, on_delete=models.CASCADE, null=True, blank=True, related_name="adjuntos"
    )
    archivo = models.FileField(upload_to=ruta_adjunto)
    nombre_original = models.CharField(max_length=255)
    es_propuesta = models.BooleanField("propuesta económica", default=False)
    es_informe = models.BooleanField("informe técnico", default=False)
    tamano = models.PositiveIntegerField("tamaño (bytes)", default=0)
    subido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado"]

    def __str__(self):
        return self.nombre_original

    @property
    def interno(self):
        """Si el adjunto va en una nota interna (no lo ve el cliente)."""
        return bool(self.seguimiento and self.seguimiento.interno)

    @property
    def es_imagen(self):
        """Si el archivo es una imagen que se puede mostrar en miniatura."""
        return self.nombre_original.lower().rsplit(".", 1)[-1] in ("jpg", "jpeg", "png", "webp", "gif")


# --- Acta de soporte: visita, equipos, fotos, evidencias y firmas ---------------------------------
class ActaSoporte(models.Model):
    """Acta de servicio técnico realizada en campo (historial de cada sede).

    Dos orígenes:
    - SafetyTrack: históricas, cargadas con ``python manage.py importar_maestros``.
    - Plataforma: el técnico la diligencia desde el ticket que atiende (numeradas desde 100001
      para no chocar con los números de SafetyTrack).
    """

    class Origen(models.TextChoices):
        """De dónde viene el acta: la plataforma o el histórico importado de SafetyTrack."""
        SAFETYTRACK = "safetytrack", "SafetyTrack (histórica)"
        PLATAFORMA = "plataforma", "Plataforma de tickets"

    # Mismas opciones del formato «Acta de Soporte, Instalación, Desinstalación V1»
    ACTIVIDADES = ["Soporte", "Instalación", "Desinstalación"]
    SISTEMAS = [("CCTV", "CCTV"), ("ALARMA", "Alarma"), ("EQUIPOS INFORMATICOS", "Equipos informáticos"),
                ("CONTROL DE ACCESO", "Control de acceso"), ("REDES", "Fibra óptica / red"),
                ("ENERGIA SOLAR", "Energía fotovoltaica"), ("RADIO", "Radiocomunicaciones"), ("GPS", "GPS")]
    PRIMER_NUMERO_PLATAFORMA = 100001

    class Resultado(models.TextChoices):
        """Cómo quedó el servicio al terminar la visita."""
        SOLUCIONADO = "solucionado", "Solucionado"
        PARCIAL = "parcial", "Solucionado parcialmente"
        RECOMENDACIONES = "recomendaciones", "Recomendaciones"

    class Modalidad(models.TextChoices):
        """Si la visita fue en el sitio o remota."""
        PRESENCIAL = "presencial", "Presencial"
        VIRTUAL = "virtual", "Virtual"

    numero = models.PositiveIntegerField("número de acta", unique=True)
    fecha = models.DateTimeField()
    empresa = models.ForeignKey(Empresa, on_delete=models.PROTECT, related_name="actas")
    sede = models.ForeignKey(Sede, on_delete=models.SET_NULL, null=True, blank=True, related_name="actas")
    sede_texto = models.CharField("sede (como se escribió)", max_length=200, blank=True)
    ciudad = models.CharField(max_length=100, blank=True)
    tecnico = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="actas", verbose_name="técnico",
    )
    tecnico_texto = models.CharField("técnico (como se escribió)", max_length=150, blank=True)
    actividad = models.CharField(max_length=100, blank=True, help_text="Soporte, Instalación, Desinstalación…")
    sistema = models.CharField(max_length=60, blank=True, help_text="CCTV, Alarma, Equipos informáticos…")
    # Vacía en las actas anteriores: no se registraba y no se puede suponer
    modalidad = models.CharField("modalidad de atención", max_length=12, choices=Modalidad.choices, blank=True)
    diagnostico = models.TextField("diagnóstico", blank=True)
    resultado = models.CharField(max_length=20, choices=Resultado.choices, blank=True)
    observaciones = models.TextField(blank=True)
    recibio = models.CharField("recibió (cliente)", max_length=150, blank=True)
    cargo_recibio = models.CharField("cargo de quien recibió", max_length=150, blank=True)
    latitud = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    longitud = models.DecimalField(max_digits=10, decimal_places=7, null=True, blank=True)
    enlace_imagenes = models.URLField("enlace a imágenes", max_length=300, blank=True)
    pdf_original = models.FileField("acta original (PDF firmado)", upload_to="actas/originales/", blank=True,
                                    help_text="El PDF del acta en papel o de SafetyTrack, cargado desde la carpeta de clientes.")
    ticket = models.ForeignKey(
        Ticket, on_delete=models.SET_NULL, null=True, blank=True, related_name="actas",
        help_text="Ticket que originó la visita, si existe.",
    )
    origen = models.CharField(max_length=20, choices=Origen.choices, default=Origen.SAFETYTRACK)
    copia_correo = models.CharField("copia correo a", max_length=500, blank=True)

    class Meta:
        ordering = ["-fecha"]
        verbose_name = "acta de soporte"
        verbose_name_plural = "actas de soporte"
        indexes = [models.Index(fields=["empresa", "-fecha"]), models.Index(fields=["sede", "-fecha"])]

    def __str__(self):
        return f"Acta {self.numero} · {self.empresa}"

    @classmethod
    def siguiente_numero(cls):
        """Consecutivo de las actas hechas en la plataforma (bloquea la fila para evitar duplicados)."""
        with transaction.atomic():
            contador, _ = ContadorActa.objects.select_for_update().get_or_create(
                pk=1, defaults={"ultimo": cls.PRIMER_NUMERO_PLATAFORMA - 1}
            )
            contador.ultimo += 1
            contador.save(update_fields=["ultimo"])
            return contador.ultimo

    def get_sistema_display_corto(self):
        """El sistema intervenido en palabras cortas."""
        return dict(self.SISTEMAS).get(self.sistema, self.sistema.title())

    @property
    def es_virtual(self):
        """Si el soporte se hizo de forma remota."""
        return self.modalidad == self.Modalidad.VIRTUAL

    @property
    def firma_tecnico(self):
        """La firma del técnico que diligenció el acta."""
        return self.firmas.filter(rol=FirmaActa.Rol.TECNICO).first()

    @property
    def firma_cliente(self):
        """La firma de quien recibió el servicio por parte del cliente."""
        return self.firmas.filter(rol=FirmaActa.Rol.CLIENTE).first()

    def get_absolute_url(self):
        """Detalle del acta en la plataforma."""
        return reverse("acta_detalle", args=[self.numero])

    @property
    def color_resultado(self):
        """Color de Bootstrap con que se muestra el resultado."""
        return {"solucionado": "success", "parcial": "warning", "recomendaciones": "info"}.get(self.resultado, "secondary")

    @property
    def lugar(self):
        """Dónde fue la visita: la sede, o el texto de la sede o la ciudad en actas históricas."""
        return str(self.sede) if self.sede else (self.sede_texto or self.ciudad)

    @property
    def equipos_intervenidos(self):
        """Equipos que se revisaron, repararon o configuraron."""
        return [e for e in self.equipos.all() if e.clase == EquipoActa.Clase.INTERVENIDO]

    @property
    def equipos_utilizados(self):
        """Material o equipos que se instalaron o gastaron para dejar el servicio funcionando."""
        return [e for e in self.equipos.all() if e.clase == EquipoActa.Clase.UTILIZADO]

    @property
    def equipos_retirados(self):
        """Equipos que se retiraron (desmonte)."""
        return [e for e in self.equipos.all() if e.clase == EquipoActa.Clase.RETIRADO]


class EquipoActa(models.Model):
    """Equipo o material de un acta: lo que se intervino o lo que se usó para dejar el servicio funcionando."""

    class Clase(models.TextChoices):
        """En qué lista del acta va el equipo: intervenido, utilizado o retirado."""
        INTERVENIDO = "intervenido", "Equipo intervenido"  # del cliente: se revisó, reparó, configuró o retiró
        UTILIZADO = "utilizado", "Material o equipo utilizado"  # se instaló o gastó para que el servicio funcione
        RETIRADO = "retirado", "Equipo retirado"  # desmonte: se entrega al cliente o vuelve a bodega si es de JANOTEC

    class Trabajo(models.TextChoices):
        """Qué se le hizo a un equipo intervenido."""
        REVISADO = "revisado", "Revisado"
        REPARADO = "reparado", "Reparado"
        CONFIGURADO = "configurado", "Configurado"

    class Propiedad(models.TextChoices):
        """De quién es el equipo. Define el seguimiento: lo de JANOTEC queda en la hoja de vida del activo
        y, si se retira, vuelve a bodega; lo del cliente se le entrega.
        """
        CLIENTE = "cliente", "Del cliente"
        JANOTEC = "janotec", "De JANOTEC"

    acta = models.ForeignKey(ActaSoporte, on_delete=models.CASCADE, related_name="equipos")
    propiedad = models.CharField("¿de quién es?", max_length=10, choices=Propiedad.choices, blank=True)
    placa = models.CharField("placa de inventario", max_length=40, blank=True,
                             help_text="Equipos de JANOTEC: la placa con la que está en el inventario.")
    clase = models.CharField(max_length=12, choices=Clase.choices, default=Clase.INTERVENIDO)
    trabajo = models.CharField("¿qué se hizo?", max_length=12, choices=Trabajo.choices, blank=True)  # vacío en las actas anteriores
    equipo = models.CharField(max_length=150, blank=True)
    marca = models.CharField(max_length=100, blank=True)
    referencia = models.CharField("referencia o modelo", max_length=100, blank=True)
    serial = models.CharField(max_length=100, blank=True)
    cantidad = models.CharField(max_length=20, blank=True)
    observaciones = models.TextField(blank=True)

    class Meta:
        verbose_name = "equipo del acta"
        verbose_name_plural = "equipos del acta"

    def __str__(self):
        return self.equipo or self.marca or "Equipo"


class ContadorActa(models.Model):
    """Último número de acta usado por la plataforma."""

    ultimo = models.PositiveIntegerField(default=ActaSoporte.PRIMER_NUMERO_PLATAFORMA - 1)

    class Meta:
        verbose_name = "contador de actas"
        verbose_name_plural = "contador de actas"


def ruta_acta(instancia, nombre):
    """Ruta de firmas, fotos y evidencias de actas en media/: carpeta por año y mes y nombre aleatorio."""
    ext = os.path.splitext(nombre)[1].lower() or ".png"
    hoy = timezone.localdate()
    return f"actas/{hoy:%Y/%m}/{uuid.uuid4().hex}{ext}"


class FotoEquipo(models.Model):
    """Foto de un equipo del acta (como «Foto del equipo» en el formato)."""

    equipo = models.ForeignKey(EquipoActa, on_delete=models.CASCADE, related_name="fotos")
    archivo = models.FileField(upload_to=ruta_acta)
    nombre_original = models.CharField(max_length=255, blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado"]
        verbose_name = "foto del equipo"
        verbose_name_plural = "fotos del equipo"


class EvidenciaActa(models.Model):
    """Fotos, videos o documentos del trabajo en sí: cómo se hizo y cómo quedó (no de un equipo en particular)."""

    class Etapa(models.TextChoices):
        """En qué momento se tomó la evidencia: durante el trabajo o del resultado final."""
        PROCESO = "proceso", "Durante el trabajo"
        RESULTADO = "resultado", "Resultado final"

    IMAGENES = (".jpg", ".jpeg", ".png", ".webp", ".heic")

    acta = models.ForeignKey(ActaSoporte, on_delete=models.CASCADE, related_name="evidencias")
    etapa = models.CharField(max_length=10, choices=Etapa.choices, default=Etapa.RESULTADO)
    archivo = models.FileField(upload_to=ruta_acta)
    nombre_original = models.CharField(max_length=255, blank=True)
    subido_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["etapa", "creado"]
        verbose_name = "evidencia del acta"
        verbose_name_plural = "evidencias del acta"

    @property
    def extension(self):
        """Extensión del archivo en minúsculas (de su nombre original)."""
        return os.path.splitext(self.nombre_original or self.archivo.name)[1].lower()

    @property
    def es_imagen(self):
        """Si la evidencia es una foto."""
        return self.extension in self.IMAGENES

    @property
    def es_video(self):
        """Si la evidencia es un video MP4."""
        return self.extension == ".mp4"


class FirmaActa(models.Model):
    """Firma de quien interviene en el acta: técnico, técnico encargado 2 y representante del cliente."""

    class Rol(models.TextChoices):
        """Quién firma el acta."""
        TECNICO = "tecnico", "Técnico"
        ACOMPANANTE = "acompanante", "Técnico encargado 2"
        CLIENTE = "cliente", "Representante del cliente"

    acta = models.ForeignKey(ActaSoporte, on_delete=models.CASCADE, related_name="firmas")
    rol = models.CharField(max_length=20, choices=Rol.choices)
    nombre = models.CharField("apellidos y nombres", max_length=150)
    cargo = models.CharField(max_length=150, blank=True)
    imagen = models.FileField(upload_to=ruta_acta)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["rol", "creado"]
        verbose_name = "firma del acta"
        verbose_name_plural = "firmas del acta"

    def __str__(self):
        return f"{self.get_rol_display()}: {self.nombre}"


# --- Informe al cliente: borrador por zonas con fotos ---------------------------------------------
def ruta_informe(instancia, nombre):
    """Ruta del PDF del informe en media/: carpeta por año y mes y nombre aleatorio."""
    return f"informes/{timezone.localdate():%Y/%m}/{uuid.uuid4().hex}.pdf"


class InformeBorrador(models.Model):
    """Informe técnico armado por la plataforma a partir de las actas del ticket.

    Queda en borrador (se puede corregir) hasta que coordinación lo aprueba: ahí se pasa a limpio
    (PDF definitivo) y es el informe que se entrega al cliente o a comercial.
    """

    class Estado(models.TextChoices):
        """Si el informe sigue en borrador o ya se pasó a limpio."""
        BORRADOR = "borrador", "Borrador"
        APROBADO = "aprobado", "Aprobado (pasado a limpio)"

    class Tipo(models.TextChoices):
        """Informe de una visita, o de estudio (levantamiento para preparar la propuesta)."""
        VISITA = "visita", "Informe de visita (soporte, instalación o desmonte)"
        ESTUDIO = "estudio", "Informe de estudio (levantamiento para la propuesta)"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="informes_borrador")
    tipo = models.CharField(max_length=10, choices=Tipo.choices, default=Tipo.VISITA)
    estado = models.CharField(max_length=10, choices=Estado.choices, default=Estado.BORRADOR)
    destinatario = models.CharField("señores", max_length=250)
    asunto = models.CharField(max_length=250)
    introduccion = models.TextField("introducción")
    actividades = models.TextField("actividades realizadas", help_text="Una por línea.")
    justificacion = models.TextField("justificación", blank=True)
    conclusiones = models.TextField(help_text="Una por línea.")
    recomendaciones = models.TextField(blank=True, help_text="Una por línea. Opcional.")
    # Solo en los informes de estudio: lo que se va a cotizar (pasa solo a comercial)
    suministros = models.TextField("equipos y materiales a suministrar", blank=True,
                                   help_text="Uno por línea: cantidad, descripción y, si quiere, la marca después de «|». "
                                             "Ej.: 3 Cámara IP 4 MP bala DS-2CD1043G0E-I | Hikvision")
    servicios = models.TextField("servicios a cotizar", blank=True, help_text="Uno por línea. Ej.: 1 Instalación y configuración")
    garantia = models.TextField("garantía y vida útil", blank=True)
    creado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)
    aprobado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    aprobado_en = models.DateTimeField(null=True, blank=True)
    archivo = models.FileField("PDF pasado a limpio", upload_to=ruta_informe, blank=True)

    class Meta:
        ordering = ["-creado"]
        verbose_name = "borrador de informe"
        verbose_name_plural = "borradores de informe"

    def __str__(self):
        return f"Informe {self.ticket.radicado} ({self.get_estado_display()})"

    @property
    def aprobado(self):
        """Si el informe ya se aprobó y pasó a limpio."""
        return self.estado == self.Estado.APROBADO

    @staticmethod
    def lineas(texto):
        """Las líneas no vacías de un texto, sin viñetas ni guiones al inicio."""
        return [x.strip(" •-\t") for x in (texto or "").splitlines() if x.strip(" •-\t")]

    @property
    def es_estudio(self):
        """Si es un informe de estudio (levantamiento)."""
        return self.tipo == self.Tipo.ESTUDIO

    def filas_suministros(self):
        """[(cantidad, descripción, marca)] de «3 m Cable UTP | Hikvision» → ("3 m", "Cable UTP", "Hikvision")."""
        filas = []
        for linea in self.lineas(self.suministros):
            texto, _, marca = linea.partition("|")
            m = re.match(r"^(\d+(?:[.,]\d+)?)\s*(m|mt|mts|mtrs|metros|und|un|rollo|rollos)?\.?\s+(.*)$", texto.strip(), re.I)
            cantidad, descripcion = ((m.group(1) + (f" {m.group(2)}" if m.group(2) else "")), m.group(3)) if m else ("1", texto.strip())
            filas.append((cantidad, descripcion.strip(), marca.strip()))
        return filas

    def materiales_para_comercial(self):
        """Los suministros en el formato de «Materiales y equipos a suministrar» (uno por línea, con cantidad)."""
        return "\n".join(f"{c} {d}" + (f" {m}" if m and m.lower() not in d.lower() else "") for c, d, m in self.filas_suministros())


class ZonaInforme(models.Model):
    """Zona o punto del estudio (Parqueadero, Báscula…): fotos de lo que verá la cámara y qué se hará ahí."""

    borrador = models.ForeignKey(InformeBorrador, on_delete=models.CASCADE, related_name="zonas")
    orden = models.PositiveSmallIntegerField(default=0)
    nombre = models.CharField("zona o punto", max_length=150)
    descripcion = models.TextField("qué se hará", blank=True)

    class Meta:
        ordering = ["orden", "pk"]
        verbose_name = "zona del estudio"
        verbose_name_plural = "zonas del estudio"

    def __str__(self):
        return self.nombre


class FotoZona(models.Model):
    """Foto de una zona del informe."""
    zona = models.ForeignKey(ZonaInforme, on_delete=models.CASCADE, related_name="fotos")
    archivo = models.FileField(upload_to=ruta_acta)
    nombre_original = models.CharField(max_length=255, blank=True)
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]


# --- Oportunidades comerciales y hallazgos detectados en campo ------------------------------------
class OportunidadComercial(models.Model):
    """Necesidad u oportunidad de venta que el técnico detecta en la visita («una venta jala otra»).

    Es interna: el cliente no la ve. La sigue el área administrativa / comercial.
    """

    # Mismos servicios de la «Ficha de requerimiento comercial»
    SERVICIOS = [
        ("cctv", "CCTV"), ("control_acceso", "Control de acceso"), ("alarma", "Alarma"), ("redes", "Redes / fibra"),
        ("radio", "Radio"), ("energia_solar", "Energía solar"), ("ia", "IA JANOTEC"), ("vigilante_virtual", "Vigilante virtual"),
        ("cobro_parqueadero", "Cobro de parqueadero"),
        ("alarma_comunitaria", "Alarma comunitaria"), ("ciberseguridad", "Ciberseguridad"),
        ("mto_computo", "Mantenimiento de cómputo"), ("proyecto_gob", "Proyecto gobierno"),
    ]

    class Estado(models.TextChoices):
        """Avance de la oportunidad en el área comercial."""
        NUEVA = "nueva", "Nueva"
        CONTACTADO = "contactado", "Contactado"
        COTIZADO = "cotizado", "Cotizado"
        GANADA = "ganada", "Ganada"
        DESCARTADA = "descartada", "Descartada"

    acta = models.OneToOneField(ActaSoporte, on_delete=models.CASCADE, null=True, blank=True, related_name="oportunidad")
    empresa = models.ForeignKey(Empresa, on_delete=models.CASCADE, related_name="oportunidades")
    sede = models.ForeignKey(Sede, on_delete=models.SET_NULL, null=True, blank=True, related_name="oportunidades")
    servicios = models.JSONField(default=list, blank=True)
    nota = models.TextField("qué vio o qué pidió el cliente", blank=True)
    detectada_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                      related_name="oportunidades_detectadas")
    estado = models.CharField(max_length=20, choices=Estado.choices, default=Estado.NUEVA)
    seguimiento = models.TextField("notas de seguimiento comercial", blank=True)
    creado = models.DateTimeField(auto_now_add=True)
    actualizado = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-creado", "-id"]
        verbose_name = "oportunidad comercial"
        verbose_name_plural = "oportunidades comerciales"

    def __str__(self):
        return f"Oportunidad · {self.empresa} · {', '.join(self.servicios_display)}"

    @property
    def servicios_display(self):
        """Los servicios de la oportunidad, en palabras."""
        nombres = dict(self.SERVICIOS)
        return [nombres.get(s, s) for s in self.servicios]

    @property
    def color_estado(self):
        """Color de Bootstrap con que se muestra el estado."""
        return {"nueva": "primary", "contactado": "info", "cotizado": "warning", "ganada": "success",
                "descartada": "secondary"}[self.estado]


class Hallazgo(models.Model):
    """Algo que la plataforma detectó sola en el uso real y que no sigue la lógica del proceso (ver diagnostico.py)."""

    class Gravedad(models.TextChoices):
        """Qué tan grave es el hallazgo encontrado en la visita."""
        ALTA = "alta", "Alta"
        MEDIA = "media", "Media"
        BAJA = "baja", "Baja"

    regla = models.CharField(max_length=40, db_index=True)
    clave = models.CharField("caso", max_length=60)
    titulo = models.CharField(max_length=250)
    detalle = models.TextField(blank=True)
    url = models.CharField(max_length=300, blank=True)
    ticket = models.ForeignKey(Ticket, on_delete=models.SET_NULL, null=True, blank=True, related_name="hallazgos")
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
                                help_text="Quién estaba involucrado (para saber qué capacitar o mejorar).")
    gravedad = models.CharField(max_length=10, choices=Gravedad.choices, default=Gravedad.MEDIA)
    area = models.CharField(max_length=40, blank=True)
    creado = models.DateTimeField("detectado", auto_now_add=True)
    visto = models.DateTimeField("última vez que se vio", default=timezone.now)
    resuelto = models.DateTimeField(null=True, blank=True, help_text="Se cierra solo cuando la situación desaparece.")
    avisado = models.BooleanField(default=False)
    # Revisado a mano: alguien lo miró y dejó el motivo (p. ej. «es así a propósito»). Sale de las listas sin estar corregido
    revisado_en = models.DateTimeField(null=True, blank=True)
    revisado_por = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    motivo_revision = models.CharField("por qué no requiere acción", max_length=300, blank=True)

    class Meta:
        ordering = ["-creado"]
        unique_together = [("regla", "clave")]
        verbose_name = "hallazgo de salud de los procesos"
        verbose_name_plural = "hallazgos de salud de los procesos"

    def __str__(self):
        return self.titulo


# --- Pasos del área técnica omitidos por coordinación ---------------------------------------------
class PasoSaltado(models.Model):
    """Coordinación omitió un paso del área técnica (p. ej. visita sin programar en el cronograma), con su motivo."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="pasos_saltados")
    paso = models.CharField(max_length=20)
    motivo = models.CharField(max_length=300)
    autor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    creado = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["creado"]
        verbose_name = "paso omitido"
        verbose_name_plural = "pasos omitidos"

    def __str__(self):
        return f"{self.ticket.radicado} · {self.paso}"
