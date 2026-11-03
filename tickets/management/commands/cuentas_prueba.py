"""Cuentas para practicar el flujo sin tocar clientes reales.

    python manage.py cuentas_prueba            # crea (o reinicia claves de) la empresa y usuarios de prueba
    python manage.py cuentas_prueba --eliminar # borra la empresa de prueba, sus usuarios y sus tickets

Todo queda en la empresa «ZZ PRUEBAS JANOTEC (no es cliente)». Las contraseñas se generan
al azar y se guardan en datos_iniciales/cuentas_de_prueba.txt (carpeta excluida de git).
"""
import secrets
import string
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import Empresa, Sede, Usuario
from tickets.models import ContadorRadicado, Ticket

CODIGO = "pruebas"
CUENTAS = [
    # usuario, nombres, apellidos, rol, cargo
    ("cliente.prueba", "Cliente", "De Prueba", Usuario.Rol.CLIENTE, "Administrador de la sede (prueba)"),
    ("tecnico.prueba", "Técnico", "De Prueba", Usuario.Rol.AGENTE, "Técnico de prueba"),
    ("comercial.prueba", "Comercial", "De Prueba", Usuario.Rol.COMERCIAL, "Asesor comercial de prueba"),
    ("central.prueba", "Central", "De Prueba", Usuario.Rol.CENTRAL, "Operador de la central de atención de prueba"),
]
ARCHIVO = Path(settings.BASE_DIR) / "datos_iniciales" / "cuentas_de_prueba.txt"


def clave_aleatoria():
    letras = string.ascii_letters + string.digits
    return "Jt-" + "".join(secrets.choice(letras) for _ in range(12))


class Command(BaseCommand):
    help = "Crea o elimina las cuentas de prueba para practicar los roles cliente y técnico."

    def add_arguments(self, parser):
        parser.add_argument("--eliminar", action="store_true", help="Borra la empresa de prueba, sus usuarios y tickets")

    @transaction.atomic
    def handle(self, *args, eliminar=False, **opts):
        if eliminar:
            return self._eliminar()

        empresa, _ = Empresa.objects.update_or_create(
            codigo=CODIGO,
            defaults={"razon_social": "ZZ PRUEBAS JANOTEC (no es cliente)", "nit": None, "ciudad": "Villavicencio",
                      "activa": True},
        )
        for nombre, ciudad, direccion, abonado in [("Sede principal (prueba)", "Villavicencio", "Calle de prueba 1", "0001"),
                                                    ("Bodega (prueba)", "Acacías", "Vereda de prueba", "0002")]:
            Sede.objects.update_or_create(empresa=empresa, nombre=nombre,
                                          defaults={"ciudad": ciudad, "direccion": direccion, "abonado": abonado})
        lineas = ["CUENTAS DE PRUEBA · Plataforma de tickets JANOTEC",
                  "Solo para practicar. Se borran con: python manage.py cuentas_prueba --eliminar", ""]
        # Las contraseñas que ya están en uso se conservan; solo las cuentas nuevas reciben una
        anteriores = {}
        if ARCHIVO.exists():
            for linea in ARCHIVO.read_text(encoding="utf-8").splitlines():
                partes = linea.split()
                if "usuario:" in partes and "contraseña:" in partes:
                    anteriores[partes[partes.index("usuario:") + 1]] = partes[partes.index("contraseña:") + 1]
        for usuario, nombres, apellidos, rol, cargo in CUENTAS:
            u, _ = Usuario.objects.get_or_create(username=usuario)
            clave = anteriores.get(usuario)
            if not (clave and u.check_password(clave)):
                clave = clave_aleatoria()
                u.set_password(clave)
            u.first_name, u.last_name, u.rol, u.cargo, u.is_active = nombres, apellidos, rol, cargo, True
            u.empresa = empresa if rol == Usuario.Rol.CLIENTE else None
            u.email = ""
            u.save()
            lineas.append(f"{u.get_rol_display():<22} usuario: {usuario:<16} contraseña: {clave}")
        ARCHIVO.parent.mkdir(exist_ok=True)
        ARCHIVO.write_text("\n".join(lineas) + "\n", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"Cuentas de prueba listas. Usuarios y contraseñas en: {ARCHIVO}"))

    def _eliminar(self):
        from django.db.models import Q

        empresa = Empresa.objects.filter(codigo=CODIGO).first()
        de_prueba = [c[0] for c in CUENTAS]
        # Lo que se haya practicado, aunque sea sobre un cliente real (la ficha comercial cae con su ticket)
        tickets = Ticket.objects.filter(Q(empresa=empresa) | Q(solicitante__username__in=de_prueba))
        n_tickets = tickets.count()
        # Desvincula las actas de prueba que se hayan asociado a esos tickets y borra todo
        tickets.delete()
        usuarios = Usuario.objects.filter(username__in=de_prueba)
        n_usuarios = usuarios.count()
        usuarios.delete()
        if empresa:
            empresa.actas.all().delete()
            empresa.sedes.all().delete()
            empresa.delete()
        # Si no quedan tickets reales, el próximo radicado vuelve a empezar en 000001
        if not Ticket.objects.exists():
            ContadorRadicado.objects.all().delete()
        if ARCHIVO.exists():
            ARCHIVO.unlink()
        self.stdout.write(self.style.SUCCESS(f"Eliminado: empresa de prueba, {n_tickets} tickets y {n_usuarios} usuarios."))
