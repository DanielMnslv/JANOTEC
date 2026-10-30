from django.core.management.base import BaseCommand

from inventario import alertas


class Command(BaseCommand):
    help = "Envía a bodega la alerta semanal de insumos por reponer (una vez por semana; --forzar la reenvía)."

    def add_arguments(self, parser):
        parser.add_argument("--forzar", action="store_true")

    def handle(self, *args, **opciones):
        n = alertas.enviar(forzar=opciones["forzar"])
        self.stdout.write(f"Insumos por reponer reportados: {n}")
