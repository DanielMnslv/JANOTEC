"""La propuesta y la orden de compra como las reales de JANOTEC (PE-2026-10-047-V2, Avícola del Magdalena, Girardot):

- La propuesta lleva el consecutivo del año (PE-AAAA-MM-NNN) y la firma «Elaboró» de quien la arma en administrativa.
- Cada ítem guarda proveedor, marca y referencia; con eso sale la orden de compra del proyecto: lo que hay en bodega
  sale de INVENTARIO (el NVR), lo demás se COMPRA, con el resumen por proveedor.
- La mano de obra subcontratada (SOLUCTEIN) se compra con su orden: no entra a bodega, se confirma prestada.
- Antes de dirección, la coordinación administrativa revisa la orden (firma «Revisó»).
"""
from decimal import Decimal

from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import Usuario
from comercial.models import Cotizacion, ItemCatalogo, PropuestaVersion
from comercial.tests import MEDIA_TMP, URGENTE, BaseComercial
from tickets.models import Ticket

from .models import Activo, OrdenCompra, Proveedor
from .proyecto import NO_APROBADA

NVR = "NVR 8 Megapixel (4K) / 32 canales IP / 4 Bahías de Disco Duro"
BALA = "Bala IP 4 Megapixel / Lente 2.8 mm / Color VU 3.0 / Exterior IP67"
PT = "Cámara PT motorizada de tres lentes, 4 MP, exterior IP66"
CABLE = "Cable FTP CAT 6+ calibre 23, exterior blindado"
MANO = "Suministro, instalación, configuración y puesta en funcionamiento de equipos para sistema de videovigilancia"


