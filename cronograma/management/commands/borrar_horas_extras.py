"""Borra los registros de horas extras de prueba, dejando antes un respaldo de ellos (18/11/2026).

Sin --confirmar solo muestra lo que borraría, por colaborador y mes. Con --confirmar guarda los
registros en respaldos/horas_extras_FECHA.json.gz y los borra. Las fotos se quedan en media/.
"""
import gzip
import json
from collections import Counter
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from cronograma.models import ReporteHorasExtra


class Command(BaseCommand):
    help = "Borra los registros de horas extras (eran de prueba), con respaldo. Sin --confirmar solo simula."

    def add_arguments(self, parser):
        parser.add_argument("--confirmar", action="store_true", help="Saca el respaldo y BORRA. Sin esto solo simula.")
        parser.add_argument("--carpeta", default=str(Path(settings.BASE_DIR) / "respaldos"),
                            help="Dónde guardar el respaldo (por defecto respaldos/ junto a manage.py).")

    def handle(self, *args, confirmar=False, carpeta=None, **opciones):
        registros = ReporteHorasExtra.objects.select_related("colaborador").order_by("fecha", "pk")
        resumen = Counter((r.colaborador.get_full_name() or r.colaborador.username, f"{r.fecha:%m/%Y}")
                          for r in registros)
        self.stdout.write(self.style.MIGRATE_HEADING("HORAS EXTRAS QUE SE BORRAN"))
        for (quien, mes), n in sorted(resumen.items()):
            self.stdout.write(f"  {quien:<45} {mes:>8} {n:>5}")
        self.stdout.write(f"  {'Total':<54} {len(registros):>5}")
        if not confirmar:
            self.stdout.write(self.style.WARNING(
                "\nSIMULACIÓN: no se borró nada. Revise la lista y, si está bien, repita con --confirmar."))
            return
        if not registros:
            self.stdout.write("No hay nada que borrar.")
            return

        carpeta = Path(carpeta)
        carpeta.mkdir(parents=True, exist_ok=True)
        archivo = carpeta / f"horas_extras_{timezone.localtime():%Y-%m-%d_%H%M}.json.gz"
        with gzip.open(archivo, "wt", encoding="utf-8") as salida:
            salida.write(serializers.serialize("json", registros))
        json.load(gzip.open(archivo, "rt", encoding="utf-8"))  # que se pueda leer antes de borrar
        self.stdout.write(self.style.SUCCESS(f"\nRespaldo: {archivo}"))
        with transaction.atomic():
            borrados, _ = ReporteHorasExtra.objects.all().delete()
        self.stdout.write(self.style.SUCCESS(f"Listo. Se borraron {borrados} registros de horas extras."))
