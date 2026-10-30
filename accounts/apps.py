from django.apps import AppConfig
from django.db.models.signals import post_migrate, post_save


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"
    verbose_name = "Usuarios y clientes"

    def ready(self):
        from .grupos import asegurar_grupo, sincronizar_usuario

        # Sin «sender»: corre tras migrar cada app, así existen ya los permisos de tickets
        post_migrate.connect(asegurar_grupo, dispatch_uid="accounts_asegurar_grupo")

        def al_guardar_usuario(sender, instance, raw=False, **kwargs):
            if not raw:
                sincronizar_usuario(instance)

        post_save.connect(al_guardar_usuario, sender="accounts.Usuario", weak=False,
                          dispatch_uid="accounts_sincronizar_grupo")
