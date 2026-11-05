"""El comando que deja listos los casos de la guía de Yuri y Tatiana funciona aunque la base no tenga nada de PRUEBA."""
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounts.models import Usuario
from comercial.models import FichaComercial
from inventario.models import OrdenCompra, SolicitudMaterial
from tickets.models import Categoria, Ticket


class PrepararPruebaAdministrativaTests(TestCase):
    def setUp(self):
        Categoria.objects.create(nombre="Instalación / ampliación")
        Usuario.objects.create_superuser("admin", "admin@x.co", "Clave-segura-1")
        for usuario in ("ychingate", "smedina"):
            Usuario.objects.create_user(usuario, f"{usuario}@x.co", "Clave-segura-1", rol="bodega", tambien_administrativo=True)

    def preparar(self, *args):
        call_command("preparar_prueba_administrativa", *args, stdout=StringIO())

    def test_deja_cada_caso_donde_actua_cada_persona(self):
        self.preparar()
        self.assertEqual(mail.outbox, [], "mientras prepara no sale ningún correo")
        for nombre in ("Yuri", "Tatiana"):
            casos = Ticket.objects.filter(asunto__startswith=f"PRUEBA {nombre} · ").order_by("pk")
            self.assertEqual(casos.count(), 6)
            etapas = [t.ficha.etapa for t in casos]
            self.assertEqual(etapas[:4], [FichaComercial.Etapa.ELABORANDO, FichaComercial.Etapa.ELABORANDO,
                                          FichaComercial.Etapa.AJUSTES, FichaComercial.Etapa.ELABORANDO])
            self.assertIn("margen a 25", casos[1].ficha.cotizaciones.get().observaciones_direccion)
            self.assertEqual(casos[4].solicitudes_material.get().estado, SolicitudMaterial.Estado.VALIDACION)
            self.assertEqual(casos[5].solicitudes_material.get().estado, SolicitudMaterial.Estado.APROBADA)
            ordenes = OrdenCompra.objects.filter(proveedor__razon_social=f"PRUEBA PROVEEDOR {nombre.upper()}").order_by("pk")
            self.assertEqual([o.estado for o in ordenes], [OrdenCompra.Estado.BORRADOR, OrdenCompra.Estado.LISTA])
            self.assertTrue(all(not o.proveedor.correo for o in ordenes), "el proveedor de prueba no tiene correo")

    def test_funciona_aunque_yuri_revise_las_ordenes(self):
        """Con la revisión administrativa activa, las órdenes quedan igual: devuelta (en preparación) y pagada (lista)."""
        Usuario.objects.filter(username="ychingate").update(revisa_compras=True)
        self.preparar()
        estados = [o.estado for o in OrdenCompra.objects.filter(proveedor__razon_social="PRUEBA PROVEEDOR TATIANA").order_by("pk")]
        self.assertEqual(estados, [OrdenCompra.Estado.BORRADOR, OrdenCompra.Estado.LISTA])

    def test_no_duplica_y_rehacer_los_vuelve_a_crear(self):
        self.preparar()
        with self.assertRaises(CommandError):
            self.preparar()
        self.preparar("--rehacer")
        self.assertEqual(Ticket.objects.filter(asunto__startswith="PRUEBA Yuri · ").count(), 6)
        self.assertEqual(OrdenCompra.objects.count(), 4)

    def test_ensayo_no_deja_nada(self):
        self.preparar("--ensayo")
        self.assertFalse(Ticket.objects.exists())

    def test_eliminar_solo_borra_tickets_prueba(self):
        self.preparar()
        real = Ticket.objects.first()
        real.empresa.razon_social = "Cliente real"
        real.empresa.save()
        with self.assertRaises(CommandError):
            self.preparar("--rehacer", "--eliminar", real.radicado)
        self.assertTrue(Ticket.objects.filter(pk=real.pk).exists())
