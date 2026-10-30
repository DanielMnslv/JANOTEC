"""Revisa la salud de los procesos y avisa por correo lo nuevo (para programarlo en el servidor, p. ej. cada hora).

    python manage.py diagnosticar
"""
from django.core.management.base import BaseCommand

from tickets import diagnostico


class Command(BaseCommand):
    help = "Detecta en el uso real lo que no sigue la lógica de los procesos y avisa al superusuario."

    def handle(self, *args, **opciones):
        nuevos = diagnostico.revisar()
        avisados = diagnostico.avisar(forzar=True)
        self.stdout.write(self.style.SUCCESS(f"{len(nuevos)} situación(es) nueva(s); {avisados} avisada(s) por correo."))
