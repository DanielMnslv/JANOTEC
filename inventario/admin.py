from django.contrib import admin

from .models import (
    Activo, AuditoriaHerramientas, CodigoActivo, DetalleAuditoria, EntregaHerramienta, Herramienta, Insumo,
    MovimientoActivo, MovimientoInsumo, Proveedor,
)


@admin.register(Insumo)
class InsumoAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descripcion", "unidad", "marca", "stock_minimo", "activo")
    list_filter = ("unidad", "activo")
    search_fields = ("codigo", "descripcion", "marca")


@admin.register(CodigoActivo)
class CodigoActivoAdmin(admin.ModelAdmin):
    list_display = ("codigo", "descripcion", "marca")
    search_fields = ("codigo", "descripcion", "marca")


@admin.register(Proveedor)
class ProveedorAdmin(admin.ModelAdmin):
    list_display = ("razon_social", "nit")
    search_fields = ("razon_social", "nit")
    filter_horizontal = ("marcas",)


class MovimientoActivoInline(admin.TabularInline):
    model = MovimientoActivo
    extra = 0
    can_delete = False
    fields = ("fecha", "tipo", "estado", "destino", "tecnico_nombre", "cliente_texto", "modalidad", "observaciones")
    readonly_fields = fields
    show_change_link = True


@admin.register(Activo)
class ActivoAdmin(admin.ModelAdmin):
    list_display = ("placa", "serial", "categoria", "tipo", "marca", "referencia", "estado", "lugar", "tecnico_nombre",
                    "cliente_texto")
    list_filter = ("estado", "lugar", "categoria", "marca")
    search_fields = ("placa", "serial", "referencia", "descripcion")
    # El estado y la ubicación los cambian los movimientos (entregas, actas, devoluciones), no a mano
    readonly_fields = ("estado", "lugar", "tecnico", "tecnico_nombre", "empresa", "cliente_texto", "sede", "modalidad",
                       "ticket_destino", "fecha_ultimo_movimiento")
    inlines = [MovimientoActivoInline]


@admin.register(MovimientoInsumo)
class MovimientoInsumoAdmin(admin.ModelAdmin):
    list_display = ("fecha", "insumo", "cantidad", "tipo", "origen", "destino", "tecnico_nombre", "cliente_texto")
    list_filter = ("tipo", "origen", "destino", "modalidad")
    search_fields = ("insumo__codigo", "insumo__descripcion", "tecnico_nombre", "cliente_texto", "factura")
    date_hierarchy = "fecha"


@admin.register(MovimientoActivo)
class MovimientoActivoAdmin(admin.ModelAdmin):
    list_display = ("fecha", "activo", "tipo", "estado", "destino", "tecnico_nombre", "cliente_texto")
    list_filter = ("tipo", "estado", "destino", "modalidad")
    search_fields = ("activo__placa", "activo__serial", "tecnico_nombre", "cliente_texto")
    date_hierarchy = "fecha"


class EntregaHerramientaInline(admin.TabularInline):
    model = EntregaHerramienta
    extra = 0
    can_delete = False
    fields = ("fecha", "tipo", "tecnico_nombre", "registrado_por")
    readonly_fields = fields


@admin.register(Herramienta)
class HerramientaAdmin(admin.ModelAdmin):
    list_display = ("nombre", "marca", "serial", "cantidad", "estado", "tecnico", "tecnico_nombre", "costo_compra")
    list_filter = ("estado", "tecnico")
    search_fields = ("nombre", "serial", "marca", "tecnico_nombre")
    inlines = [EntregaHerramientaInline]


class DetalleAuditoriaInline(admin.TabularInline):
    model = DetalleAuditoria
    extra = 0
    can_delete = False
    fields = ("herramienta_nombre", "cantidad_registrada", "cantidad_encontrada", "estado_registrado", "estado_encontrado",
              "resultado", "nota")
    readonly_fields = fields


@admin.register(AuditoriaHerramientas)
class AuditoriaHerramientasAdmin(admin.ModelAdmin):
    """Solo consulta: la auditoría es un registro permanente."""
    list_display = ("pk", "fecha", "tecnico_nombre", "auditor_nombre", "resultado", "novedades")
    list_filter = ("resultado",)
    inlines = [DetalleAuditoriaInline]

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        return False
