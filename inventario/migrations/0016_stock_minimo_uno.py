from decimal import Decimal

from django.db import migrations, models


def minimo_uno(apps, schema_editor):
    """Decisión del 29/10/2026: todo producto arranca con stock mínimo de 1 (el encargado lo cambia si hace falta)."""
    apps.get_model("inventario", "Insumo").objects.filter(stock_minimo=0).update(stock_minimo=Decimal("1"))


class Migration(migrations.Migration):

    dependencies = [
        ("inventario", "0015_minimo_por_modelo"),
    ]

    operations = [
        migrations.AlterField(
            model_name="insumo",
            name="stock_minimo",
            field=models.DecimalField(decimal_places=2, default=1, help_text="Se avisa para reponer cuando en bodega queda menos de esto.",
                                      max_digits=12, verbose_name="stock mínimo (alerta)"),
        ),
        migrations.AlterField(
            model_name="minimomodelo",
            name="minimo",
            field=models.PositiveIntegerField(default=1, verbose_name="stock mínimo"),
        ),
        migrations.RunPython(minimo_uno, migrations.RunPython.noop),
    ]
