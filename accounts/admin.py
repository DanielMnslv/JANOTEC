from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import Empresa, Sede, Usuario


class SedeInline(admin.TabularInline):
    model = Sede
    extra = 1


class UsuarioInline(admin.TabularInline):
    model = Usuario
    fields = ("username", "first_name", "last_name", "email", "cargo", "is_active")
    extra = 0
    show_change_link = True
    can_delete = False


@admin.register(Empresa)
class EmpresaAdmin(admin.ModelAdmin):
    list_display = ("razon_social", "nit", "sector", "ciudad", "activa", "es_prospecto")
    list_filter = ("activa", "es_prospecto", "sector", "ciudad")
    search_fields = ("razon_social", "nit", "codigo")
    inlines = [SedeInline, UsuarioInline]


@admin.register(Sede)
class SedeAdmin(admin.ModelAdmin):
    list_display = ("nombre", "empresa", "ciudad", "abonado", "activa")
    list_filter = ("empresa", "activa")
    search_fields = ("nombre", "empresa__razon_social", "ciudad", "abonado")


@admin.register(Usuario)
class UsuarioAdmin(UserAdmin):
    list_display = ("username", "get_full_name", "email", "rol", "tambien_comercial", "tambien_tecnico", "empresa", "is_active")
    list_filter = ("rol", "empresa", "is_active")
    search_fields = ("username", "first_name", "last_name", "email", "documento", "empresa__razon_social")
    fieldsets = UserAdmin.fieldsets + (
        ("JANOTEC", {"fields": ("rol", "tambien_comercial", "tambien_tecnico", "empresa", "documento", "telefono", "cargo", "area", "sede")}),
    )
    add_fieldsets = UserAdmin.add_fieldsets + (
        ("Datos JANOTEC", {"fields": ("first_name", "last_name", "email", "rol", "empresa", "documento", "telefono", "cargo")}),
    )

    # Campos que solo el superusuario puede tocar: evitan que un supervisor se dé más poder
    CAMPOS_DE_SUPERUSUARIO = ("is_superuser", "is_staff", "groups", "user_permissions")

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        # El supervisor gestiona clientes y encargados, y su propia cuenta.
        # Superusuarios y otros supervisores los gestiona solo el superusuario.
        from django.db.models import Q
        return qs.filter(Q(is_superuser=False, rol__in=[Usuario.Rol.CLIENTE, Usuario.Rol.AGENTE, Usuario.Rol.COMERCIAL, Usuario.Rol.CENTRAL,
                                                                 Usuario.Rol.BODEGA])
                         | Q(pk=request.user.pk))

    def get_readonly_fields(self, request, obj=None):
        campos = super().get_readonly_fields(request, obj)
        if not request.user.is_superuser:
            campos = tuple(campos) + self.CAMPOS_DE_SUPERUSUARIO + ("last_login", "date_joined")
            if obj is not None and obj.pk == request.user.pk:
                campos += ("rol", "is_active")  # no puede cambiarse su propio rol ni desactivarse
        return campos

    def formfield_for_choice_field(self, db_field, request, **kwargs):
        if db_field.name == "rol" and not request.user.is_superuser:
            # Solo el superusuario nombra supervisores
            kwargs["choices"] = [c for c in Usuario.Rol.choices if c[0] not in (Usuario.Rol.SUPERVISOR, Usuario.Rol.DIRECCION)]
        return super().formfield_for_choice_field(db_field, request, **kwargs)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if request.user.is_superuser or obj is None:
            return fieldsets
        # Al supervisor no se le muestran grupos ni permisos sueltos
        limpios = []
        for nombre, opciones in fieldsets:
            campos = [c for c in opciones.get("fields", ()) if c not in ("groups", "user_permissions", "is_superuser")]
            limpios.append((nombre, {**opciones, "fields": campos}))
        return limpios
