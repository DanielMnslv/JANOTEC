from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tickets", "0050_version_corregida_informe")]

    operations = [
        migrations.AddField(
            model_name="actasoporte",
            name="envio_campo",
            field=models.UUIDField(blank=True, editable=False, null=True, unique=True),
        ),
    ]
