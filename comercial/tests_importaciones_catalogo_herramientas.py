import csv
from datetime import datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from accounts.models import Usuario
from comercial.models import ItemCatalogo
from inventario.models import EntregaHerramienta, Herramienta


class ImportacionesTests(TestCase):
    def csv_temporal(self, columnas, filas, delimitador=","):
        carpeta = TemporaryDirectory()
        self.addCleanup(carpeta.cleanup)
        ruta = Path(carpeta.name) / "carga.csv"
        with ruta.open("w", encoding="utf-8-sig", newline="") as archivo:
            csv.writer(archivo, delimiter=delimitador).writerows([columnas, *filas])
        return str(ruta)

    def archivo_actas(self, *filas):
        return self.csv_temporal(
            ["acta", "renglon", "fecha", "responsable", "entregado_por", "tipo",
             "nombre", "marca", "serial", "cantidad", "descripcion"],
            [("100", indice, "2026-10-09 12:00:00", "Ana Pérez", "Bodega", tipo,
              nombre, "", serial, 1, "") for indice, (tipo, nombre, serial) in enumerate(filas, 1)],
        )

    def test_actas_simulacion_historial_y_recarga(self):
        tecnico = Usuario.objects.create(username="ana", first_name="Ana", last_name="Perez", rol=Usuario.Rol.AGENTE)
        ruta = self.archivo_actas(("herramienta", "Taladro", "ABC"), ("material", "Tornillo", ""))
        call_command("importar_actas_herramientas", ruta, stdout=StringIO())
        self.assertFalse(Herramienta.objects.exists())
        call_command("importar_actas_herramientas", ruta, confirmar=True, stdout=StringIO())
        entrega = EntregaHerramienta.objects.get()
        self.assertEqual(entrega.herramienta.tecnico, tecnico)
        self.assertEqual(timezone.localtime(entrega.fecha).replace(tzinfo=None), datetime(2026, 10, 9, 12))
        for _ in range(2):
            call_command("importar_actas_herramientas", ruta, confirmar=True,
                         incluir_materiales=True, stdout=StringIO())
        self.assertEqual(Herramienta.objects.count(), 2)
        self.assertEqual(EntregaHerramienta.objects.count(), 2)

    def test_actas_serial_existente_revierte_la_carga(self):
        Herramienta.objects.create(nombre="Equipo existente", serial="ABC")
        ruta = self.archivo_actas(("herramienta", "Equipo nuevo", "DEF"), ("herramienta", "Taladro", "abc"))
        with self.assertRaisesMessage(CommandError, "el serial abc ya figura"):
            call_command("importar_actas_herramientas", ruta, confirmar=True, stdout=StringIO())
        self.assertEqual(Herramienta.objects.count(), 1)
        self.assertFalse(EntregaHerramienta.objects.exists())

    def test_hikvision_actualiza_por_referencia_y_conserva_precio_bruto(self):
        item = ItemCatalogo.objects.create(descripcion="DVR existente", referencia="DS-TEST",
                                          marca="Hikvision", valor_unitario=1)
        ruta = self.csv_temporal(
            ["referencia", "descripcion_fuente", "estado", "preferencial_con_iva", "iva_pct", "hoja"],
            [("ds-test", "DVR", "candidato", "404764.22", 19, "HIKVISION TURBO +DVR"),
             ("TEST-2", "Consultar", "sin_precio_numerico", "", 19, "STORAGE")],
            ";",
        )
        call_command("importar_catalogo_hikvision", ruta, stdout=StringIO())
        item.refresh_from_db()
        self.assertEqual(item.valor_unitario, 1)
        for _ in range(2):
            call_command("importar_catalogo_hikvision", ruta, confirmar=True, stdout=StringIO())
        item.refresh_from_db()
        self.assertEqual(ItemCatalogo.objects.count(), 1)
        self.assertEqual(item.descripcion, "DVR existente")
        self.assertEqual(item.valor_unitario, Decimal("340138.00"))
        self.assertEqual(item.valor_unitario * (1 + item.iva / 100), Decimal("404764.22"))
