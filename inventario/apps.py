from django.apps import AppConfig
from django.db.models.signals import post_save


def _vincular(sender, instance, **kwargs):
    from .servicios import vincular_usuario
    vincular_usuario(instance)


class InventarioConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "inventario"
    verbose_name = "Inventario (activos e insumos)"

    def ready(self):
        # Al crear el usuario de un técnico se le asocia lo que el inventario anterior tenía a su nombre
        post_save.connect(_vincular, sender="accounts.Usuario", dispatch_uid="inventario_vincular_usuario")
