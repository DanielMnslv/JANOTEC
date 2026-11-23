"""Ejemplos de redacción (23/11/2026): se enseñan en su página y «Mejorar redacción» se los muestra a Claude."""

import os
from types import SimpleNamespace
from unittest import mock

from django.urls import reverse

from . import redaccion
from .models import EjemploRedaccion
from .tests import BaseTickets


class EjemplosRedaccionTests(BaseTickets):
    """Quién enseña, qué ejemplos se eligen y que lleguen a las instrucciones de la mejora."""

    def _ejemplo(self, aplica="general", titulo="Ejemplo", original="texto", mejorado="Texto.", activo=True):
        return EjemploRedaccion.objects.create(titulo=titulo, aplica=aplica, original=original, mejorado=mejorado,
                                               activo=activo)

    def test_solo_coordinacion_o_administrativa_ensena(self):
        url = reverse("redaccion_ejemplos")
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(url).status_code, 403)
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(url).status_code, 200)
        r = self.client.post(url, {"titulo": "Informe por áreas", "aplica": "informe", "original": "1 camara",
                                   "mejorado": "• Instalación de una cámara.", "activo": "on"})
        self.assertRedirects(r, url)
        ejemplo = EjemploRedaccion.objects.get()
        self.assertEqual((ejemplo.aplica, ejemplo.creado_por), ("informe", self.supervisor))
        self.client.post(reverse("redaccion_ejemplo_eliminar", args=[ejemplo.pk]))
        self.assertFalse(EjemploRedaccion.objects.exists())

    def test_reconoce_informes_por_el_nombre_o_por_ser_una_lista(self):
        self.assertEqual(redaccion.aplica_para("diagnostico"), ["diagnostico", "general"])
        self.assertEqual(redaccion.aplica_para("general", "Requerimientos de la visita", "Ticket"), ["informe", "general"])
        self.assertEqual(redaccion.aplica_para("", "Alcance", "Propuesta"), ["propuesta", "general"])
        self.assertEqual(redaccion.aplica_para("", "Notas", "Ticket", "a\nb\nc\nd\ne\nf"), ["informe", "general"])
        self.assertEqual(redaccion.aplica_para("", "Notas", "Ticket", "una línea"), ["general"])

    def test_elige_los_especificos_primero_solo_activos_y_con_tope(self):
        general = self._ejemplo("general", "General")
        informe = self._ejemplo("informe", "Informe")
        self._ejemplo("informe", "Apagado", activo=False)
        self._ejemplo("diagnostico", "Otro tipo")
        self.assertEqual(redaccion.ejemplos_para(["informe", "general"]), [informe, general])
        largo = self._ejemplo("informe", "Muy largo", mejorado="x" * redaccion.EJEMPLOS_CARACTERES)
        self.assertNotIn(largo, redaccion.ejemplos_para(["informe", "general"]))

    def test_la_mejora_lleva_los_ejemplos_en_las_instrucciones(self):
        self._ejemplo("informe", "Informe por áreas", "1 camara poste", "1. Oficinas\n• Instalación de una cámara.")
        self.client.force_login(self.agente)
        with mock.patch.dict(os.environ, {"REDACCION_API_KEY": "clave-de-prueba"}), \
                mock.patch("anthropic.Anthropic") as cliente:
            crear = cliente.return_value.messages.create
            crear.return_value = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="Listo.")])
            r = self.client.post(reverse("mejorar_redaccion"), {"texto": "1 camara bodega", "tipo": "general",
                                                                "campo": "Requerimientos", "pagina": "Informe"})
        self.assertEqual(r.json()["texto"], "Listo.")
        sistema = crear.call_args.kwargs["system"]
        self.assertIn("<como_debe_quedar>\n1. Oficinas\n• Instalación de una cámara.", sistema)
        self.assertIn("Conclusiones", sistema)  # las reglas de informe siguen ahí

    def test_sin_ejemplos_quedan_solo_las_instrucciones(self):
        self.assertEqual(redaccion.instrucciones_con_ejemplos([]), redaccion.INSTRUCCIONES)
        self.assertNotIn("**", redaccion.INSTRUCCIONES.replace("sin ** ni", ""))