@override_settings(MEDIA_ROOT=MEDIA_TMP)
class PropuestaYOrdenRealesTests(BaseComercial):
    def setUp(self):
        super().setUp()
        self.syscom = Proveedor.objects.create(razon_social="SYSCOM", correo="ventas@syscom.test")
        self.gvs = Proveedor.objects.create(razon_social="GVS")
        self.soluctein = Proveedor.objects.create(razon_social="SOLUCTEIN")
        self.bodega = Usuario.objects.create_user("alejandra", "aleja@janotec.test", "Clave-segura-1", rol="bodega",
                                                  first_name="Alejandra", last_name="Vargas Tabares", cargo="Auxiliar administrativo")
        self.yuri = Usuario.objects.create_user("yuri", "yuri@janotec.test", "Clave-segura-1", rol="bodega", revisa_compras=True,
                                                first_name="Yuri Marcela", last_name="Chingate Silva", cargo="Coordinadora administrativa")
        self.tatiana = Usuario.objects.create_user("tatiana", "tati@janotec.test", "Clave-segura-1", rol="administrativo",
                                                   first_name="Tatiana", last_name="Medina", cargo="Auxiliar Admon")
        # El NVR ya está en bodega: sale de inventario
        Activo.objects.create(placa="NVR-0001", referencia="DS-7732NI-M4", descripcion="NVR 32 canales", tipo="NVR")

    # --- Ayudas ---
    def requerimiento(self):
        t = self.crear(asunto="Suministro e instalación de equipos para sistema de videovigilancia")
        self.client.post(reverse("comercial:informe", args=[t.radicado]), {"necesidad": "Videovigilancia en portería",
                                                                            "materiales": "1 NVR\n3 Bala"})
        return t

    def cotizar(self, t, filas, usuario=None, **extra):
        """Guarda la cotización con las filas (descripción, cant., costo, proveedor, marca, referencia)."""
        self.client.force_login(usuario or self.tatiana)
        datos = {"alcance": "Suministro e instalación de equipos para sistema de videovigilancia", "condiciones": "x",
                 "modalidad": "venta", "margen": "25", "tasa_mensual": "2", "recargo_alquiler": "2", "plazos": "12",
                 "opcion_compra": "0", "lineas-TOTAL_FORMS": str(len(filas)), "lineas-INITIAL_FORMS": "0",
                 "lineas-MIN_NUM_FORMS": "0", "lineas-MAX_NUM_FORMS": "1000", **extra}
        for i, (desc, cant, costo, prov, marca, ref) in enumerate(filas):
            datos.update({f"lineas-{i}-descripcion": desc, f"lineas-{i}-cantidad": cant, f"lineas-{i}-valor_unitario": costo,
                          f"lineas-{i}-descuento": "0", f"lineas-{i}-iva": "19", f"lineas-{i}-proveedor": prov.pk if prov else "",
                          f"lineas-{i}-marca": marca, f"lineas-{i}-referencia": ref})
        return self.client.post(reverse("comercial:cotizador", args=[t.radicado]), datos)

    def filas_reales(self):
        return [(NVR, "1", "1992385", self.syscom, "HIKVISION", "DS-7732NI-M4"),
                (PT, "1", "257742", self.gvs, "IMOU", "IPC-S7UN-11M0WED-IMOU"),
                (BALA, "3", "265080", self.syscom, "HIKVISION", "DS-2CD1047G3-LIU"),
                (CABLE, "380", "1841.25", self.syscom, "LINKEDPRO BY EPCOM", "PRO-CAT-6-EXT"),
                (MANO, "1", "4200383", self.soluctein, "", "")]

    def enviar_y_aprobar(self, t):
        self.client.force_login(self.tatiana)
        self.client.post(reverse("comercial:cotizador", args=[t.radicado]),
                         {**self._sin_lineas(t), **URGENTE, "accion": "enviar"})
        self.client.force_login(self.cli)
        self.client.post(reverse("comercial:responder", args=[t.radicado]), {"accion": "aprobar"})
        t.refresh_from_db()

    def _sin_lineas(self, t):
        """El formulario del cotizador con las líneas que ya tiene (para enviar sin cambiarlas)."""
        cot = t.ficha.cotizaciones.filter(version__isnull=True).get()
        datos = {"alcance": cot.alcance, "condiciones": cot.condiciones, "modalidad": cot.modalidad, "margen": "25",
                 "tasa_mensual": "2", "recargo_alquiler": "2", "plazos": "12", "opcion_compra": "0",
                 "lineas-TOTAL_FORMS": str(cot.lineas.count()), "lineas-INITIAL_FORMS": str(cot.lineas.count()),
                 "lineas-MIN_NUM_FORMS": "0", "lineas-MAX_NUM_FORMS": "1000"}
        for i, l in enumerate(cot.lineas.all()):
            datos.update({f"lineas-{i}-id": l.pk, f"lineas-{i}-cotizacion": cot.pk, f"lineas-{i}-descripcion": l.descripcion,
                          f"lineas-{i}-cantidad": l.cantidad, f"lineas-{i}-valor_unitario": l.valor_unitario,
                          f"lineas-{i}-descuento": "0", f"lineas-{i}-iva": "19", f"lineas-{i}-proveedor": l.proveedor_id or "",
                          f"lineas-{i}-marca": l.marca, f"lineas-{i}-referencia": l.referencia})
        return datos

    # --- Propuesta ---
    def test_consecutivo_del_anio_y_ajustable(self):
        hoy = timezone.localdate()
        t1 = self.requerimiento()
        self.cotizar(t1, self.filas_reales()[:1])
        self.client.post(reverse("comercial:cotizador", args=[t1.radicado]), {**self._sin_lineas(t1), **URGENTE, "accion": "enviar"})
        v1 = PropuestaVersion.objects.get(ficha=t1.ficha)
        self.assertEqual(v1.codigo, f"PE-{hoy:%Y-%m}-001-V1")
        # La siguiente sugiere el 002; administrativa la ajusta al consecutivo que lleva (047)
        t2 = self.requerimiento()
        self.cotizar(t2, self.filas_reales()[:1])
        self.assertContains(self.client.get(reverse("comercial:cotizador", args=[t2.radicado])), f"PE-{hoy:%Y-%m}-002-V1")
        self.cotizar(t2, [], consecutivo="47")
        self.assertContains(self.client.get(reverse("comercial:cotizador", args=[t2.radicado])), f"PE-{hoy:%Y-%m}-047-V1")
        self.client.post(reverse("comercial:cotizador", args=[t2.radicado]), {**self._sin_lineas(t2), "consecutivo": "47", **URGENTE,
                                                                             "accion": "enviar"})
        self.assertEqual(PropuestaVersion.objects.get(ficha=t2.ficha).codigo, f"PE-{hoy:%Y-%m}-047-V1")
        # La que sigue va en el 048, y el 047 no se puede repetir
        t3 = self.requerimiento()
        self.cotizar(t3, self.filas_reales()[:1])
        self.assertContains(self.client.get(reverse("comercial:cotizador", args=[t3.radicado])), f"PE-{hoy:%Y-%m}-048-V1")
        r = self.cotizar(t3, [], consecutivo="47")
        self.assertContains(r, "ya lo usa otra propuesta")

    def test_elaboro_es_quien_la_arma_y_cada_item_guarda_proveedor_marca_y_referencia(self):
        t = self.requerimiento()
        self.cotizar(t, self.filas_reales())
        cot = t.ficha.cotizaciones.get()
        self.assertEqual(cot.elaborada_por, self.tatiana)
        nvr = cot.lineas.get(descripcion=NVR)
        self.assertEqual((nvr.proveedor, nvr.marca, nvr.referencia), (self.syscom, "HIKVISION", "DS-7732NI-M4"))
        self.assertTrue(cot.lineas.get(descripcion=MANO).es_servicio)
        # Dirección la corrige: «Elaboró» sigue siendo Tatiana
        self.client.force_login(self.director)
        self.client.post(reverse("comercial:cotizador", args=[t.radicado]), self._sin_lineas(t))
        cot.refresh_from_db()
        self.assertEqual(cot.elaborada_por, self.tatiana)
        pdf = self.client.get(reverse("comercial:cotizacion_pdf", args=[t.radicado]))
        self.assertEqual(pdf["Content-Type"], "application/pdf")

    def test_el_catalogo_lleva_marca_referencia_y_proveedor(self):
        ItemCatalogo.objects.create(descripcion=BALA, valor_unitario=265080, marca="HIKVISION", referencia="DS-2CD1047G3-LIU",
                                    proveedor=self.syscom)
        t = self.requerimiento()
        self.client.force_login(self.tatiana)
        r = self.client.get(reverse("comercial:cotizador", args=[t.radicado]))
        self.assertContains(r, "DS-2CD1047G3-LIU")  # el cotizador lo llena al elegir el ítem

    # --- Orden de compra del proyecto ---
    def test_hoja_del_proyecto_antes_y_despues_de_aprobar(self):
        t = self.requerimiento()
        self.cotizar(t, self.filas_reales())
        self.client.force_login(self.bodega)
        url = reverse("inventario:orden_proyecto", args=[t.radicado])
        r = self.client.get(url)
        self.assertContains(r, NO_APROBADA)
        self.assertNotContains(r, "Preparar orden")
        self.enviar_y_aprobar(t)
        self.client.force_login(self.bodega)
        r = self.client.get(url)
        h = r.context["h"]
        self.assertTrue(h["aprobada"])
        self.assertEqual(h["numero"], "OC-" + h["id_proyecto"])
        decisiones = {f["descripcion"]: (f["decision"], f["comprar"]) for f in h["filas"]}
        self.assertEqual(decisiones[NVR], ("INVENTARIO", Decimal(0)))  # está en bodega
        self.assertEqual(decisiones[BALA], ("COMPRAR", Decimal(3)))
        self.assertEqual(decisiones[MANO], ("COMPRAR", Decimal(1)))
        resumen = {g["proveedor"]: g["items"] for g in h["resumen"]}
        self.assertEqual(resumen, {"GVS": 1, "SYSCOM": 2, "SOLUCTEIN": 1})
        # Precios del proveedor (costo), no los de la venta: 3 balas × 265.080
        bala = next(f for f in h["filas"] if f["descripcion"] == BALA)
        self.assertEqual(bala["base"], Decimal("795240"))
        pdf = self.client.get(reverse("inventario:orden_proyecto_pdf", args=[t.radicado]))
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        # Desde la ficha se llega a la hoja
        self.client.force_login(self.tatiana)
        self.assertContains(self.client.get(t.ficha.get_absolute_url()), url)

    def test_ordenes_por_proveedor_con_revision_y_mano_de_obra(self):
        t = self.requerimiento()
        self.cotizar(t, self.filas_reales())
        self.enviar_y_aprobar(t)
        self.client.force_login(self.bodega)
        ordenar = reverse("inventario:orden_proyecto_ordenar", args=[t.radicado])
        # SYSCOM: la bala y el cable (el NVR sale de bodega)
        self.client.post(ordenar, {"proveedor": self.syscom.pk})
        syscom = OrdenCompra.objects.get(proveedor=self.syscom)
        self.assertEqual(sorted(l.descripcion for l in syscom.lineas.all()), sorted([BALA, CABLE]))
        self.assertEqual(syscom.ticket, t)
        self.assertEqual(syscom.lineas.get(descripcion=BALA).referencia, "DS-2CD1047G3-LIU")
        self.client.post(ordenar, {"proveedor": self.syscom.pk})
        self.assertEqual(OrdenCompra.objects.filter(proveedor=self.syscom).count(), 1, "no se ordena dos veces")
        # SOLUCTEIN: la mano de obra, como servicio
        self.client.post(ordenar, {"proveedor": self.soluctein.pk})
        orden = OrdenCompra.objects.get(proveedor=self.soluctein)
        linea = orden.lineas.get()
        self.assertTrue(linea.es_servicio)
        orden.fecha_estimada, orden.forma_pago, orden.porcentaje_anticipo = timezone.localdate(), "anticipo", 50
        orden.save()
        # Alejandra la envía: pasa primero por la revisión de Yuri
        from . import financiera
        mail.outbox.clear()
        financiera.enviar_a_direccion(orden, self.bodega)
        self.assertEqual(orden.estado, OrdenCompra.Estado.POR_REVISAR)
        self.assertTrue(any("yuri@janotec.test" in m.to and "para revisar" in m.subject for m in mail.outbox))
        # Yuri la devuelve, Alejandra corrige y la reenvía; Yuri la revisa y pasa a dirección
        self.client.force_login(self.yuri)
        self.client.post(reverse("inventario:orden_paso", args=[orden.pk, "devolver_revision"]), {"observaciones": "Falta el acta"})
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.observaciones_revision), (OrdenCompra.Estado.BORRADOR, "Falta el acta"))
        financiera.enviar_a_direccion(orden, self.bodega)
        self.client.post(reverse("inventario:orden_paso", args=[orden.pk, "revisar"]))
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.revisada_por), (OrdenCompra.Estado.REVISION, self.yuri))
        financiera.aprobar(orden, self.director)
        orden.refresh_from_db()
        self.assertEqual(orden.estado, OrdenCompra.Estado.POR_PAGAR)  # el anticipo del 50 % antes del servicio
        from django.core.files.uploadedfile import SimpleUploadedFile
        financiera.registrar_pago(orden, self.director, timezone.localdate(), orden.valor_por_pagar, "Transferencia",
                                  SimpleUploadedFile("anticipo.pdf", b"%PDF-1.4 x", content_type="application/pdf"))
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.saldo_pendiente), (OrdenCompra.Estado.LISTA, True))
        # No se «recibe» en bodega: se confirma que SOLUCTEIN prestó el servicio
        self.client.force_login(self.bodega)
        self.assertNotContains(self.client.get(orden.get_absolute_url()), "Llegó: recibir")
        self.client.post(reverse("inventario:orden_paso", args=[orden.pk, "servicio"]), {"nota": "Acta firmada por el cliente"})
        orden.refresh_from_db()
        self.assertEqual(orden.estado, OrdenCompra.Estado.RECIBIDA)
        # La hoja muestra las tres firmas
        h = self.client.get(reverse("inventario:orden_proyecto", args=[t.radicado])).context["h"]
        self.assertIn(("ALEJANDRA VARGAS TABARES", "AUXILIAR ADMINISTRATIVO"), h["realizo"])
        self.assertIn(("YURI MARCELA CHINGATE SILVA", "COORDINADORA ADMINISTRATIVA"), h["reviso"])
        self.assertEqual(len(h["aprobo"]), 1)
        self.assertEqual(self.client.get(reverse("inventario:orden_archivo", args=[orden.pk, "pdf"]))["Content-Type"],
                         "application/pdf")

    def test_quien_revisa_la_envia_directo_a_direccion(self):
        from . import financiera
        orden = OrdenCompra.objects.create(proveedor=self.syscom, creado_por=self.yuri, fecha_estimada=timezone.localdate())
        orden.lineas.create(descripcion="Conector RJ45", cantidad=60, valor_unitario=890)
        financiera.enviar_a_direccion(orden, self.yuri)
        self.assertEqual((orden.estado, orden.revisada_por), (OrdenCompra.Estado.REVISION, self.yuri))

    def test_sin_quien_revise_sigue_como_antes(self):
        from . import financiera
        Usuario.objects.filter(revisa_compras=True).update(revisa_compras=False)
        orden = OrdenCompra.objects.create(proveedor=self.syscom, creado_por=self.bodega, fecha_estimada=timezone.localdate())
        orden.lineas.create(descripcion="Conector RJ45", cantidad=60, valor_unitario=890)
        financiera.enviar_a_direccion(orden, self.bodega)
        self.assertEqual((orden.estado, orden.revisada_por), (OrdenCompra.Estado.REVISION, None))

    def test_solo_quien_ve_compras_entra_a_la_hoja(self):
        t = self.requerimiento()
        self.cotizar(t, self.filas_reales())
        self.client.force_login(self.tecnico)
        self.assertEqual(self.client.get(reverse("inventario:orden_proyecto", args=[t.radicado])).status_code, 403)
        self.assertIsInstance(Cotizacion.objects.first(), Cotizacion)
        self.assertTrue(Ticket.objects.exists())
