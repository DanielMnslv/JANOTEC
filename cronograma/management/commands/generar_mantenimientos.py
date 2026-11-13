"""Crea los tickets de los planes de mantenimiento que ya tocan (ver ``cronograma/mantenimiento.py``).

Programarlo una vez al día en el servidor:
    Linux (crontab -e):   30 6 * * *  cd /ruta/janotec_tickets && python3 manage.py generar_mantenimientos
"""
from django.core.management.base import BaseCommand

from cronograma.mantenimiento import dias_antes, generar


class Command(BaseCommand):
    help = "Crea los tickets de mantenimiento preventivo de los planes por sede que vencen en los próximos días."

    def handle(self, *args, **opts):
        creados = generar()
        for t in creados:
            self.stdout.write(f"  creado {t.radicado} · {t.empresa} · {t.sede}")
        self.stdout.write(self.style.SUCCESS(f"{len(creados)} mantenimiento(s) generados ({dias_antes()} días antes)."))