class AprendizajeTests(BaseTickets):
    """Lo aprendido del historial (anonimizado) y de las correcciones de los usuarios."""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()

    def test_anonimiza_clientes_sedes_personas_y_datos_de_contacto(self):
        from .aprendizaje import anonimizar
        texto = ("Visita a POLIGROW Colombia S.A.S. sede Mapiripán con Juan Pérez, NIT 900.123.456-7, "
                 "cel 310 555 1234, correo juan@poligrow.co. Sede principal sin novedad.")
        r = anonimizar(texto, ["Poligrow Colombia S.A.S."], ["Mapiripan", "Principal"], ["Juan Perez"])
        for dato in ("POLIGROW", "Mapiripán", "Juan", "900.123", "310", "juan@"):
            self.assertNotIn(dato, r)
        for marca in ("[CLIENTE]", "[SEDE]", "[PERSONA]", "[NIT]", "[TELÉFONO]", "[CORREO]"):
            self.assertIn(marca, r)
        self.assertIn("Sede principal", r)  # los nombres genéricos de sede no se tocan

    def test_busca_actas_parecidas_y_las_manda_sin_el_nombre_del_cliente(self):
        from django.utils import timezone

        from . import aprendizaje
        from .models import ActaSoporte
        ActaSoporte.objects.create(
            numero=900001, fecha=timezone.now(), empresa=self.emp_a, sede=self.sede_a, sistema="CCTV",
            observaciones="Se realizó mantenimiento preventivo al DVR de Empresa A; se reemplazó el conector BNC "
                          "de la cámara de portería y la fuente de poder. El sistema quedó operativo.")
        docs = aprendizaje.documentos_parecidos(["resultado", "general"], "se cambio conector BNC camara porteria dvr")
        self.assertEqual(len(docs), 1)
        self.assertNotIn("Empresa A", docs[0]["texto"])
        self.assertIn("[CLIENTE]", docs[0]["texto"])
        self.assertEqual(aprendizaje.documentos_parecidos(["propuesta"], "conector BNC"), [])

    def test_la_correccion_queda_por_revisar_y_se_aprueba(self):
        url = reverse("redaccion_correccion")
        self.client.force_login(self.agente)
        datos = {"original": "camara sin imagen", "propuesta": "La cámara no tenía imagen.",
                 "final": "La cámara de portería no presentaba imagen.", "tipo": "diagnostico", "campo": "Diagnóstico"}
        self.assertEqual(self.client.post(url, datos).status_code, 204)
        self.client.post(url, {**datos, "final": datos["propuesta"]})  # sin cambios: no se aprende nada
        e = EjemploRedaccion.objects.get()
        self.assertEqual((e.origen, e.activo, e.aplica, e.mejorado), ("correccion", False, "diagnostico", datos["final"]))
        self.assertEqual(redaccion.ejemplos_para(["diagnostico", "general"]), [])
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(reverse("redaccion_ejemplos")), "Correcciones por revisar")
        self.client.post(reverse("redaccion_ejemplo_aprobar", args=[e.pk]))
        self.assertEqual(redaccion.ejemplos_para(["diagnostico", "general"]), [e])

    def test_las_instrucciones_llevan_documentos_y_vocabulario(self):
        sistema = redaccion.instrucciones_con_ejemplos(
            [], [{"titulo": "Resultado de un acta", "texto": "Se reemplazó la fuente de [CLIENTE]."}],
            ["Fuente 12 V 5 A"])
        self.assertIn("<documento_aprobado", sistema)
        self.assertIn("- Fuente 12 V 5 A", sistema)
