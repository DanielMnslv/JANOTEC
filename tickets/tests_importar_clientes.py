"""Importar la relación de clientes y sedes en Excel (una fila por sede)."""
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from openpyxl import Workbook

from accounts.models import EncargadoCliente, Empresa, Sede, Usuario
from cronograma.models import EquipoInstalado

from . import importar_clientes as imp
from .tests import BaseTickets

ENCABEZADO = ["NIT", "CLIENTE", "SEDE", "CIUDAD", "DIRECCIÓN", "ABONADO", "SISTEMA DE ALARMA", "VIDEOVIGILANCIA",
              "COMERCIAL", "PERSONA CONTACTO", "NÚMERO CONTACTO", "N° DE LÍNEA", "OPERADOR", "TIPO DE LÍNEA"]


def relacion():
    """Una relación como la real: cliente ya existente (Empresa A, NIT 1) y uno nuevo con dos sedes."""
    wb = Workbook()
    ws = wb.active
    ws.append(ENCABEZADO)
    # La sede «Principal» de Empresa A, escrita distinto pero con el mismo abonado que ya tiene
    ws.append([1, "EMPRESA A SAS", "OFICINA PRINCIPAL", "VILLAVICENCIO", "CALLE 1 # 2-3", 5529, "PARADOX SP6000", "",
               "JESSICA AGUDELO", "ROGER ENCISO", "321 4693300", "8910300000003501858", "SOLUTEC", "VERTICAL"])
    ws.append([900298862, "BIOAGRICOLA DEL LLANO S.A", "CORALINA", "PUERTO LOPEZ", "CALLE 3 NO. 32-30", 7129, "DSC",
               "HIKVISION NVR DS-7616NI-Q2 - 11 CÁMARAS IP", "DERLY LEON", "LYA MEJIA", "300 8126483", "", "", ""])
    ws.append([900298862, "BIOAGRICOLA DEL LLANO S.A", "ROCHELA", "YOPAL", "", None, "", "", "DERLY LEON", "", "", "", "", ""])
    ws.append([None, None, None, None, None, None, None, None, None, None, None, None, None, None])
    salida = io.BytesIO()
    wb.save(salida)
    salida.seek(0)
    return salida


class ImportarClientesTests(BaseTickets):
    def setUp(self):
        super().setUp()
        self.sede_a.abonado = "5529"
        self.sede_a.direccion = "Ya escrita"
        self.sede_a.save()
        self.url = reverse("importar_clientes")

    def test_lee_equipos_y_municipios(self):
        filas = imp.leer(relacion())
        self.assertEqual(len(filas), 3)
        panel, nvr = imp.equipos_de(filas[1])
        self.assertEqual((panel["tipo"], panel["marca"], panel["modelo"]), ("Panel de alarma", "DSC", ""))
        self.assertEqual((nvr["tipo"], nvr["marca"], nvr["modelo"], nvr["caracteristicas"]),
                         ("NVR", "HIKVISION", "DS-7616NI-Q2", "11 cámaras IP"))
        self.assertIn("Línea 8910300000003501858 · SOLUTEC · Vertical", imp.equipos_de(filas[0])[0]["caracteristicas"])
        self.assertEqual(imp.municipio("PUERTO LOPEZ"), ("Puerto Lopez", "Meta"))
        self.assertEqual(imp.municipio("YOPAL")[1], "Casanare")

    def test_revisar_no_guarda_e_importar_crea_y_completa_sin_pisar(self):
        self.client.force_login(self.supervisor)
        r = self.client.post(self.url, {"archivo": SimpleUploadedFile("clientes.xlsx", relacion().read())})
        plan = r.context["plan"]
        self.assertEqual([c["razon_social"] for c in plan["nuevos_clientes"]], ["BIOAGRICOLA DEL LLANO S.A"])
        self.assertEqual([s["nombre"] for s in plan["nuevas_sedes"]], ["CORALINA", "ROCHELA"])
        self.assertEqual(plan["sedes_completar"][0]["sede"], self.sede_a)  # encontrada por el abonado
        self.assertEqual(plan["equipos"], 3)
        self.assertFalse(Empresa.objects.filter(nit="900298862").exists())

        r = self.client.post(self.url, {"accion": "importar", "token": r.context["token"]}, follow=True)
        self.assertContains(r, "1 clientes y 2 sedes nuevas, 1 sedes completadas y 3 equipos cargados")
        self.sede_a.refresh_from_db()
        self.assertEqual((self.sede_a.nombre, self.sede_a.direccion, self.sede_a.contacto_nombre, self.sede_a.ciudad),
                         ("Principal", "Ya escrita", "Roger Enciso", "Villavicencio"))
        bio = Empresa.objects.get(nit="900298862")
        self.assertEqual(bio.encargado.nombre, "Derly Leon")
        coralina = Sede.objects.get(empresa=bio, nombre="CORALINA")
        self.assertEqual((coralina.ciudad, coralina.departamento, coralina.abonado), ("Puerto Lopez", "Meta", "7129"))
        self.assertEqual(Sede.objects.get(empresa=bio, nombre="ROCHELA").departamento, "Casanare")
        self.assertEqual(EquipoInstalado.objects.filter(sede=coralina).count(), 2)
        self.assertTrue(EquipoInstalado.objects.filter(sede=self.sede_a, sistema="alarma", marca="PARADOX").exists())
        self.assertEqual(self.emp_a.encargado.nombre, "Jessica Agudelo")

        # Subirlo otra vez no repite nada
        r = self.client.post(self.url, {"archivo": SimpleUploadedFile("clientes.xlsx", relacion().read())})
        plan = r.context["plan"]
        self.assertEqual((len(plan["nuevos_clientes"]), len(plan["nuevas_sedes"]), plan["equipos"]), (0, 0, 0))
        self.client.post(self.url, {"accion": "importar", "token": r.context["token"]})
        self.assertEqual((EquipoInstalado.objects.count(), EncargadoCliente.objects.count()), (3, 2))

    def test_bodega_importa_datos_pero_no_equipos(self):
        bodega = Usuario.objects.create_user("bodega1", "bodega1@janotec.test", "Clave-segura-1", rol="bodega")
        self.client.force_login(bodega)
        r = self.client.post(self.url, {"archivo": SimpleUploadedFile("clientes.xlsx", relacion().read())})
        self.assertEqual(r.context["plan"]["equipos"], 0)
        self.client.post(self.url, {"accion": "importar", "token": r.context["token"]})
        self.assertTrue(Sede.objects.filter(nombre="CORALINA").exists())
        self.assertFalse(EquipoInstalado.objects.exists())

    def test_el_tecnico_no_importa(self):
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(self.url).status_code, 403)
