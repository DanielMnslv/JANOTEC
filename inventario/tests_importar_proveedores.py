"""Importar el listado de proveedores en Excel (NIT, razón social, dirección, ciudad y contacto)."""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from . import importar_proveedores as imp
from .models import AsesorProveedor, Proveedor
from .tests import BaseInventario


def listado():
    """Un listado como el real: título arriba, encabezado en la fila 4."""
    wb = Workbook()
    ws = wb.active
    ws.append(["Relación de proveedores 2026"])
    ws.append([])
    ws.append([])
    ws.append(["NIT / Cédula", "Nombre / Razón social (RUT)", "Dirección", "Ciudad", "Nombre contacto"])
    ws.append(["901192317", "SYSCOM COLOMBIA SAS", "CL 10 20 30", "Bogotá", "ANDRES BALLEN"])
    ws.append(["900371527", "AK SECURITY SAS", "CL 38 33 79", "Villavicencio", "AK SECURITY SAS"])
    ws.append(["901013210", "MYM SYSTECH S.A.S.", "", "Villavicencio", "MYM SYSTECH SAS"])
    ws.append(["901013210", "MYM SYSTECH S.A.S.", "", "Villavicencio", "MYM SYSTECH SAS"])  # repetido
    ws.append([None, None, None, None, None])
    salida = io.BytesIO()
    wb.save(salida)
    salida.seek(0)
    return salida


class ImportarProveedoresTests(BaseInventario):
    def setUp(self):
        super().setUp()
        self.syscom = Proveedor.objects.create(razon_social="Syscom Colombia", direccion="Ya escrita")

    def test_el_contacto_que_es_la_misma_empresa_no_es_asesor(self):
        self.assertTrue(imp.es_persona("ANDRES BALLEN", "SYSCOM COLOMBIA SAS"))
        self.assertFalse(imp.es_persona("MYM SYSTECH SAS", "MYM SYSTECH S.A.S."))
        self.assertFalse(imp.es_persona("Precoll Del Llano", "PRECOLL S.A.S."))
        self.assertFalse(imp.es_persona("", "AK SECURITY SAS"))

    def test_revisar_no_guarda_e_importar_crea_y_completa_sin_pisar(self):
        self.client.force_login(self.bodega)
        url = reverse("inventario:importar_proveedores")
        r = self.client.post(url, {"archivo": SimpleUploadedFile("proveedores.xlsx", listado().read())})
        plan = r.context["plan"]
        self.assertEqual(plan["total"], 3)
        self.assertEqual([x["fila"]["razon_social"] for x in plan["nuevos"]], ["AK SECURITY SAS", "MYM SYSTECH S.A.S."])
        self.assertEqual(plan["completar"][0]["cambios"], ["NIT", "asesor"])
        self.assertEqual(Proveedor.objects.count(), 1)

        r = self.client.post(url, {"accion": "importar", "token": r.context["token"]}, follow=True)
        self.assertContains(r, "2 proveedores nuevos, 1 completados y 1 asesores")
        self.syscom.refresh_from_db()
        self.assertEqual((self.syscom.nit, self.syscom.direccion), ("901192317", "Ya escrita"))
        self.assertEqual(self.syscom.asesor.nombre, "Andres Ballen")
        ak = Proveedor.objects.get(nit="900371527")
        self.assertEqual(ak.direccion, "CL 38 33 79, Villavicencio")
        self.assertFalse(ak.asesores.exists())
        self.assertEqual(Proveedor.objects.get(nit="901013210").direccion, "Villavicencio")

        # Subirlo otra vez no repite nada
        r = self.client.post(url, {"archivo": SimpleUploadedFile("proveedores.xlsx", listado().read())})
        self.assertEqual((len(r.context["plan"]["nuevos"]), len(r.context["plan"]["completar"])), (0, 0))
        self.client.post(url, {"accion": "importar", "token": r.context["token"]})
        self.assertEqual((Proveedor.objects.count(), AsesorProveedor.objects.count()), (3, 1))

    def test_solo_bodega_direccion_y_financiera(self):
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(reverse("inventario:importar_proveedores")).status_code, 403)

    def test_archivo_sin_columnas(self):
        wb = Workbook()
        wb.active.append(["Producto", "Cantidad"])
        salida = io.BytesIO()
        wb.save(salida)
        self.client.force_login(self.bodega)
        r = self.client.post(reverse("inventario:importar_proveedores"),
                             {"archivo": SimpleUploadedFile("x.xlsx", salida.getvalue())}, follow=True)
        self.assertContains(r, "No se encontró ninguna hoja")
