"""Importar el Excel «Compras y facturas»: proveedores con NIT e historial de compras en la ficha del proveedor."""
import io
from datetime import date, datetime
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from . import importar_compras as imp
from .models import CompraHistorica, Proveedor
from .tests import BaseInventario

ENLACE = "https://drive.google.com/file/d/abc/view"


def libro(extra_feb=()):
    """Un Excel como el real: Terceros, la hoja de 2025 y la de feb.–dic. 2026 con filas del formato viejo y del nuevo."""
    wb = Workbook()
    t = wb.active
    t.title = "TERCEROS"
    t.append(["NIT", "RAZON SOCIAL"])
    t.append([900795612.0, "GRUPO DEFA SAS"])
    t.append([900857120.0, "DEVELOPMENT SYSTEM S.A.S"])
    t.append([1006529823.0, "SAENZ CALDERON IRIS"])  # tercero sin compras: no se crea
    v = wb.create_sheet("FACTURAS - 2025-ENERO 2026")
    v.append(["NIT", "RAZON SOCIAL", "FECHA FACTURA", "N° FACTURA", "VALOR FACTURA", "FECHA DE PAGO", "ANTICIPO",
              "ENLACE DE FACTURA", "OBSERVACIONES COMPRAS", "OBSERVACIONES FINANCIERA"])
    v.append([900795612.0, "GRUPO DEFA SAS ", datetime(2025, 7, 2), 47156.0, 452200.0, None, None, ENLACE,
              "Suministro SEMAG SAS", None])
    v.append([901192317.0, "SYSCOM COLOMBIA", datetime(2025, 8, 1), 1033.0, " $1,297,860 ", datetime(2025, 8, 5), "X",
              ENLACE, "Contrato Union Temporal Arauca", "Pagado"])
    v.append([None, None, None, None, None, None, None, None, None, None])
    f = wb.create_sheet("FACTURAS FEBRERO-DIC 2026")
    f.append(["NIT", "RAZON SOCIAL", "FECHA COTIZACIÓN", "NO. COTIZACIÓN", "VALOR COTIZACIÓN", "ENLACE DE COTIZACIÓN",
              "ENLACE DE ORDEN DE COMPRA", "OBSERVACIONES COMPRAS", "LLEGO PEDIDO", "SUBTOTAL", "IVA", "RETENCION",
              "VALOR A PAGAR", "Saldo a favor", "VALOR FACTURA", "ESTADO DE FACTURA", "FECHA DE PAGO", "OBSERVACIONES"])
    # Formato viejo (feb.–abr.): fecha de factura en «Subtotal», número en «Saldo a favor», enlace en «Estado».
    f.append([900857120.0, None, datetime(2026, 2, 4), None, None, None, None, None, None, 46057.0, None, None, None,
              815.0, 858600.0, ENLACE, None, None])
    # Formato nuevo.
    f.append([901192317.0, "SYSCOM COLOMBIA", datetime(2026, 6, 25), "Cotizacion\n01547", 990392.87, ENLACE, None,
              "Para Stock", "SI", 832262.916, 158129.954, 20806.5729, 969586.2971, None, None, None, datetime(2026, 6, 26), None])
    f.append([None, "$600,000.00", None, None, None, None, None, None, None, None, None, None, None, None, None, None, None, None])
    for fila in extra_feb:
        f.append(fila)
    salida = io.BytesIO()
    wb.save(salida)
    salida.seek(0)
    return salida


