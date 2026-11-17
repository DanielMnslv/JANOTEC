"""Comando limpiar_pruebas: conserva lo de jguana y el área técnica, borra lo de prueba y deja respaldo."""
import gzip
import io
import json
import shutil
import tempfile
from datetime import date
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from accounts.models import Usuario
from comercial.models import ItemCatalogo
from inventario.models import Insumo, MovimientoInsumo, Proveedor

from .models import Categoria, Empresa, Ticket


class LimpiarPruebasTests(TestCase):
    def setUp(self):
        self.carpeta = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.carpeta, ignore_errors=True)
        cat = Categoria.objects.create(nombre="CCTV")
        real = Empresa.objects.create(razon_social="Cliente real", nit="1")
        self.jguana = Usuario.objects.create_user("jguana", "j@x.co", "clave-prueba-123", rol="supervisor")
        asesor = Usuario.objects.create_user("asesor", "a@x.co", "clave-prueba-123", rol="comercial")
        bodega = Usuario.objects.create_user("bodega", "b@x.co", "clave-prueba-123", rol="bodega")
        cli = Usuario.objects.create_user("cli", "c@x.co", "clave-prueba-123", rol="cliente", empresa=real)
        base = dict(empresa=real, solicitante=cli, categoria=cat, descripcion="y")
        self.tecnico = Ticket.objects.create(asunto="Cámara", tipo="soporte", **base)
        self.de_jguana = Ticket.objects.create(asunto="Levantamiento", tipo="requerimiento", radicado_por=self.jguana, **base)
        self.comercial = Ticket.objects.create(asunto="Prueba comercial", tipo="requerimiento", radicado_por=asesor, **base)
        ItemCatalogo.objects.create(descripcion="Cámara domo", valor_unitario=100)
        Proveedor.objects.create(razon_social="Proveedor de prueba")
        hecho_por_jguana = Insumo.objects.create(codigo="A1", descripcion="Cable")
        de_bodega = Insumo.objects.create(codigo="B1", descripcion="Conector")
        for insumo, quien in ((hecho_por_jguana, self.jguana), (de_bodega, bodega)):
            MovimientoInsumo.objects.create(insumo=insumo, cantidad=5, fecha=date(2026, 11, 1), tipo="compra",
                                            origen="proveedor", destino="bodega", registrado_por=quien)

    def correr(self, *args):
        salida = io.StringIO()
        call_command("limpiar_pruebas", *args, "--carpeta", self.carpeta, stdout=salida)
        return salida.getvalue()

    def test_sin_confirmar_no_borra_nada(self):
        texto = self.correr()
        self.assertIn("SIMULACIÓN", texto)
        self.assertEqual((Ticket.objects.count(), ItemCatalogo.objects.count(), Insumo.objects.count()), (3, 1, 2))

    def test_confirmar_respalda_y_deja_solo_lo_real(self):
        self.correr("--confirmar")
        self.assertEqual(set(Ticket.objects.values_list("pk", flat=True)), {self.tecnico.pk, self.de_jguana.pk})
        self.assertFalse(ItemCatalogo.objects.exists())
        self.assertFalse(Proveedor.objects.exists())
        self.assertEqual(list(Insumo.objects.values_list("codigo", flat=True)), ["A1"])
        self.assertEqual(MovimientoInsumo.objects.get().registrado_por, self.jguana)
        respaldo = list(Path(self.carpeta).glob("antes_de_limpiar_*.json.gz"))
        self.assertEqual(len(respaldo), 1)
        datos = json.load(gzip.open(respaldo[0], "rt", encoding="utf-8"))
        guardados = {(d["model"], d["pk"]) for d in datos}
        self.assertIn(("tickets.ticket", self.comercial.pk), guardados)
        self.assertIn("comercial.itemcatalogo", {d["model"] for d in datos})
