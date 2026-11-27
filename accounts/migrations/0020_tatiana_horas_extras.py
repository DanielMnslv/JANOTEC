from django.db import migrations


def asignar_horas_extras(apps, schema_editor):
    Usuario = apps.get_model("accounts", "Usuario")
    Usuario.objects.using(schema_editor.connection.alias).filter(username__iexact="smedina").update(
        lleva_horas_extras=True,
    )


class Migration(migrations.Migration):
    dependencies = [("accounts", "0019_lleva_horas_extras")]
    operations = [migrations.RunPython(asignar_horas_extras, migrations.RunPython.noop)]
