"""Yuri (ychingate) y Sindy (smedina) siguen en bodega, pero pasan de «también comercial» a «también administrativa».

Cambio de datos reales aprobado el 04/11/2026 al separar el área comercial (requerimientos y seguimiento) del área
administrativa (propuestas). Mientras el área comercial no tenga usuario, dirección hace su trabajo.
Queda también en el historial de cada usuario (Configuración → Usuarios → Historial).
"""
from django.db import migrations

QUIENES = ("ychingate", "smedina")
MENSAJE = "Pasa de «también área comercial» a «también área administrativa» (separación de áreas, 04/11/2026)."


def a_administrativa(apps, schema_editor):
    Usuario = apps.get_model("accounts", "Usuario")
    LogEntry = apps.get_model("admin", "LogEntry")
    ContentType = apps.get_model("contenttypes", "ContentType")
    tipo, _ = ContentType.objects.get_or_create(app_label="accounts", model="usuario")
    autor = Usuario.objects.filter(is_superuser=True).order_by("pk").first()
    for u in Usuario.objects.filter(username__in=QUIENES):
        u.tambien_comercial, u.tambien_administrativo = False, True
        u.save(update_fields=["tambien_comercial", "tambien_administrativo"])
        if autor:
            LogEntry.objects.create(user_id=autor.pk, content_type_id=tipo.pk, object_id=str(u.pk),
                                    object_repr=(f"{u.first_name} {u.last_name}".strip() or u.username)[:200],
                                    action_flag=2, change_message=MENSAJE)


def a_comercial(apps, schema_editor):
    Usuario = apps.get_model("accounts", "Usuario")
    Usuario.objects.filter(username__in=QUIENES).update(tambien_comercial=True, tambien_administrativo=False)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0014_area_administrativa"),
        ("admin", "0003_logentry_add_action_flag_choices"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [migrations.RunPython(a_administrativa, a_comercial)]
