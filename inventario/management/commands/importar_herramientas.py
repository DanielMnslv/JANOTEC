from django.core.management.base import BaseCommand, CommandError

from inventario.importar_global import ORIGEN, ImportadorHerramientas
from inventario.models import Herramienta


class Command(BaseCommand):
    help = ("Importa herramientas, entregas y auditorías de la exportación SQL de Inventario_Global (phpMyAdmin). "
            "Sin --confirmar solo simula.")

    def add_arguments(self, parser):
        parser.add_argument("ruta", help="Archivo .sql exportado de la base inventario_global")
        parser.add_argument("--confirmar", action="store_true", help="Guardar de verdad (sin esto solo simula)")
        parser.add_argument("--informe", help="Guardar el informe en este archivo")

    def handle(self, ruta, confirmar=False, informe=None, **opciones):
        if confirmar and Herramienta.objects.filter(importado_de__startswith=ORIGEN).exists():
            raise CommandError("Las herramientas de Inventario_Global ya se importaron. No se vuelven a cargar para no duplicar.")
        resultado = ImportadorHerramientas(ruta).ejecutar(simular=not confirmar)
        texto = ("SIMULACIÓN: no se guardó nada.\n\n" if not confirmar else "IMPORTACIÓN GUARDADA.\n\n") + resultado.texto_informe()
        if informe:
            with open(informe, "w", encoding="utf-8") as f:
                f.write(texto)
        self.stdout.write(texto)
