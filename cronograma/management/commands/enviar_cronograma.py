"""Envío automático del cronograma (reemplaza la programación de los jueves que hacía n8n).

Programar en el servidor para los jueves en la tarde, por ejemplo con cron:
    0 16 * * 4  cd /srv/janotec && .venv/bin/python manage.py enviar_cronograma

- Si está apagado en «Configuración del envío», no hace nada (salvo con --forzar).
- Si la semana (la siguiente o la en curso, según la configuración) no se ha enviado, envía su cronograma a cada técnico y a la administrativa.
- Si ya se envió y cambió algo, envía el aviso de cambios solo a los afectados.
- Si no hay nada nuevo, no envía nada.
"""
from datetime import date, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from cronograma import semanal
from cronograma.models import Actividad, ConfiguracionEnvio, ProgramacionSemana


class Command(BaseCommand):
    help = "Envía el cronograma de la semana siguiente (o el aviso de cambios) a los técnicos y a la administrativa."

    def add_arguments(self, parser):
        parser.add_argument("--semana", help="Cualquier día de la semana a enviar (AAAA-MM-DD). Por defecto, la próxima.")
        parser.add_argument("--simular", action="store_true", help="Muestra qué se enviaría, sin enviar.")
        parser.add_argument("--forzar", action="store_true", help="Envía aunque el envío automático esté apagado.")

    def handle(self, *args, **opts):
        config = ConfiguracionEnvio.actual()
        if not config.envio_automatico and not opts["forzar"] and not opts["simular"]:
            self.stdout.write("El envío automático del jueves está apagado (Cronograma → Configuración del envío).")
            return
        if opts["semana"]:
            try:
                dia = date.fromisoformat(opts["semana"])
            except ValueError:
                raise CommandError("Use el formato AAAA-MM-DD en --semana.")
        else:
            dia = timezone.localdate() + timedelta(days=0 if config.semana_objetivo == "actual" else 7)
        lunes = Actividad.lunes_de(dia)
        # «Programar para el jueves»: si se programó otra semana (sin --semana), esa es la que se envía
        if not opts["semana"]:
            programada = ProgramacionSemana.objects.order_by("lunes").first()
            if programada:
                lunes = programada.lunes
        etiqueta = semanal.etiqueta_semana(lunes)
        cambio = semanal.cambios(lunes)
        if cambio is None:
            correos, faltan = semanal.correos_semana(lunes)
            accion = "cronograma de la semana"
        elif cambio["hay"]:
            correos, faltan = semanal.correos_semana(lunes, cambio=cambio)
            accion = "aviso de cambios"
        else:
            self.stdout.write(f"Semana {etiqueta}: ya enviada y sin cambios. No se envía nada.")
            return
        if not correos:
            self.stdout.write(f"Semana {etiqueta}: no hay actividades con destinatarios.")
            return
        if opts["simular"]:
            for c in correos:
                self.stdout.write(f"  [{c['tipo']}] {c['email']} · {c['asunto']}")
        elif cambio is None:
            enviados, faltan = semanal.enviar_semana(lunes, automatica=True)
            self.stdout.write(self.style.SUCCESS(f"Semana {etiqueta}: {accion} enviado ({enviados} correos)."))
        else:
            enviados, faltan = semanal.notificar_cambios(lunes, automatica=True)
            self.stdout.write(self.style.SUCCESS(f"Semana {etiqueta}: {accion} enviado ({enviados} correos)."))
        for f in faltan:
            self.stdout.write(self.style.WARNING(f"  Sin correo: {f}"))
