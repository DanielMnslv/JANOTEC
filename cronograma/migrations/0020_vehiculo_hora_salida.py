from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("cronograma", "0019_solicitud_vehiculo")]

    operations = [
        migrations.AddField(
            model_name="actividad",
            name="vehiculo_hora_salida",
            field=models.TimeField("hora de salida del vehículo", null=True, blank=True),
        ),
    ]
