"""Envía a un correo una muestra de cada correo que recibe el cliente, con datos de ejemplo.

    python manage.py muestras_correo_cliente d.jimenez@seguridadjano.com

Todo se hace dentro de una transacción que se deshace al final: no queda ningún ticket, acta ni actividad,
y los consecutivos no se consumen. Los enlaces de los correos (consulta, autorización) no funcionarán.
"""
from datetime import time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from accounts.models import Empresa, Usuario
from tickets import notificaciones as notif
from tickets.models import ActaSoporte, Categoria, EquipoActa, Ticket

E = Ticket.Estado


class Deshacer(Exception):
    pass


class Command(BaseCommand):
    help = "Envía una muestra de cada correo que recibe el cliente (sin dejar datos en la plataforma)."

    def add_arguments(self, parser):
        parser.add_argument("destino")
        parser.add_argument("--solo-estados", action="store_true",
                            help="Solo los 3 correos de estado: registrada, en proceso y cerrada.")

    def handle(self, destino, **opciones):
        enviados = []
        # Solo al destino: sin copias al buzón de soporte ni a las personas del área comercial
        with override_settings(NOTIFICAR_SOPORTE_EMAIL="", NOTIFICAR_COMERCIAL_EMAIL=destino):
            try:
                with transaction.atomic():
                    self._muestras(destino, enviados, opciones["solo_estados"])
                    raise Deshacer
            except Deshacer:
                pass
        for n, nombre in enumerate(enviados, 1):
            self.stdout.write(f"  {n}. {nombre}")
        self.stdout.write(self.style.SUCCESS(f"{len(enviados)} correos de muestra enviados a {destino}. No quedó ningún dato."))

    def _muestras(self, destino, enviados, solo_estados=False):
        from cronograma import servicios as crono
        from cronograma.models import Actividad

        coord = Usuario.objects.filter(username="jguana").first() or Usuario.objects.filter(is_superuser=True).first()
        tecnico = Usuario.objects.filter(rol="agente", is_active=True).exclude(email="").first() or coord
        empresa = Empresa.objects.filter(activa=True, sedes__isnull=False).first()
        sede = empresa.sedes.first()
        categoria = Categoria.objects.first()
        t = Ticket.objects.create(
            radicado="JNT-2026-MUESTRA", empresa=empresa, sede=sede, solicitante=coord, radicado_por=coord,
            categoria=categoria, tipo=Ticket.Tipo.SOPORTE, asunto="Cámara de la entrada sin imagen",
            descripcion="La cámara de la entrada principal no muestra imagen desde ayer en la tarde.",
            impacto="media", prioridad="media", contacto_nombre="Daniel Jiménez", contacto_telefono="3100000000",
            contacto_email=destino, departamento="Meta", municipio="Villavicencio", direccion=sede.direccion or "Calle 1",
            no_disponible={"viernes": "tarde"},
        )
        def paso(estado, mensaje, nombre):
            seg = t.cambiar_estado(estado, coord, mensaje)
            notif.actualizacion_para_cliente(t, seg)
            if estado in notif.ETAPAS_CLIENTE:  # las demás etapas no le llegan al cliente por correo
                enviados.append(nombre)

        notif.ticket_creado(t)
        enviados.append("Solicitud registrada (al radicar)")
        paso(E.RECEPCIONADO, "Recibimos su solicitud: estamos validando el requerimiento y pronto la asignaremos "
                             "al cronograma de visitas.", "Su solicitud está en proceso (recepcionado)")

        dia = timezone.localdate() + timedelta(days=2)
        while dia.weekday() >= 5:
            dia += timedelta(days=1)
        a = Actividad.objects.create(ticket=t, fecha=dia, hora_inicio=time(8), hora_fin=time(12), cliente=empresa.razon_social,
                                     sede=sede.nombre, actividad=t.descripcion, tipo=Actividad.Tipo.SOPORTE, notificar_a=destino)
        a.encargados.add(tecnico)
        t.visita_programada = a.inicio
        t.asignado_a = tecnico
        t.save(update_fields=["visita_programada", "asignado_a"])
        paso(E.EN_CRONOGRAMA, f"Programamos la visita técnica para el {crono.cuando(a)} con "
                              f"{tecnico.get_full_name() or tecnico.username}. Le enviaremos la autorización de ingreso "
                              "para que la acepte.", "Programamos su visita técnica (cronograma)")
        if not solo_estados:
            crono.enviar_autorizacion(a, coord)
            enviados.append("Autorización de ingreso (con botones Aceptar / Rechazar)")
        paso(E.AUTORIZADO, f"Gracias por autorizar el ingreso de nuestro personal para el {crono.cuando(a)}.",
             "Visita técnica confirmada (autorizada)")

        acta = ActaSoporte.objects.create(
            numero=999999, fecha=timezone.now(), origen=ActaSoporte.Origen.PLATAFORMA, ticket=t, empresa=empresa, sede=sede,
            sede_texto=str(sede), ciudad="Villavicencio", tecnico=tecnico, tecnico_texto=tecnico.get_full_name() or tecnico.username,
            actividad="Soporte", sistema="CCTV", diagnostico="Fuente de poder de la cámara averiada",
            resultado=ActaSoporte.Resultado.SOLUCIONADO,
            observaciones="Se reemplazó la fuente de poder de la cámara de la entrada y se verificó la imagen en el grabador.",
            recibio="Daniel Jiménez", cargo_recibio="Administrador", copia_correo=destino,
        )
        EquipoActa.objects.create(acta=acta, clase=EquipoActa.Clase.INTERVENIDO, equipo="Cámara IP bala", marca="Hikvision", cantidad="1")
        EquipoActa.objects.create(acta=acta, clase=EquipoActa.Clase.UTILIZADO, equipo="Fuente de poder 12 V 2 A", cantidad="1")
        if not solo_estados:
            notif.acta_registrada(acta)
            enviados.append("Acta de servicio técnico")
        paso(E.VISITA_EJECUTADA, f"Se diligenció el acta de soporte N.º {acta.numero} · Resultado: Solucionado.\n"
                                 f"{acta.observaciones}", "Visita técnica realizada (pide confirmar la solución)")
        paso(E.CERRADO, "Damos por cerrada su solicitud. ¡Gracias por confiar en JANOTEC!", "Su solicitud fue solucionada y cerrada (invita a calificar)")
        if solo_estados:
            return

        t2 = Ticket.objects.create(
            radicado="JNT-2026-MUESTRA2", empresa=empresa, sede=sede, solicitante=coord, radicado_por=coord, categoria=categoria,
            tipo=Ticket.Tipo.COTIZACION, area=Ticket.Area.COMERCIAL, asunto="Ampliación del sistema de cámaras",
            descripcion="Requiere 4 cámaras adicionales en el parqueadero.", contacto_nombre="Daniel Jiménez",
            contacto_email=destino, departamento="Meta", municipio="Villavicencio", direccion=sede.direccion or "Calle 1",
        )
        try:
            from comercial import servicios as com
            from comercial.models import FichaComercial
            ficha = FichaComercial.objects.create(ticket=t2)
            com.requerimiento_creado(ficha)
            enviados.append("Recibimos su requerimiento comercial (con la ficha en PDF)")
            ficha.valor_propuesta = 4500000
            com.propuesta_enviada(ficha, None)
            enviados.append("Le enviamos su propuesta económica")
        except Exception as e:  # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"No se pudieron generar las muestras comerciales: {e}"))