class ImportarComprasExcelTests(BaseInventario):
    def setUp(self):
        super().setUp()
        # Ya existe con otro formato de NIT y otro sin NIT: no se duplican.
        self.syscom = Proveedor.objects.create(razon_social="Syscom Colombia", nit="901.192.317-5")
        self.defa = Proveedor.objects.create(razon_social="Grupo Defa S.A.S.")

    def test_lee_las_hojas_y_entiende_el_formato_viejo(self):
        datos = imp.leer(libro())
        self.assertEqual(datos.hojas, {"FACTURAS - 2025-ENERO 2026": 2, "FACTURAS FEBRERO-DIC 2026": 2})
        vieja = next(f for f in datos.filas if f["nit"] == "900857120")
        self.assertEqual(vieja["razon_social"], "DEVELOPMENT SYSTEM S.A.S")  # tomada de Terceros
        self.assertEqual((vieja["fecha_factura"], vieja["factura"], vieja["valor_factura"], vieja["enlace_factura"]),
                         (date(2026, 2, 4), "815", Decimal("858600.0"), ENLACE))
        self.assertNotIn("subtotal", vieja)
        nueva = next(f for f in datos.filas if f.get("cotizacion") == "Cotizacion 01547")
        self.assertEqual((nueva["retencion"], nueva["fecha_pago"], nueva["llego_pedido"]),
                         (Decimal("20806.57"), date(2026, 6, 26), "SI"))
        pagada = next(f for f in datos.filas if f.get("factura") == "1033")
        self.assertEqual((pagada["valor_factura"], pagada["anticipo"]), (Decimal("1297860.00"), True))
        self.assertEqual(len(datos.ignoradas), 1)
        self.assertIn("corrida", datos.ignoradas[0][2])

    def test_revisar_no_guarda_e_importar_crea_y_completa(self):
        self.client.force_login(self.bodega)
        url = reverse("inventario:importar_compras")
        r = self.client.post(url, {"archivo": SimpleUploadedFile("compras.xlsx", libro().read())})
        plan = r.context["plan"]
        self.assertEqual([x["razon_social"] for x in plan["nuevos"]], ["DEVELOPMENT SYSTEM S.A.S"])
        self.assertEqual([(x["proveedor"], x["nit"]) for x in plan["con_nit"]], [(self.defa, "900795612")])
        self.assertEqual(CompraHistorica.objects.count(), 0)
        self.assertEqual(Proveedor.objects.count(), 2)

        r = self.client.post(url, {"accion": "importar", "token": r.context["token"]}, follow=True)
        self.assertContains(r, "4 compras en el historial")
        self.assertEqual(Proveedor.objects.count(), 3)
        self.defa.refresh_from_db()
        self.assertEqual(self.defa.nit, "900795612")
        self.assertEqual(self.syscom.compras_historicas.count(), 2)
        self.assertFalse(Proveedor.objects.filter(razon_social__icontains="SAENZ").exists())

        # Volver a subirlo (actualizado) reemplaza el historial: nada se repite.
        otra = [901192317.0, "SYSCOM COLOMBIA", datetime(2026, 11, 11), "COT 3132", 916905.75, ENLACE, None,
                "Alquiler - Potenza", None, None, None, None, None, None, None, None, None, None]
        r = self.client.post(url, {"archivo": SimpleUploadedFile("compras.xlsx", libro([otra]).read())})
        self.client.post(url, {"accion": "importar", "token": r.context["token"]})
        self.assertEqual(CompraHistorica.objects.count(), 5)
        self.assertEqual(Proveedor.objects.count(), 3)
        # El token ya se usó: el archivo temporal se borró.
        r = self.client.post(url, {"accion": "importar", "token": r.context["token"]}, follow=True)
        self.assertContains(r, "ya no está")

    def test_la_ficha_muestra_el_historial_solo_a_quien_maneja_compras(self):
        self.client.force_login(self.bodega)
        imp.aplicar(imp.leer(libro()), self.bodega)
        url = reverse("inventario:proveedor", args=[self.syscom.pk])
        r = self.client.get(url)
        self.assertContains(r, "Historial de compras del Excel (2)")
        self.assertContains(r, "Contrato Union Temporal Arauca")
        self.assertContains(r, "Fact. 1033")
        self.assertContains(r, "26/06/2026")
        self.client.force_login(self.agente)  # el técnico ve la ficha pero no los valores ni los pagos
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "Historial de compras del Excel")
        self.assertEqual(self.client.get(reverse("inventario:importar_compras")).status_code, 403)

    def test_archivo_que_no_es_excel(self):
        self.client.force_login(self.bodega)
        r = self.client.post(reverse("inventario:importar_compras"),
                             {"archivo": SimpleUploadedFile("compras.xlsx", b"no es un excel")}, follow=True)
        self.assertContains(r, "No se pudo leer el archivo")
        self.assertEqual(CompraHistorica.objects.count(), 0)
