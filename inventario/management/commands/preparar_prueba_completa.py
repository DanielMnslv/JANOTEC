"""Deja listo el entorno PRUEBA para la guía de validación completa de Daniel (todas las áreas, todas las bifurcaciones).

Crea o repara los usuarios de prueba de cada área (``prueba.comercial``, ``prueba.direccion``, ``prueba.tecnico``…,
ver ``accounts/prueba.py``) y completa lo que falte del entorno PRUEBA: cliente y sede, catálogo con precios, productos
con stock (y uno sin stock), equipos, proveedores sin correo. No crea casos: el recorrido se hace desde la radicación.
No envía correos y no toca nada que no sea de PRUEBA. Se puede correr las veces que se quiera.

Lo mismo hace el botón «Preparar el entorno de prueba» de la pantalla «Actuar como otra área».

Uso:
    python manage.py preparar_prueba_completa
    python manage.py preparar_prueba_completa --ensayo     # hace todo y lo deshace (para revisar)
"""
from django.core.management.base import CommandError
from django.db import transaction
from django.test.utils import override_settings

from accounts import prueba
from inventario.management.commands import preparar_prueba_administrativa as base


class Command(base.Command):
    help = "Crea los usuarios de prueba de cada área y completa el entorno PRUEBA (catálogo, stock, proveedores)."

    def add_arguments(self, parser):
        parser.add_argument("--autor", default="", help="Superusuario que queda como autor en el historial "
                                                        "(por defecto, el primero activo).")
        parser.add_argument("--ensayo", action="store_true", help="Hace todo y lo deshace al final.")

    def handle(self, *args, **op):
        """Usuarios de prueba y entorno en una sola transacción, sin enviar correos."""
        try:
            self.autor = self._autor(op.get("autor", ""))
        except CommandError:
            raise CommandError("Hace falta un superusuario activo (es quien actúa como las demás áreas).")
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend"), transaction.atomic():
            creados = prueba.asegurar_usuarios()
            self._entorno()
            self.stdout.write(f"Usuarios de prueba: {len(prueba.USUARIOS)} ({creados} nuevos), sin contraseña ni correo.")
            self.stdout.write(f"Cliente: {self.empresa} · sede {self.sede.nombre}. Catálogo PRUEBA: {len(self.catalogo)} ítems. "
                              f"Proveedores PRUEBA: {len(self.proveedores)} (sin correo).")
            if op.get("ensayo"):
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Ensayo: todo se deshizo, no quedó nada creado."))
            else:
                self.stdout.write(self.style.SUCCESS("Entorno de prueba listo. Entre a «Actuar como otra área» desde su menú."))
