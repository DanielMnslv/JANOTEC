"""Datos de prueba SOLO para desarrollo local. No usar en producción."""
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from accounts.models import Empresa, Sede, Usuario
from tickets.models import Categoria, Seguimiento, Ticket

CLAVE = "Janotec2026*"


class Command(BaseCommand):
    help = "Carga empresas, usuarios y tickets de ejemplo para probar la plataforma."

    def add_arguments(self, parser):
        parser.add_argument("--forzar", action="store_true", help="Permitir aunque DEBUG=False")

    def handle(self, *args, **opts):
        if not settings.DEBUG and not opts["forzar"]:
            raise CommandError("DEBUG=False: este comando es solo para desarrollo (use --forzar si está seguro).")
        if Empresa.objects.exclude(codigo__isnull=True).exists() and not opts["forzar"]:
            raise CommandError(
                "La base ya tiene datos reales (importados con importar_maestros); "
                "no se mezclan con datos de prueba. Use una base aparte (DB_ENGINE=sqlite) o --forzar."
            )
        call_command("inicializar")

        def usuario(username, nombre, apellido, rol, empresa=None, **extra):
            u, creado = Usuario.objects.get_or_create(
                username=username,
                defaults=dict(first_name=nombre, last_name=apellido, rol=rol, empresa=empresa,
                              email=f"{username}@ejemplo.com", **extra),
            )
            if creado:
                u.set_password(CLAVE)
                u.save()
            return u

        sup = usuario("coordinador", "Laura", "Rincón", Usuario.Rol.SUPERVISOR, is_staff=True)
        tec1 = usuario("tecnico1", "Andrés", "Pardo", Usuario.Rol.AGENTE)
        tec2 = usuario("tecnico2", "Camila", "Rojas", Usuario.Rol.AGENTE)

        e1, _ = Empresa.objects.get_or_create(nit="900000001-1", defaults=dict(razon_social="Comercializadora El Llano S.A.S.", ciudad="Villavicencio"))
        e2, _ = Empresa.objects.get_or_create(nit="900000002-2", defaults=dict(razon_social="Clínica Demo del Meta", ciudad="Villavicencio"))
        s1, _ = Sede.objects.get_or_create(empresa=e1, nombre="Bodega principal", defaults=dict(ciudad="Villavicencio", direccion="Km 3 vía Puerto López"))
        s2, _ = Sede.objects.get_or_create(empresa=e1, nombre="Punto de venta Centro", defaults=dict(ciudad="Villavicencio"))
        s3, _ = Sede.objects.get_or_create(empresa=e2, nombre="Sede Barzal", defaults=dict(ciudad="Villavicencio"))

        c1 = usuario("cliente1", "Mario", "Gutiérrez", Usuario.Rol.CLIENTE, e1, cargo="Jefe de seguridad", telefono="3100000000")
        c2 = usuario("cliente2", "Sandra", "Mejía", Usuario.Rol.CLIENTE, e2, cargo="Administradora")

        if Ticket.objects.exists():
            self.stdout.write("Ya hay tickets; no se crean tickets de ejemplo.")
        else:
            cat = {c.nombre: c for c in Categoria.objects.all()}
            ejemplos = [
                (e1, s1, c1, "soporte", "CCTV / Videovigilancia", "Cámara 4 de la bodega sin imagen", "Desde anoche la cámara del muelle de carga muestra 'sin señal' en el NVR.", "alta", None, Ticket.Estado.RADICADO, ("Jorge (vigilante)", "3201112233", {"domingo": "todo"})),
                (e1, s2, c1, "soporte", "Alarmas", "Sensor de la puerta trasera dispara falsas alarmas", "La central nos ha llamado 3 veces esta semana por la zona 5.", "media", tec1, Ticket.Estado.EN_PROCESO, None),
                (e2, s3, c2, "soporte", "Control de acceso", "Biométrico de urgencias no reconoce huellas", "El lector de la puerta de urgencias rechaza a todo el personal desde hoy.", "alta", tec2, Ticket.Estado.ASIGNADO, None),
                (e2, s3, c2, "peticion", "Mantenimiento preventivo", "Programar mantenimiento trimestral", "Solicitamos agendar el mantenimiento del cuarto de equipos.", "", tec1, Ticket.Estado.RESUELTO, None),
                (e1, None, c1, "reclamo", "Facturación y contratos", "Cobro doble en la factura de septiembre", "En la factura de septiembre aparece dos veces el cargo de mantenimiento.", "", None, Ticket.Estado.RADICADO, None),
            ]
            for emp, sede, sol, tipo, cat_n, asunto, desc, impacto, tec, estado, contacto in ejemplos:
                nombre, tel, horario = contacto or (
                    sol.get_full_name(), sol.telefono, {"viernes": "tarde"})
                t = Ticket(empresa=emp, sede=sede, solicitante=sol, tipo=tipo, categoria=cat[cat_n],
                           asunto=asunto, descripcion=desc, impacto=impacto, asignado_a=tec,
                           contacto_nombre=nombre, contacto_telefono=tel, contacto_email=sol.email,
                           no_disponible=horario)
                t.asignar_prioridad_inicial()
                t.save()
                Seguimiento.objects.create(ticket=t, autor=sol, tipo=Seguimiento.Tipo.CREACION,
                                           mensaje="Solicitud radicada desde el portal.", estado_nuevo=t.estado)
                if estado != Ticket.Estado.RADICADO:
                    Seguimiento.objects.create(ticket=t, autor=sup, tipo=Seguimiento.Tipo.ASIGNACION, interno=True,
                                               mensaje=f"Asignado a {tec}.")
                    t.cambiar_estado(Ticket.Estado.EN_REVISION, sup, "Recibimos su solicitud y la estamos revisando.")
                    t.cambiar_estado(Ticket.Estado.ASIGNADO, sup, f"Asignamos al técnico {tec.first_name}.")
                if estado in (Ticket.Estado.EN_PROCESO, Ticket.Estado.RESUELTO):
                    t.cambiar_estado(Ticket.Estado.EN_PROCESO, tec, "Visita técnica programada para mañana en la mañana.")
                    Seguimiento.objects.create(ticket=t, autor=tec, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                                               mensaje="Llevar sensor magnético de repuesto y revisar cableado de la zona.")
                if estado == Ticket.Estado.RESUELTO:
                    t.cambiar_estado(Ticket.Estado.RESUELTO, tec, "Mantenimiento realizado. Adjuntaremos el informe técnico.")

        self.stdout.write(self.style.SUCCESS(
            f"\nDatos de prueba listos. Contraseña de todos los usuarios: {CLAVE}\n"
            "  Clientes:  cliente1 (Comercializadora El Llano), cliente2 (Clínica Demo)\n"
            "  Encargados: tecnico1, tecnico2 (solo ven lo que se les asigna)\n"
            "  Área administrativa: coordinador (direcciona y asigna)\n"
        ))
