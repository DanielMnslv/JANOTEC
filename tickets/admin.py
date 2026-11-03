"""Configuración (admin de Django) de los tickets: categorías y fallas, tickets, actas de soporte, oportunidades y contadores.

Las actas firmadas y sus equipos y firmas solo se consultan: un documento firmado no se altera.
"""

from django.contrib import admin

from .models import (
    ActaSoporte, Adjunto, Categoria, ContadorRadicado, EquipoActa, FirmaActa, OportunidadComercial, Seguimiento, Ticket,
    TipoFalla,
)


class TipoFallaInline(admin.TabularInline):
    """Fallas de la categoría, editables dentro de ella (nombre, orden y si se ofrece al radicar)."""
    model = TipoFalla
    extra = 1
    fields = ("nombre", "orden", "activa")


@admin.register(Categoria)
class CategoriaAdmin(admin.ModelAdmin):
    """Sistemas a intervenir (categorías) con sus fallas; el orden y la casilla «activa» se editan desde la lista."""
    list_display = ("nombre", "orden", "activa", "n_fallas")
    list_editable = ("orden", "activa")
    inlines = [TipoFallaInline]

    @admin.display(description="fallas en la lista")
    def n_fallas(self, obj):
        """Cuántas fallas activas ofrece la categoría al radicar."""
        return obj.fallas.filter(activa=True).count()


class SeguimientoInline(admin.TabularInline):
    """Historial del ticket (mensajes, cambios de estado y notas internas) dentro de su ficha."""
    model = Seguimiento
    extra = 0
    fields = ("creado", "autor", "tipo", "estado_nuevo", "interno", "mensaje")
    readonly_fields = ("creado",)


class AdjuntoInline(admin.TabularInline):
    """Archivos adjuntos del ticket."""
    model = Adjunto
    extra = 0
    fields = ("nombre_original", "archivo", "subido_por", "creado")
    readonly_fields = ("creado",)


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    """Tickets: búsqueda por radicado, asunto o cliente, con su historial y adjuntos."""
    list_display = ("radicado", "tipo", "asunto", "empresa", "estado", "prioridad", "asignado_a", "creado")
    list_filter = ("tipo", "estado", "prioridad", "categoria", "falla", "empresa", "asignado_a")
    search_fields = ("radicado", "asunto", "descripcion", "empresa__razon_social")
    date_hierarchy = "creado"
    readonly_fields = ("radicado", "creado", "actualizado", "fecha_resolucion", "fecha_cierre")
    autocomplete_fields = ("empresa", "solicitante", "sede")
    inlines = [SeguimientoInline, AdjuntoInline]


class EquipoActaInline(admin.TabularInline):
    """Los equipos son parte del acta firmada: se consultan, no se editan."""
    model = EquipoActa
    fields = ["clase", "equipo", "marca", "serial", "cantidad", "observaciones"]
    readonly_fields = fields
    extra = 0
    can_delete = False

    def has_add_permission(self, request, obj=None):
        """No se agregan equipos a un acta ya firmada."""
        return False


class FirmaActaInline(admin.TabularInline):
    """Las firmas no se editan: un acta firmada queda como se firmó."""
    model = FirmaActa
    extra = 0
    fields = ("rol", "nombre", "cargo", "imagen", "creado")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        """No se agregan firmas a un acta ya firmada."""
        return False


@admin.register(ActaSoporte)
class ActaSoporteAdmin(admin.ModelAdmin):
    """Actas de soporte en modo consulta: se buscan y se revisan, pero no se crean, editan ni borran aquí."""
    list_display = ("numero", "fecha", "empresa", "sede", "sistema", "resultado", "tecnico", "origen")
    list_filter = ("origen", "resultado", "sistema", "empresa", "tecnico")
    search_fields = ("numero", "diagnostico", "observaciones", "sede_texto", "empresa__razon_social")
    date_hierarchy = "fecha"
    autocomplete_fields = ("empresa", "sede", "tecnico", "ticket")
    inlines = [EquipoActaInline, FirmaActaInline]

    # Un acta firmada y guardada no se altera: en Configuración solo se consulta
    def has_add_permission(self, request):
        """Las actas se diligencian en la plataforma, no en Configuración."""
        return False

    def has_change_permission(self, request, obj=None):
        """Un acta firmada queda como se firmó."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Un acta firmada no se borra: es soporte del servicio."""
        return False


@admin.register(OportunidadComercial)
class OportunidadComercialAdmin(admin.ModelAdmin):
    """Oportunidades de venta detectadas por los técnicos en sus visitas."""
    list_display = ("empresa", "sede", "estado", "detectada_por", "creado")
    list_filter = ("estado", "empresa", "detectada_por")
    search_fields = ("empresa__razon_social", "nota", "seguimiento")
    autocomplete_fields = ("empresa", "sede", "acta")
    readonly_fields = ("creado", "actualizado")


@admin.register(ContadorRadicado)
class ContadorAdmin(admin.ModelAdmin):
    """Último consecutivo de radicado usado por año (JNT-AAAA-NNNNNN)."""
    list_display = ("anio", "ultimo")
