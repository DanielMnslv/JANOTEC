"""Equipos que el técnico encuentra instalados en la sede (23/11/2026): del acta a la hoja de vida de la sede."""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from cronograma.models import EquipoInstalado

from .models import ActaSoporte, EquipoActa
from .tests import ActaBase


class EquiposExistentesTests(ActaBase):
    def existentes(self, *filas):
        datos = {"existentes-TOTAL_FORMS": str(len(filas)), "existentes-INITIAL_FORMS": "0",
                 "existentes-MIN_NUM_FORMS": "0", "existentes-MAX_NUM_FORMS": "30"}
        for n, fila in enumerate(filas):
            datos.update({f"existentes-{n}-{k}": v for k, v in fila.items()})
        return datos

    def registrar(self, *filas):
        self.client.force_login(self.agente)
        r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]),
                             self.datos_acta(**self.existentes(*filas)))
        if r.status_code != 302:
            c = r.context
            self.fail([c["form"].errors, c["equipos"].errors, c["existentes"].errors, c["material_errores"]])
        return ActaSoporte.objects.latest("numero")

    def test_el_acta_trae_la_lista_de_equipos_ya_instalados(self):
        self.client.force_login(self.agente)
        r = self.client.get(reverse("acta_nueva", args=[self.ticket.radicado]))
        self.assertContains(r, "Equipos que ya estaban instalados")
        self.assertContains(r, 'name="existentes-0-ubicacion"')
        self.assertNotContains(r, 'name="existentes-0-propiedad"')

    def test_carga_uno_por_uno_los_equipos_encontrados_en_la_sede(self):
        foto = SimpleUploadedFile("domo.jpg", bytes([255, 216, 255]) + b"0" * 50, content_type="image/jpeg")
        acta = self.registrar({"equipo": "Cámara domo", "marca": "Hikvision", "referencia": "DS-2CE56", "cantidad": "4",
                               "ubicacion": "Parqueadero", "estado_equipo": "operativo", "fotos": [foto]},
                              {"equipo": "DVR 16 canales", "marca": "Dahua", "serial": "DV123", "cantidad": "1",
                               "sistema_equipo": "cctv", "ubicacion": "Cuarto de monitoreo", "estado_equipo": "con_falla"})
        domos = EquipoInstalado.objects.filter(sede=self.sede_a, tipo="Cámara domo")
        self.assertEqual(domos.count(), 4)
        self.assertTrue(all(d.sistema == "cctv" and d.ubicacion == "Parqueadero" and d.creado_por == self.agente
                            and f"acta N° {acta.numero}" in d.observaciones for d in domos))
        dvr = EquipoInstalado.objects.get(serial="DV123")
        self.assertEqual(dvr.estado, "con_falla")
        existentes = acta.equipos_existentes
        self.assertEqual(len(existentes), 2)
        self.assertTrue(all(e.equipo_instalado_id for e in existentes))
        self.assertIn("Ubicación: Cuarto de monitoreo · Con falla", existentes[1].detalle)
        # Se ve en el detalle del acta y en la ficha de la sede
        self.assertContains(self.client.get(acta.get_absolute_url()), "Equipos que ya estaban instalados en la sede")
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(reverse("sede_detalle", args=[self.sede_a.pk])), "DVR 16 canales")

    def test_un_serial_ya_cargado_no_se_duplica(self):
        previo = EquipoInstalado.objects.create(sede=self.sede_a, sistema="cctv", tipo="NVR", serial="NV9")
        acta = self.registrar({"equipo": "NVR 32 canales", "marca": "Hikvision", "serial": "nv9", "cantidad": "1",
                               "ubicacion": "Rack", "estado_equipo": "fuera"})
        self.assertEqual(EquipoInstalado.objects.filter(sede=self.sede_a).count(), 1)
        previo.refresh_from_db()
        self.assertEqual((previo.tipo, previo.marca, previo.ubicacion, previo.estado), ("NVR", "Hikvision", "Rack", "fuera"))
        self.assertEqual(acta.equipos_existentes[0].equipo_instalado, previo)

    def test_filas_vacias_no_cargan_nada(self):
        self.registrar({"equipo": "", "estado_equipo": "operativo"})
        self.assertFalse(EquipoInstalado.objects.exists())
        self.assertFalse(EquipoActa.objects.filter(clase=EquipoActa.Clase.EXISTENTE).exists())
