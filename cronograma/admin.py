from django.contrib import admin

from .models import Actividad, DocumentoPersonal, ReporteHorasExtra, SolicitudViaticos


@admin.register(Actividad)
class ActividadAdmin(admin.ModelAdmin):
    list_display = ("fecha", "hora_inicio", "cliente", "sede", "tipo", "autorizacion", "ticket")
    list_filter = ("tipo", "autorizacion", "fecha")
    search_fields = ("cliente", "sede", "actividad", "ticket__radicado")
    filter_horizontal = ("encargados",)
    raw_id_fields = ("ticket",)


@admin.register(ReporteHorasExtra)
class ReporteHorasExtraAdmin(admin.ModelAdmin):
    list_display = ("colaborador", "fecha", "rol", "hora_salida", "hora_llegada", "duracion", "cliente")
    list_filter = ("rol", "colaborador", "fecha")
    search_fields = ("cliente",)


@admin.register(DocumentoPersonal)
class DocumentoPersonalAdmin(admin.ModelAdmin):
    list_display = ("usuario", "tipo", "vigente_hasta", "actual", "subido")
    list_filter = ("tipo", "actual")
    search_fields = ("usuario__first_name", "usuario__last_name", "usuario__username")


@admin.register(SolicitudViaticos)
class SolicitudViaticosAdmin(admin.ModelAdmin):
    list_display = ("numero", "fecha", "destino", "para_texto", "conceptos_texto", "estado", "valor_entregado")
    list_filter = ("estado", "para_sst", "para_tecnicos", "fecha")
    search_fields = ("destino", "personas", "ticket__radicado")
    raw_id_fields = ("ticket", "actividad")
