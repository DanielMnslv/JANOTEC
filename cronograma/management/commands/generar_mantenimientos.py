"""Avisa a coordinación los mantenimientos que llegan a los 15 días (ver ``cronograma/mantenimiento.py``).

Desde el 23/11/2026 no crea tickets: coordinación los programa desde la ficha de la sede en «Clientes y sedes».
Programarlo una vez al día en el servidor:
    Linux (crontab -e):   30 6 * * *  cd /ruta/janotec_tickets && python3 manage.py generar_mantenimientos
"""
from django.core.management.base import BaseCommand

from cronograma.mantenimiento import avisar, dias_antes


class Command(BaseCommand):
    help = "Avisa a coordinación los mantenimientos preventivos que vencen en los próximos días (no crea tickets)."

    def handle(self, *args, **opts):
        avisados = avisar()
        for p in avisados:
            self.stdout.write(f"  aviso {p.proxima:%d/%m/%Y} · {p.sede.empresa} · {p.sede} · {p.get_sistema_display()}")
        self.stdout.write(self.style.SUCCESS(f"{len(avisados)} mantenimiento(s) avisados ({dias_antes()} días antes)."))
