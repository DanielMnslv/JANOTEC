from django.contrib import admin

from .models import Actividad, DocumentoPersonal


@admin.register(Actividad)
class ActividadAdmin(admin.ModelAdmin):
    list_display = ("fecha", "hora_inicio", "cliente", "sede", "tipo", "autorizacion", "ticket")
    list_filter = ("tipo", "autorizacion", "fecha")
    search_fields = ("cliente", "sede", "actividad", "ticket__radicado")
    filter_horizontal = ("encargados", "acompanantes")
    raw_id_fields = ("ticket",)


@admin.register(DocumentoPersonal)
class DocumentoPersonalAdmin(admin.ModelAdmin):
    list_display = ("usuario", "tipo", "vigente_hasta", "actual", "subido")
    list_filter = ("tipo", "actual")
    search_fields = ("usuario__first_name", "usuario__last_name", "usuario__username")
