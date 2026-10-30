from django.contrib import admin

from .models import AjusteMonitoreo, FichaComercial, InformeRequerimiento, PropuestaVersion, ServicioMonitoreo


class InformeInline(admin.StackedInline):
    model = InformeRequerimiento
    extra = 0
    readonly_fields = ("elaborado_por", "creado", "actualizado")


class VersionInline(admin.TabularInline):
    model = PropuestaVersion
    extra = 0
    fields = ("numero", "valor", "estado", "cambios", "motivo", "enviada", "respondida")
    readonly_fields = ("enviada", "respondida")


@admin.register(FichaComercial)
class FichaComercialAdmin(admin.ModelAdmin):
    list_display = ("ticket", "etapa", "origen", "comercial", "valor_propuesta", "creado")
    list_filter = ("etapa", "origen", "comercial")
    search_fields = ("ticket__radicado", "ticket__empresa__razon_social", "ticket__asunto", "referencia_externa")
    autocomplete_fields = ("ticket", "comercial", "oportunidad")
    readonly_fields = ("creado", "actualizado", "fecha_envio", "fecha_respuesta")
    inlines = [InformeInline, VersionInline]


class AjusteMonitoreoInline(admin.TabularInline):
    model = AjusteMonitoreo
    extra = 0
    readonly_fields = ("aplicado_por", "aplicado")


@admin.register(ServicioMonitoreo)
class ServicioMonitoreoAdmin(admin.ModelAdmin):
    list_display = ("sede", "valor_mensual", "cantidad", "anio_valor", "activo")
    list_filter = ("activo", "anio_valor", "sede__empresa")
    search_fields = ("sede__nombre", "sede__empresa__razon_social")
    inlines = [AjusteMonitoreoInline]
