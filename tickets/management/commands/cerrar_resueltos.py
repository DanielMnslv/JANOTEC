"""Cierra los tickets «Resuelto» que el cliente no confirmó ni reabrió dentro del plazo.

Programarlo cada hora en el servidor:
    Linux (crontab -e):   0 * * * *  cd /ruta/janotec_tickets && .venv/bin/python manage.py cerrar_resueltos
    Windows: Programador de tareas → «.venv\\Scripts\\python.exe manage.py cerrar_resueltos», cada hora
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from tickets.cierre import cerrar_vencidos


class Command(BaseCommand):
    help = "Cierra los tickets resueltos sin respuesta del cliente tras TICKET_DIAS_CIERRE_AUTOMATICO días."

    def handle(self, *args, **opts):
        cerrados = cerrar_vencidos()
        for t in cerrados:
            self.stdout.write(f"  cerrado {t.radicado} · {t.empresa}")
        self.stdout.write(self.style.SUCCESS(
            f"{len(cerrados)} ticket(s) cerrados automáticamente (plazo: {settings.TICKET_DIAS_CIERRE_AUTOMATICO} días)."
        ))
