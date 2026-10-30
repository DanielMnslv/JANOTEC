from django.core.management.base import BaseCommand, CommandError

from inventario.importar import Importador
from inventario.models import Activo, Insumo, MovimientoActivo, MovimientoInsumo


class Command(BaseCommand):
    help = "Importa el Google Sheets INVENTARIO (.xlsx) con todo su historial. Sin --confirmar solo simula."

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo .xlsx descargado del Google Sheets (Archivo → Descargar → Excel)")
        parser.add_argument("--confirmar", action="store_true", help="Guardar de verdad (sin esto solo simula)")
        parser.add_argument("--informe", help="Guardar el informe en este archivo")

    def handle(self, ruta, confirmar=False, informe=None, **opciones):
        if confirmar and (MovimientoActivo.objects.filter(importado_de__gt="").exists()
                          or MovimientoInsumo.objects.filter(importado_de__gt="").exists()):
            raise CommandError("El inventario del Google Sheets ya se importó. No se vuelve a cargar para no duplicar.")
        if confirmar and (Activo.objects.exists() or Insumo.objects.exists()):
            raise CommandError("Ya hay activos o insumos en la plataforma: la importación es para arrancar desde cero.")
        resultado = Importador(ruta).ejecutar(simular=not confirmar)
        texto = ("SIMULACIÓN: no se guardó nada.\n\n" if not confirmar else "IMPORTACIÓN GUARDADA.\n\n") + resultado.texto_informe()
        if informe:
            with open(informe, "w", encoding="utf-8") as f:
                f.write(texto)
        self.stdout.write(texto)
