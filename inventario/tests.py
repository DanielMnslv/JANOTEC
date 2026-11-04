import os
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from accounts.models import Empresa, Usuario
from tickets.models import ActaSoporte, Ticket
from tickets.tests import ActaBase

from . import servicios
from .models import Activo, Insumo, Lugar, Modalidad, MovimientoActivo, MovimientoInsumo

E = Activo.Estado
T = MovimientoActivo.Tipo


class BaseInventario(ActaBase):
    def setUp(self):
        super().setUp()
        self.bodega = Usuario.objects.create_user("bodega", "b@janotec.test", "Clave-segura-1", rol="bodega",
                                                  first_name="Alejandra", last_name="Vargas Tabares")
        self.camara = Activo.objects.create(placa="CAM-BAL-2MP002", serial="FZ8608366", categoria="CCTV", tipo="Cámara",
                                            subtipo="Bala", marca="Hikvisión", referencia="DS2CD1027G2HLIU",
                                            valor=Decimal("159095.58"))
        self.cable = Insumo.objects.create(codigo="CAB-UTP-EXT", descripcion="Cable UTP exterior", unidad="Metro")
        servicios.mover_insumo(self.cable, 305, T.COMPRA, Lugar.PROVEEDOR, Lugar.BODEGA, self.bodega,
                               valor_unitario=Decimal("1000"), iva=19, factura="227065")

    def solicitud_aprobada(self, cantidad="40", camaras=1):
        """Lo que exige bodega para entregar a un cliente: una solicitud con sus firmas."""
        from .models import ItemSolicitud, SolicitudMaterial
        s = SolicitudMaterial.objects.create(ticket=self.ticket, origen="tecnica", estado="aprobada", cargo="janotec",
                                             modalidad="alquiler", tecnico=self.agente)
        if camaras:
            ItemSolicitud.objects.create(solicitud=s, referencia="DS2CD1027G2HLIU", descripcion="Cámara bala", cantidad=camaras)
        if cantidad:
            ItemSolicitud.objects.create(solicitud=s, insumo=self.cable, descripcion="Cable UTP", cantidad=Decimal(cantidad))
        return s

    def entregar(self, solicitud=True, **kw):
        datos = {"tecnico": self.agente.pk, "modalidad": "alquiler", "ticket": self.ticket.pk,
                 "placa": ["CAM-BAL-2MP002"], "insumo": ["CAB-UTP-EXT · Cable UTP exterior"], "cantidad": ["40"],
                 "firma_entrega": ActaBase.FIRMA}
        datos.update(kw)
        if solicitud is True:
            cantidad = (datos["cantidad"] or [""])[0]
            solicitud = self.solicitud_aprobada(cantidad=cantidad if cantidad.strip() else None)
        if solicitud:
            datos["solicitud"] = solicitud.pk
        self.client.force_login(self.bodega)
        return self.client.post(reverse("inventario:entrega"), datos, follow=True)


class FlujoBodegaTecnicoClienteTests(BaseInventario):
    def test_entrega_acta_y_devolucion(self):
        self.entregar()
        self.camara.refresh_from_db()
        self.assertEqual((self.camara.estado, self.camara.tecnico, self.camara.modalidad), (E.EN_TECNICO, self.agente, "alquiler"))
        self.assertEqual(servicios.disponible_en(self.cable, Lugar.BODEGA), 265)
        self.assertEqual(servicios.disponible_en(self.cable, Lugar.TECNICO, self.agente), 40)

        # En el acta el técnico ve su material: la cámara viene marcada porque se entregó para este ticket
        self.client.force_login(self.agente)
        r = self.client.get(reverse("acta_nueva", args=[self.ticket.radicado]))
        self.assertContains(r, "Del inventario que bodega le entregó")
        self.assertIn(self.camara.pk, r.context["elegidos"])
        self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]),
                         self.datos_acta(usar_activo=[self.camara.pk], **{f"usar_insumo_{self.cable.pk}": "25"}))
        acta = ActaSoporte.objects.get(ticket=self.ticket)
        self.camara.refresh_from_db()
        self.assertEqual((self.camara.estado, self.camara.lugar, self.camara.empresa), (E.ALQUILADO, Lugar.CLIENTE, self.emp_a))
        self.assertEqual(self.camara.movimientos.get(tipo=T.INSTALACION).acta, acta)
        self.assertEqual(servicios.disponible_en(self.cable, Lugar.TECNICO, self.agente), 15)
        self.assertEqual(MovimientoInsumo.objects.get(acta=acta).modalidad, "alquiler")
        self.assertContains(self.client.get(acta.get_absolute_url()), "Materiales y equipos utilizados del inventario")

        # Lo que sobró vuelve a bodega
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:devolucion", args=[self.agente.pk]), {f"cantidad_{self.cable.pk}": "15"})
        self.assertEqual(servicios.disponible_en(self.cable, Lugar.TECNICO, self.agente), 0)
        self.assertEqual(servicios.disponible_en(self.cable, Lugar.BODEGA), 280)

    def test_no_se_entrega_lo_que_no_esta_en_bodega_ni_mas_de_lo_que_hay(self):
        r = self.entregar(cantidad=["999"])
        self.assertContains(r, "solo hay 305")
        self.camara.refresh_from_db()
        self.assertEqual(self.camara.estado, E.DISPONIBLE)  # no se movió nada
        from .models import SolicitudMaterial
        SolicitudMaterial.objects.update(estado="cancelada")  # esa solicitud de prueba no sigue
        self.entregar(insumo=[""], cantidad=[""])
        r = self.entregar(insumo=[""], cantidad=[""])
        self.assertContains(r, "En poder del técnico")  # la cámara ya salió: no se entrega dos veces

    def test_la_modalidad_y_el_cliente_son_obligatorios(self):
        r = self.entregar(modalidad="", ticket="")
        self.assertEqual(Activo.objects.get().estado, E.DISPONIBLE)
        r = self.entregar(modalidad="venta", ticket="")
        self.assertContains(r, "Indique el ticket o el cliente")

    def test_el_acta_no_descuenta_mas_de_lo_que_tiene_el_tecnico(self):
        self.entregar()
        self.client.force_login(self.agente)
        r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]),
                             self.datos_acta(**{f"usar_insumo_{self.cable.pk}": "60"}))
        self.assertContains(r, "tiene 40 y escribió 60")
        self.assertFalse(ActaSoporte.objects.exists())

    def test_retiro_de_alquiler_vuelve_a_bodega(self):
        servicios.mover_activo(self.camara, T.SALIDA, Lugar.CLIENTE, self.bodega, empresa=self.emp_a, modalidad=Modalidad.ALQUILER)
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:cambiar_estado", args=[self.camara.placa]),
                         {"accion": "devolucion", "fecha": "2026-10-20", "observaciones": "Fin del contrato"})
        self.camara.refresh_from_db()
        self.assertEqual((self.camara.estado, self.camara.lugar, self.camara.empresa), (E.DISPONIBLE, Lugar.BODEGA, None))


class PermisosYPantallasTests(BaseInventario):
    def test_quien_ve_y_quien_gestiona(self):
        self.client.force_login(self.cli_a)
        self.assertEqual(self.client.get(reverse("inventario:panel")).status_code, 403)
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(reverse("inventario:entrega")).status_code, 403)
        self.assertContains(self.client.get(reverse("inventario:mi_material")), "Mi material")
        self.assertEqual(self.client.get(reverse("inventario:tecnico", args=[self.agente2.pk])).status_code, 403)
        self.client.force_login(self.bodega)
        self.assertRedirects(self.client.get(reverse("inicio")), reverse("inventario:panel"))
        r = self.client.get(reverse("inventario:panel"))
        self.assertContains(r, "CAM-BAL-2MP002")
        self.assertContains(self.client.get(reverse("inventario:panel") + "?ver=insumos"), "305")
        self.assertContains(self.client.get(self.camara.get_absolute_url()), "Hoja de vida")
        self.assertContains(self.client.get(self.cable.get_absolute_url()), "Kardex")

    def test_compra_de_activos_con_placa_consecutiva(self):
        self.client.force_login(self.bodega)
        self.assertEqual(self.client.get(reverse("inventario:siguiente_placa") + "?prefijo=CAM-BAL-2MP").json()["placa"],
                         "CAM-BAL-2MP003")
        self.client.post(reverse("inventario:compra"), {
            "tipo": "activo", "a-prefijo": "CAM-BAL-2MP", "a-cantidad_iguales": "2", "a-seriales": "S1\nS2",
            "a-categoria": "CCTV", "a-tipo": "Cámara", "a-descripcion": "Bala 2 MP", "a-fecha_compra": "2026-10-22",
            "a-proveedor": "SYSCOM COLOMBIA", "a-factura": "300001", "a-valor": "159095.58", "a-iva": "19"})
        self.assertEqual(list(Activo.objects.filter(serial__in=["S1", "S2"]).values_list("placa", flat=True).order_by("placa")),
                         ["CAM-BAL-2MP003", "CAM-BAL-2MP004"])
        self.client.post(reverse("inventario:compra"), {
            "tipo": "insumo", "i-codigo": "CON-RJ45-001", "i-descripcion": "Conector RJ45", "i-unidad": "Unidad",
            "i-cantidad": "200", "i-valor_unitario": "252.10", "i-iva": "19", "i-proveedor": "MARIA LUISA CARREÑO PINZON",
            "i-factura": "26322", "i-fecha": "2026-10-22"})
        self.assertEqual(servicios.disponible_en(Insumo.objects.get(codigo="CON-RJ45-001"), Lugar.BODEGA), 200)

    def test_al_crear_el_usuario_del_tecnico_se_le_asocia_su_material(self):
        mov = servicios.mover_activo(self.camara, T.ENTREGA, Lugar.TECNICO, self.bodega, modalidad=Modalidad.ASIGNACION,
                                     tecnico_nombre="Espinosa Calderon Jorge Alberto", tecnico_documento="1022324125")
        alberto = Usuario.objects.create_user("aespinosa", "j.espinosa@janotec.test", "Clave-segura-1", rol="agente",
                                              first_name="Jorge Alberto", last_name="Espinosa Calderón")
        mov.refresh_from_db()
        self.camara.refresh_from_db()
        self.assertEqual((mov.tecnico, self.camara.tecnico), (alberto, alberto))


class ImportarSheetsTests(BaseInventario):
    """Un libro pequeño con la misma estructura del Google Sheets INVENTARIO."""

    def libro(self):
        import openpyxl

        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        def hoja(nombre, filas):
            ws = wb.create_sheet(nombre)
            for f in filas:
                ws.append(f)

        hoja("BD_FUNCIONARIOS", [["CEDULA", "NOMBRE", "CARGO", "ROL", "AREA", "SEDE", "CORREO", "ESTADO"],
                                 [1022324125, "ESPINOSA CALDERON JORGE ALBERTO", "ANALISTA", "TECNICO", "TECNICA", "PRINCIPAL", "", "ACTIVO"],
                                 [1010126166, "GORDILLO LEON FREYDER VLADIMIR", "AUXILIAR", "TECNICO", "TECNICA", "PRINCIPAL", "", "INACTIVO"]])
        hoja("PROVEDORES ", [["NIT", "RAZON SOCIAL "], [900000001, "SYSCOM COLOMBIA"]])
        hoja("CODIGOS DE ACTIVO", [["CODIGO", "DESCRIPCION", "UNIDAD DE MEDIDA ", "MARCA", "STOCK ACTUAL"],
                                   ["CAM-BAL-4MP", "Cámara bala 4 MP", "Unidad", "HIKVISION", None]])
        hoja("INVENTARIO_INSUMO", [["CODIGO", "DESCRIPCION", "UNIDAD DE MEDIDA ", "MARCA", "E", "S", "STOCK  ACTUAL", "STOCK  ACTUAL DATA"],
                                   ["BAT-12V7A", "Bateria 12V 7Ah", "unidad", None, 10, 4, 6, 6]])
        hoja("RPTA_COMPRA_INSUMOS", [["Marca temporal", "Digite la Fecha de compra", "Elija el Proveedor", "Número de Factura",
                                      "Código del Insumo", "Cantidad", "Valor Unitario sin I.V.A", "Valor de I.V.A aplicado",
                                      "Responsable de la Compra"],
                                     [datetime(2026, 6, 9), datetime(2026, 1, 22), "SYSCOM COLOMBIA", 26322, "BAT-12V7A", 10, "50420.17", 19, "VARGAS TABARES ALEJANDRA"],
                                     [datetime(2026, 7, 31), datetime(2026, 7, 31), "SYSCOM COLOMBIA", 24407, "BAT-12V7A", 3, 50420, 19, "VARGAS TABARES ALEJANDRA"]])
        hoja("SALIDA_INSUMOS", [["FECHA", "TIPO DE MOVIMIENTO", "ORIGEN", "DESTINO", "CODIGO", "CANTIDAD", "ENTREGADO_A", "RESPONSABLE", "OBSERVACIONES"],
                                [datetime(2026, 2, 2), "ENTREGA", "BODEGA PRINCIPAL", "Empresa A", "BAT-12V7A", 4, "ESPINOSA CALDERON JORGE ALBERTO", "VARGAS TABARES ALEJANDRA", "Mantenimiento sede principal"],
                                [datetime(2026, 1, 29), "ENTREGA", "BODEGA PRINCIPAL", "Empresa A", "BAT-12V7A", 0, "ESPINOSA CALDERON JORGE ALBERTO", "VARGAS TABARES ALEJANDRA", "Prueba"]])
        enc_compra = ["Marca temporal", "Fecha de Compra", "Elija el Proveedor", "Número de Factura", "Ingresa la Placa Asignada",
                      "Ingrese el Serial", "Categoría ", "TIPO", "Subtipo", "Marca", "Referencia ", "Descripción",
                      "Valor de Adquicisión sin I.V.A", "Valor de I.V.A aplicado", "Usuario que realiza Ingreso",
                      "Responsable de la compra", "Observaciones"]
        hoja("RPTA_COMPRA_ACTIVOS", [enc_compra] + [
            [datetime(2026, 6, 16), datetime(2026, 2, 18), "SYSCOM COLOMBIA", 210557, f"CAM-BAL-4MP0{n}", f"GA84905{n}", "CCTV",
             "Cámara", "Bala", "Hikvisión", "DS2CD1043G2LIU", "BALA IP 4 MP", valor, 19, "VARGAS TABARES ALEJANDRA",
             "CHINGATE SILVA YURI MARCELA", "Para Stock"] for n, valor in ((18, "211176.29"), (19, "21176.29"), (20, 211176.29), (21, 211176.29))])
        hoja("INVENTARIO_ACTIVOS", [["PLACA", "SERIAL", "CATEGORIA", "TIPO", "SUBTIPO", "MARCA", "REFERENCIA", "DESCRIPCION", "I", "J", "K", "L", "M", "N", "O",
                                     "FECHA ULTIMO MOVIMIENTO", "ESTADO", "UBICACIÓN ACTIVO (cliente)", "RESPONSABLE ACTUAL", "OBSERVACIONES"],
                                    ["CAM-BAL-4MP018", "GA8490518", "CCTV", "Cámara", "Bala", "Hikvisión", "DS2CD1043G2LIU", "BALA", *[None] * 7,
                                     datetime(2026, 3, 1), "Alquilado", "Empresa A", "ESPINOSA CALDERON JORGE ALBERTO", ""],
                                    ["TEC-INA001", "TEC-INA001", "ALARMA", "Teclado", "", "Paradox", "K636", "TECLADO", *[None] * 7,
                                     None, "Disponible", "JANOTEC", "", ""]])
        hoja("MOVIMIENTOS_ACTIVOS", [["PLACA", "SERIAL", "CATEGORIA", "TIPO DE MEDIO TECNOLOGICO", "SUBTIPO", "MARCA", "REFERENCIA", "DESCRIPCION"]])
        hoja("RPTA_MOVIMIENTOS_ACTIVOS", [["Marca temporal", "Coloque la fecha del Movimeinto", "Escoja o escriba la placa",
                                           "Escoja el estado nuevo para el activo", "Escoja Cliente o empresa a quien se le hará el movimiento del activo",
                                           "Escoja Responsable", "Observaciones"],
                                          [datetime(2026, 7, 10), datetime(2026, 3, 1), "CAM-BAL-4MP018", "Alquilado", "800021272 GASES DEL LLANO S.A.",
                                           "1022324125 ESPINOSA CALDERON JORGE ALBERTO", "Alquiler para Empresa A sede principal"],
                                          [datetime(2026, 7, 10), datetime(2026, 2, 20), "CAM-BAL-4MP019", "Disponible", "901542948 JANOTEC",
                                           "VLADIMIR GORDILLO", "En Stock, bodega Jano Tec"],
                                          [datetime(2026, 7, 10), datetime(2026, 4, 2), "CAM-BAL-4MP020", "Vendido", "822002315 SEGURIDAD JANO LTDA",
                                           "LEON FREYDER BLADIMIR", "Venta para Seguridad Jano, reinversion cliente Imecol"]])
        ruta = os.path.join(tempfile.mkdtemp(), "INVENTARIO.xlsx")
        wb.save(ruta)
        return ruta

    def test_simula_sin_guardar_y_luego_importa_con_historial(self):
        Empresa.objects.filter(pk=self.emp_a.pk).update(nit="800021272-1")
        Activo.objects.all().delete()
        MovimientoInsumo.objects.all().delete()
        Insumo.objects.all().delete()
        ruta = self.libro()
        salida = tempfile.mktemp(suffix=".txt")
        call_command("importar_inventario", ruta, informe=salida, stdout=open(os.devnull, "w"))
        self.assertFalse(Activo.objects.exists())  # simulación: nada guardado
        informe = open(salida, encoding="utf-8").read()
        self.assertIn("CAM-BAL-4MP019 (DS2CD1043G2LIU): $21,176", informe)  # valor sospechoso
        self.assertIn("Stock de insumo distinto", informe)  # la compra del 31 de julio que la hoja no sumaba
        self.assertIn("TEC-INA001", informe)  # sin compra

        call_command("importar_inventario", ruta, confirmar=True, stdout=open(os.devnull, "w"))
        a18 = Activo.objects.get(placa="CAM-BAL-4MP018")
        self.assertEqual((a18.estado, a18.empresa, a18.tecnico_nombre), (E.ALQUILADO, self.emp_a, "Espinosa Calderon Jorge Alberto"))
        a20 = Activo.objects.get(placa="CAM-BAL-4MP020")
        self.assertEqual((a20.estado, a20.modalidad, a20.tecnico_nombre), (E.VENDIDO, "reinversion", "Gordillo Leon Freyder Vladimir"))
        self.assertEqual(Activo.objects.get(placa="CAM-BAL-4MP021").estado, E.DISPONIBLE)  # comprado sin movimientos
        self.assertEqual(Activo.objects.get(placa="TEC-INA001").serial, "")
        bateria = Insumo.objects.get(codigo="BAT-12V7A")
        self.assertEqual(servicios.disponible_en(bateria, Lugar.BODEGA), 9)  # 10 + 3 − 4 (la fila «Prueba» de 0 se descarta)
        self.assertEqual(bateria.unidad, "Unidad")
        with self.assertRaisesMessage(Exception, "ya se importó"):
            call_command("importar_inventario", ruta, confirmar=True, stdout=open(os.devnull, "w"))


class SolicitudesDeMaterialTests(BaseInventario):
    """Nada sale hacia un cliente sin validación de bodega y firmas; si es a cargo del cliente, primero la propuesta."""

    def setUp(self):
        super().setUp()
        from datetime import time, timedelta

        from django.utils import timezone
        self.director = Usuario.objects.create_user("miguel", "dir@janotec.test", "Clave-segura-1", rol="direccion",
                                                    first_name="Miguel", last_name="Tibamoso")
        self.ticket.estado = Ticket.Estado.RECEPCIONADO
        self.ticket.save()
        Activo.objects.create(placa="CAM-BAL-4MP001", referencia="DS2CD1043G2LIU", tipo="Cámara")
        self.fecha = timezone.localdate() + timedelta(days=2)
        while self.fecha.weekday() > 4:
            self.fecha += timedelta(days=1)
        self.hora = (time(8), time(12))

    def programar(self, insumos):
        from cronograma import servicios as crono
        from cronograma.models import Actividad
        a = Actividad.objects.create(ticket=self.ticket, fecha=self.fecha, hora_inicio=self.hora[0], hora_fin=self.hora[1],
                                     cliente="Empresa A", sede="Principal", actividad="Visita", insumos=insumos)
        a.encargados.add(self.agente)
        crono.guardar(a, self.supervisor)
        return a

    def test_del_cronograma_a_la_entrega_con_validacion_compra_y_firmas(self):
        from django.core import mail

        from .models import SolicitudMaterial
        mail.outbox.clear()
        a = self.programar("40 m Cable UTP exterior [CAB-UTP-EXT]\n2 Cámara Bala Hikvisión DS2CD1043G2LIU [DS2CD1043G2LIU]\n"
                           "1 cinta aislante")
        s = SolicitudMaterial.objects.get(actividad=a)
        self.assertEqual((s.origen, s.estado, s.tecnico), ("tecnica", "validacion", self.agente))
        destinos = {d for m in mail.outbox for d in m.to}
        self.assertIn("b@janotec.test", destinos)  # bodega; la autorización se pide cuando bodega valida
        self.assertNotIn("dir@janotec.test", destinos)
        # Sin firmas no sale: bodega no puede entregar a un cliente
        self.client.force_login(self.bodega)
        r = self.client.post(reverse("inventario:entrega"), {"tecnico": self.agente.pk, "modalidad": "venta",
                                                             "ticket": self.ticket.pk, "placa": ["CAM-BAL-4MP001"]}, follow=True)
        self.assertContains(r, "solo sale con una solicitud de material aprobada")
        # Validación: a cargo de JANOTEC, holgura de cable; falta una cámara → solicitud de compra
        cable = s.items.get(insumo=self.cable)
        cinta = s.items.get(descripcion="cinta aislante")
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]),
                         {"cargo": "janotec", "modalidad": "mantenimiento", "tecnico": self.agente.pk, f"adicional_{cable.pk}": "10",
                          f"listo_{cinta.pk}": "on"})
        s.refresh_from_db()
        self.assertEqual(s.estado, "compra")
        self.assertContains(self.client.get(reverse("inventario:solicitudes")), "Compras pendientes")
        # Ingresa la compra de la cámara que faltaba: la solicitud sigue sola a firmas
        self.client.post(reverse("inventario:compra"), {
            "tipo": "activo", "a-prefijo": "CAM-BAL-4MP", "a-cantidad_iguales": "1", "a-categoria": "CCTV", "a-tipo": "Cámara",
            "a-referencia": "DS2CD1043G2LIU", "a-descripcion": "Bala 4 MP", "a-fecha_compra": "2026-10-22",
            "a-proveedor": "SYSCOM COLOMBIA", "a-factura": "1", "a-valor": "200000", "a-iva": "19"})
        s.refresh_from_db()
        self.assertEqual(s.estado, "aprobacion")
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]), {})  # ya no está en validación
        # Bodega firma al validar; falta la autorización. El técnico no puede firmar.
        self.client.force_login(self.agente)
        self.assertEqual(self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "firmar"]),
                                          {"decision": "aprobar", "firma": ActaBase.FIRMA}).status_code, 403)
        s.refresh_from_db()
        self.assertEqual(s.estado, "aprobacion")
        # Pasó por compra: bodega firma cuando ya tiene todo en bodega
        from . import solicitudes as sol
        self.assertEqual(sol.firmas_pendientes(s), ["bodega", "direccion"])
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "firmar"]), {"decision": "aprobar", "firma": ActaBase.FIRMA})
        # Dirección no aprueba sin explicar; luego aprueba
        self.client.force_login(self.director)
        r = self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "firmar"]), {"decision": "rechazar"}, follow=True)
        self.assertContains(r, "Explique por qué no aprueba")
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "firmar"]), {"decision": "aprobar", "firma": ActaBase.FIRMA})
        s.refresh_from_db()
        self.assertEqual(s.estado, "aprobada")
        # Entrega precargada con lo aprobado (40 + 10 de holgura) y dos placas de la referencia
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:entrega") + f"?solicitud={s.pk}")
        self.assertEqual(r.context["insumos_post"], [("CAB-UTP-EXT · Cable UTP exterior", "50")])
        self.assertEqual(len(r.context["placas"]), 2)
        self.assertEqual([i.descripcion for i in r.context["pendiente"]["a_mano"]], ["cinta aislante"])
        r = self.client.post(reverse("inventario:entrega"), {
            "solicitud": s.pk, "tecnico": self.agente.pk, "modalidad": "mantenimiento", "ticket": self.ticket.pk,
            "firma_entrega": ActaBase.FIRMA,
            "placa": r.context["placas"], "insumo": ["CAB-UTP-EXT · Cable UTP exterior"], "cantidad": ["60"]}, follow=True)
        self.assertContains(r, "aprobado 50")  # no sale más de lo aprobado
        self.client.post(reverse("inventario:entrega"), {
            "solicitud": s.pk, "tecnico": self.agente.pk, "modalidad": "mantenimiento", "ticket": self.ticket.pk,
            "firma_entrega": ActaBase.FIRMA,
            "placa": self.client.get(reverse("inventario:entrega") + f"?solicitud={s.pk}").context["placas"],
            "insumo": ["CAB-UTP-EXT · Cable UTP exterior"], "cantidad": ["50"],
            "a_mano": [str(s.items.get(descripcion="cinta aislante").pk)]})  # lo escrito a mano también va en el acta
        s.refresh_from_db()
        self.assertEqual((s.estado, s.actas_entrega.count()), ("entregada", 1))
        self.assertEqual(servicios.disponible_en(self.cable, Lugar.TECNICO, self.agente), 50)

    def test_a_cargo_del_cliente_va_primero_a_propuesta(self):
        from comercial import servicios as com

        from .models import SolicitudMaterial
        a = self.programar("1 Cámara Bala Hikvisión DS2CD1027G2HLIU [DS2CD1027G2HLIU]")
        s = SolicitudMaterial.objects.get(actividad=a)
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]),
                         {"cargo": "cliente", "modalidad": "venta", "tecnico": self.agente.pk})
        s.refresh_from_db()
        self.ticket.refresh_from_db()
        self.assertEqual((s.estado, self.ticket.area), ("propuesta", Ticket.Area.COMERCIAL))  # pasó a comercial
        self.assertIn("a cargo del cliente", self.ticket.ficha.levantamiento)
        # El cliente aprueba la propuesta: la solicitud sigue (hay existencias) a firmas
        com.registrar_respuesta(self.ticket.ficha, self.supervisor, aprobada=True)
        s.refresh_from_db()
        self.assertEqual((s.estado, s.cliente_aprobo), ("aprobacion", True))

    def test_si_el_cliente_no_aprueba_se_cancela(self):
        from comercial import servicios as com

        from .models import SolicitudMaterial
        a = self.programar("1 Cámara Bala Hikvisión DS2CD1027G2HLIU [DS2CD1027G2HLIU]")
        s = SolicitudMaterial.objects.get(actividad=a)
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]),
                         {"cargo": "cliente", "modalidad": "venta"})
        self.ticket.refresh_from_db()
        com.registrar_respuesta(self.ticket.ficha, self.supervisor, aprobada=False, motivo="Muy costoso")
        s.refresh_from_db()
        self.assertEqual(s.estado, "cancelada")

    def test_de_comercial_basta_la_autorizacion_del_coordinador(self):
        from comercial import servicios as com
        from comercial.models import FichaComercial

        from .models import SolicitudMaterial
        ficha = FichaComercial.objects.create(ticket=self.ticket, etapa="enviada")
        com.registrar_respuesta(ficha, self.supervisor, aprobada=True)
        s = SolicitudMaterial.objects.get(ticket=self.ticket, origen="comercial")
        self.assertEqual((s.estado, s.cargo, s.cliente_aprobo), ("borrador", "cliente", True))
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "enviar"]),
                         {"materiales": "1 Cámara Bala Hikvisión DS2CD1027G2HLIU [DS2CD1027G2HLIU]\n20 m Cable UTP exterior [CAB-UTP-EXT]"})
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]),
                         {"cargo": "cliente", "modalidad": "venta", "tecnico": self.agente.pk})
        s.refresh_from_db()
        self.assertEqual(s.estado, "aprobacion")
        from . import solicitudes as sol
        self.assertEqual(sol.firmas_pendientes(s), ["direccion"])  # bodega firmó al validar; falta la autorización
        # El jefe no está: autoriza el coordinador TIC y con eso basta
        self.assertEqual(sol.roles_que_puede_firmar(s, self.supervisor), ["coordinador"])
        self.assertEqual(sol.roles_que_puede_firmar(s, self.director), ["direccion"])
        self.client.force_login(self.supervisor)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "firmar"]), {"decision": "aprobar", "firma": ActaBase.FIRMA})
        s.refresh_from_db()
        self.assertEqual(s.estado, "aprobada")
        self.assertEqual(sol.roles_que_puede_firmar(s, self.director), [])
        self.assertContains(self.client.get(s.get_absolute_url()), "Autorización (coordinador TIC o dirección)")

    def test_propuesta_aprobada_queda_en_inventario_con_lista_y_acta_de_entrega(self):
        """Propuesta aprobada → «En inventario»: lista (aprobado + adicionales) → firmas → acta de entrega → recepcionado."""
        from django.core import mail

        from comercial import servicios as com
        from comercial.models import FichaComercial, InformeRequerimiento
        from inventario import solicitudes as sol

        from .models import SolicitudMaterial
        self.ticket.estado, self.ticket.area = Ticket.Estado.PROPUESTA_ENVIADA, Ticket.Area.COMERCIAL
        self.ticket.save()
        ficha = FichaComercial.objects.create(ticket=self.ticket, etapa="enviada")
        InformeRequerimiento.objects.create(ficha=ficha, necesidad="Cableado", elaborado_por=self.supervisor,
                                            servicios="1 Instalación de cableado",
                                            materiales="40 m Cable UTP exterior [CAB-UTP-EXT]")
        com.registrar_respuesta(ficha, self.supervisor, aprobada=True)
        self.ticket.refresh_from_db()
        # 1. Aprobada: el ticket queda «En inventario» y bodega tiene la lista con lo aprobado (sin el servicio)
        self.assertEqual((self.ticket.estado, self.ticket.area), (Ticket.Estado.EN_INVENTARIO, Ticket.Area.TECNICA))
        s = SolicitudMaterial.objects.get(ticket=self.ticket, origen="comercial")
        self.assertEqual((s.estado, [i.insumo for i in s.items.all()]), ("validacion", [self.cable]))
        # 2. Coordinación agrega un adicional y ve el avance en el ticket (sin entrar al módulo de inventario)
        self.client.force_login(self.supervisor)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "adicionales"]), {"adicionales": "6 Chazo plástico"})
        lista = sol.lista_alistamiento(s)
        self.assertEqual((len(lista["aprobado"]), len(lista["adicional"]), lista["adicional"][0]["estado"]), (1, 1, "confirmar"))
        r = self.client.get(self.ticket.get_absolute_url())
        self.assertContains(r, "2. Adicionales para instalar")
        self.assertContains(r, "Pasar a Recepcionado")
        # 3. Bodega valida (ya tiene el adicional), autoriza el coordinador y queda aprobada
        chazo = s.items.get(seccion="adicional")
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]),
                         {"cargo": "cliente", "modalidad": "venta", "tecnico": self.agente.pk, f"listo_{chazo.pk}": "on"})
        s.refresh_from_db()
        self.assertEqual((s.estado, sol.lista_alistamiento(s)["avance"]), ("aprobacion", 100))
        self.client.force_login(self.supervisor)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "firmar"]), {"decision": "aprobar", "firma": ActaBase.FIRMA})
        s.refresh_from_db()
        self.assertEqual(s.estado, "aprobada")
        # 4. Entrega: queda el acta (solo lo entregado, en sus dos secciones) en PDF y el técnico confirma
        mail.outbox.clear()
        self.client.force_login(self.bodega)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("inventario:entrega"), {
                "solicitud": s.pk, "tecnico": self.agente.pk, "modalidad": "venta", "ticket": self.ticket.pk,
                "firma_entrega": ActaBase.FIRMA,
                "insumo": ["CAB-UTP-EXT · Cable UTP exterior"], "cantidad": ["40"], "a_mano": [str(chazo.pk)]})
        s.refresh_from_db()
        acta = s.actas_entrega.get()
        self.assertEqual((s.estado, [x["seccion"] for x in acta.detalle]), ("entregada", ["aprobado", "adicional"]))
        self.assertTrue(acta.adjunto.nombre_original.endswith(".pdf"))
        self.assertTrue(any(self.agente.email in m.to for m in mail.outbox), "el técnico confirma que recibió")
        mail.outbox.clear()
        self.client.force_login(self.agente)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "recibido"]), {"acta": acta.pk})  # sin firma no cuenta
        acta.refresh_from_db()
        self.assertIsNone(acta.recibido_en)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "recibido"]), {"acta": acta.pk, "firma": ActaBase.FIRMA})
        acta.refresh_from_db()
        self.assertEqual(acta.recibido_por, self.agente)
        # 5. Todo entregado y confirmado: pasa solo al área técnica, asignado a quien recibió, y coordinación lo sabe
        self.ticket.refresh_from_db()
        self.assertEqual((self.ticket.estado, self.ticket.asignado_a), (Ticket.Estado.RECEPCIONADO, self.agente))
        self.assertTrue(any("instalación por programar" in m.subject for m in mail.outbox))
        # 6. Al programarla, el coordinador ve que es la instalación y los insumos son lo entregado (no se vuelve a pedir)
        from cronograma.models import Actividad
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(reverse("cronograma:semana")), "Instalación · material entregado")
        valores = Actividad.valores_desde_ticket(self.ticket)
        self.assertEqual(valores["tipo"], "instalacion")
        self.assertIn("40 Cable UTP exterior [CAB-UTP-EXT]", valores["insumos"])
        self.client.post(reverse("cronograma:crear"), {
            **{k: v for k, v in valores.items() if k not in ("trabajo_alturas", "sst", "bodega")}, "ticket": self.ticket.pk,
            "fecha": timezone.localdate().isoformat(), "encargados": [self.agente.pk], "insumos": "99 Otra cosa",
            "sst_duracion": "medio día"})
        actividad = Actividad.objects.get(ticket=self.ticket)
        self.assertIn("40 Cable UTP exterior", actividad.insumos)
        self.assertNotIn("Otra cosa", actividad.insumos)
        self.assertEqual(SolicitudMaterial.objects.filter(ticket=self.ticket).count(), 1)

    def test_el_informe_separa_servicios_de_materiales(self):
        """Acta → informe → propuesta: los servicios se cotizan; a bodega solo llegan los materiales y equipos."""
        from comercial import servicios as com
        from comercial.models import FichaComercial, InformeRequerimiento

        from .models import SolicitudMaterial
        ficha = FichaComercial.objects.create(ticket=self.ticket, etapa="enviada")
        InformeRequerimiento.objects.create(ficha=ficha, necesidad="Desmonte de alarmas y CCTV", elaborado_por=self.supervisor,
                                            servicios="1 Desmonte del sistema de alarma PACOM\n1 Inventario y registro fotográfico")
        com.registrar_respuesta(ficha, self.supervisor, aprobada=True)
        # Solo servicios (un desmonte): bodega no recibe una solicitud vacía y el traspaso no queda frenado
        self.assertFalse(SolicitudMaterial.objects.filter(ticket=self.ticket).exists())
        self.assertTrue(self.ticket.seguimientos.filter(mensaje__contains="solo tiene servicios").exists())

    def test_a_bodega_no_llegan_los_servicios_del_informe(self):
        from comercial.models import FichaComercial, InformeRequerimiento
        from inventario import solicitudes as sol
        ficha = FichaComercial.objects.create(ticket=self.ticket, etapa="aprobada")
        InformeRequerimiento.objects.create(ficha=ficha, necesidad="Cámaras", elaborado_por=self.supervisor,
                                            servicios="1 Instalación y configuración de cámaras",
                                            materiales="3 Cámara bala IP 4 MP\n80 m Cable UTP cat 6")
        self.assertEqual(sol.material_de_la_propuesta(ficha), "3 Cámara bala IP 4 MP\n80 m Cable UTP cat 6")

    def test_un_rechazo_devuelve_la_solicitud_con_el_motivo(self):
        from .models import SolicitudMaterial
        a = self.programar("1 Cámara Bala Hikvisión DS2CD1027G2HLIU [DS2CD1027G2HLIU]")
        s = SolicitudMaterial.objects.get(actividad=a)
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]), {"cargo": "janotec", "modalidad": "venta"})
        self.client.force_login(self.director)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "firmar"]),
                         {"decision": "rechazar", "comentario": "El cliente pidió domo, no bala."})
        s.refresh_from_db()
        self.assertEqual((s.estado, s.ronda), ("validacion", 2))
        self.assertIn("domo", s.motivo_devolucion)
        self.assertContains(self.client.get(s.get_absolute_url()), "Firmas de rondas anteriores")

    def test_las_herramientas_del_personal_salen_sin_firmas_pero_los_insumos_no(self):
        r = self.entregar(solicitud=None, modalidad="asignacion", ticket="", insumo=[""], cantidad=[""])
        self.camara.refresh_from_db()
        self.assertEqual((self.camara.estado, self.camara.tecnico), (E.ASIGNADO, self.agente))
        r = self.entregar(solicitud=None, modalidad="asignacion", ticket="", placa=[""])
        self.assertContains(r, "solo salen herramientas")
        self.assertEqual(servicios.disponible_en(self.cable, Lugar.TECNICO, self.agente), 0)

    def test_el_coordinador_ve_la_solicitud_desde_el_cronograma(self):
        a = self.programar("40 m Cable UTP exterior [CAB-UTP-EXT]")
        # Bodega prepara la entrega de la visita; coordinación ve el alistamiento en el ticket (sin el módulo)
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:entrega") + f"?actividad={a.pk}", follow=True)
        self.assertContains(r, "Validación de bodega")
        self.client.force_login(self.supervisor)
        r = self.client.get(reverse("cronograma:semana") + f"?d={a.fecha:%Y-%m-%d}")
        self.assertNotContains(r, f"{reverse('inventario:entrega')}?actividad={a.pk}")
        self.assertContains(self.client.get(self.ticket.get_absolute_url()), "Alistamiento de materiales y equipos")


class HerramientasYAuditoriaTests(BaseInventario):
    """Herramientas por técnico y su auditoría, con las reglas de Inventario_Global."""

    def setUp(self):
        super().setUp()
        from .models import Herramienta
        self.ponchadora = Herramienta.objects.create(nombre="Ponchadora RJ45", marca="Truper", cantidad=1, costo_compra=45000)
        self.alicates = Herramienta.objects.create(nombre="Alicate", marca="Stanley", cantidad=2, costo_compra=30000)
        self.client.force_login(self.bodega)
        for h in (self.ponchadora, self.alicates):
            self.client.post(reverse("inventario:herramienta_mover", args=[h.pk]), {"tecnico": self.agente.pk})

    def auditar(self, **datos):
        base = {f"cantidad_{self.ponchadora.pk}": "1", f"estado_{self.ponchadora.pk}": "en_uso",
                f"cantidad_{self.alicates.pk}": "2", f"estado_{self.alicates.pk}": "en_uso"}
        base.update(datos)
        self.client.force_login(self.bodega)
        return self.client.post(reverse("inventario:auditar", args=[self.agente.pk]), base, follow=True)

    def test_entregar_y_devolver_queda_en_el_historial_sin_firmas(self):
        self.ponchadora.refresh_from_db()
        self.assertEqual((self.ponchadora.tecnico, self.ponchadora.estado), (self.agente, "en_uso"))
        self.client.post(reverse("inventario:herramienta_mover", args=[self.ponchadora.pk]), {"tecnico": self.agente2.pk})
        self.client.post(reverse("inventario:herramienta_mover", args=[self.ponchadora.pk]), {"accion": "devolver"})
        self.ponchadora.refresh_from_db()
        self.assertEqual((self.ponchadora.tecnico, self.ponchadora.estado), (None, "disponible"))
        self.assertEqual(list(self.ponchadora.entregas.order_by("pk").values_list("tipo", flat=True)),
                         ["entrega", "entrega", "devolucion"])
        r = self.client.get(self.ponchadora.get_absolute_url())
        self.assertContains(r, "Historial de entregas")

    def test_auditoria_conforme(self):
        r = self.auditar()
        from .models import AuditoriaHerramientas
        a = AuditoriaHerramientas.objects.get()
        self.assertEqual((a.resultado, a.novedades, a.unidades_registradas, a.unidades_encontradas), ("conforme", 0, 3, 3))
        self.assertContains(r, "Auditoría #")

    def test_con_novedad_exige_nota_y_solo_se_da_de_baja_lo_danado(self):
        from .models import AuditoriaHerramientas
        r = self.auditar(**{f"cantidad_{self.alicates.pk}": "1"})
        self.assertContains(r, "Explique en la nota qué pasó con «Alicate»")
        r = self.auditar(**{f"baja_{self.ponchadora.pk}": "on"})
        self.assertContains(r, "Solo se puede dar de baja una herramienta dañada")
        self.assertFalse(AuditoriaHerramientas.objects.exists())
        self.auditar(**{f"cantidad_{self.alicates.pk}": "1", f"nota_{self.alicates.pk}": "Se perdió uno en Gases del Llano",
                        f"estado_{self.ponchadora.pk}": "danada", f"baja_{self.ponchadora.pk}": "on",
                        f"nota_{self.ponchadora.pk}": "Se partió el mango"})
        a = AuditoriaHerramientas.objects.get()
        self.assertEqual((a.resultado, a.novedades), ("con_novedades", 2))
        self.assertEqual(dict(a.detalles.values_list("herramienta_nombre", "resultado")),
                         {"Ponchadora RJ45": "dada_de_baja", "Alicate": "con_novedad"})
        self.ponchadora.refresh_from_db()
        self.assertEqual(self.ponchadora.estado, "baja")
        # La siguiente auditoría ya no incluye lo dado de baja
        from . import herramientas as srv
        self.assertEqual(srv.por_auditar(self.agente), [self.alicates])

    def test_no_encontrada_cuenta_cero(self):
        from .models import DetalleAuditoria
        self.auditar(**{f"estado_{self.alicates.pk}": "no_encontrada", f"nota_{self.alicates.pk}": "No los tenía en la camioneta"})
        d = DetalleAuditoria.objects.get(herramienta=self.alicates)
        self.assertEqual((d.cantidad_encontrada, d.resultado), (0, "no_encontrada"))

    def test_el_tecnico_ve_sus_herramientas_y_auditorias_pero_no_audita_ni_ve_las_de_otros(self):
        self.auditar()
        self.client.force_login(self.agente)
        r = self.client.get(reverse("inventario:mi_material"))
        self.assertContains(r, "Ponchadora RJ45")
        self.assertContains(r, "Última auditoría")
        self.assertEqual(self.client.get(reverse("inventario:auditar", args=[self.agente.pk])).status_code, 403)
        self.client.force_login(self.agente2)
        self.assertEqual(self.client.get(self.ponchadora.get_absolute_url()).status_code, 404)
        self.assertNotContains(self.client.get(reverse("inventario:herramientas")), "Ponchadora RJ45")

    def test_crear_editar_y_listado(self):
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:herramienta_nueva"), {
            "nombre": "Taladro percutor", "marca": "Bosch", "serial": "TL-1", "cantidad": "1", "estado": "disponible",
            "tecnico": self.agente2.pk, "costo_compra": "320000"})
        from .models import Herramienta
        t = Herramienta.objects.get(serial="TL-1")
        self.assertEqual((t.tecnico, t.estado, t.entregas.count()), (self.agente2, "en_uso", 1))
        r = self.client.get(reverse("inventario:herramientas"))
        self.assertContains(r, "Taladro percutor")
        self.assertContains(r, "Sin auditar")

    def test_al_crear_el_usuario_del_tecnico_se_le_asocian_sus_herramientas(self):
        from .models import Herramienta
        h = Herramienta.objects.create(nombre="Multímetro", tecnico_nombre="Gordillo Leon Freyder Vladimir", estado="en_uso")
        freyder = Usuario.objects.create_user("fgordillo", "f@janotec.test", "Clave-segura-1", rol="agente",
                                              first_name="Freyder Vladimir", last_name="Gordillo León")
        h.refresh_from_db()
        self.assertEqual(h.tecnico, freyder)


class ImportarInventarioGlobalTests(BaseInventario):
    """La exportación SQL de phpMyAdmin de Inventario_Global: herramientas, entregas y auditorías."""

    VOLCADO = """-- phpMyAdmin SQL Dump
CREATE TABLE `usuarios` (
  `usuario_id` bigint(20) UNSIGNED NOT NULL,
  `nombre_completo` varchar(180) NOT NULL,
  `documento` varchar(40) DEFAULT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `usuarios` (`usuario_id`, `nombre_completo`, `documento`) VALUES
(1, 'ESPINOSA CALDERON JORGE ALBERTO', '1022324125'),
(2, 'GORDILLO LEON FREYDER VLADIMIR', NULL);
CREATE TABLE `marcas` (
  `marca_id` bigint(20) UNSIGNED NOT NULL,
  `nombre` varchar(120) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO `marcas` VALUES (7, 'Truper');
INSERT INTO `herramientas` (`herramienta_id`, `nombre`, `descripcion`, `marca`, `marca_id`, `serial`, `cantidad`, `estado`, `usuario_id`, `costo_compra`) VALUES
(10, 'Ponchadora RJ45', 'Con \\'trinquete\\', para cat6', 'TRUPER', 7, NULL, 1, 'En uso', 1, '45000.00'),
(11, 'Alicate', NULL, 'Stanley', NULL, 'AL-1', 2, 'Dañada', 2, '30000.00'),
(12, 'Escalera', NULL, NULL, NULL, NULL, 1, 'Disponible', NULL, '0.00');
INSERT INTO `entregas_herramientas` (`entrega_id`, `herramienta_id`, `herramienta_nombre`, `tecnico_id`, `tecnico_nombre`, `entregado_en`, `registrado_por`) VALUES
(1, 10, 'Ponchadora RJ45', 1, 'ESPINOSA CALDERON JORGE ALBERTO', '2026-10-01 09:00:00', 'avargas');
INSERT INTO `auditorias_herramientas` (`auditoria_id`, `tecnico_id`, `tecnico_nombre`, `fecha_auditoria`, `auditor`, `resultado`, `total_herramientas`, `unidades_registradas`, `unidades_encontradas`, `novedades`, `nota_general`, `ip`) VALUES
(5, 2, 'GORDILLO LEON FREYDER VLADIMIR', '2026-10-15 10:30:00', 'Alejandra Vargas', 'Con novedades', 1, 2, 1, 1, 'Revisión mensual', '192.168.1.20');
INSERT INTO `auditoria_herramientas_detalle` (`detalle_id`, `auditoria_id`, `herramienta_id`, `herramienta_nombre`, `marca`, `serial`, `cantidad_registrada`, `cantidad_encontrada`, `estado_registrado`, `estado_encontrado`, `resultado`, `nota`) VALUES
(1, 5, 11, 'Alicate', 'Stanley', 'AL-1', 2, 1, 'En uso', 'Dañada', 'Con novedad', 'Se partió uno');
"""

    def test_simula_y_luego_importa(self):
        import os
        import tempfile

        from django.core.management import call_command

        from .models import AuditoriaHerramientas, Herramienta
        alberto = Usuario.objects.create_user("aespinosa", "a@janotec.test", "Clave-segura-1", rol="agente",
                                              first_name="Jorge Alberto", last_name="Espinosa Calderon", documento="1022324125")
        ruta = os.path.join(tempfile.mkdtemp(), "inventario_global.sql")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(self.VOLCADO)
        informe = os.path.join(tempfile.mkdtemp(), "informe.txt")
        call_command("importar_herramientas", ruta, informe=informe, stdout=open(os.devnull, "w"))
        self.assertFalse(Herramienta.objects.exists())  # simulación
        texto = open(informe, encoding="utf-8").read()
        self.assertIn("herramientas: 3", texto)
        self.assertIn("GORDILLO LEON FREYDER VLADIMIR", texto)  # aún sin usuario

        call_command("importar_herramientas", ruta, confirmar=True, stdout=open(os.devnull, "w"))
        p = Herramienta.objects.get(nombre="Ponchadora RJ45")
        self.assertEqual((p.tecnico, p.marca, p.estado, p.descripcion), (alberto, "Truper", "en_uso", "Con 'trinquete', para cat6"))
        self.assertEqual(p.entregas.get().registrado_por, "avargas")
        a = Herramienta.objects.get(nombre="Alicate")
        self.assertEqual((a.tecnico, a.tecnico_nombre, a.estado), (None, "GORDILLO LEON FREYDER VLADIMIR", "danada"))
        self.assertEqual(Herramienta.objects.get(nombre="Escalera").tecnico_nombre, "")
        aud = AuditoriaHerramientas.objects.get()
        self.assertEqual((aud.resultado, aud.novedades, aud.detalles.get().estado_encontrado), ("con_novedades", 1, "danada"))
        # Al crear el usuario de Freyder se le asocian su herramienta y su auditoría
        freyder = Usuario.objects.create_user("fgordillo", "f@janotec.test", "Clave-segura-1", rol="agente",
                                              first_name="Freyder Vladimir", last_name="Gordillo Leon")
        a.refresh_from_db()
        aud.refresh_from_db()
        self.assertEqual((a.tecnico, aud.tecnico), (freyder, freyder))
        with self.assertRaisesMessage(Exception, "ya se importaron"):
            call_command("importar_herramientas", ruta, confirmar=True, stdout=open(os.devnull, "w"))


class PrestamosYAlquileresTests(BaseInventario):
    """Préstamos y alquileres con fecha prevista de devolución, vencidos y prórrogas."""

    def setUp(self):
        super().setUp()
        from datetime import timedelta

        from django.utils import timezone
        self.hoy = timezone.localdate()
        self.antier = self.hoy - timedelta(days=2)

    def test_la_validacion_de_un_prestamo_exige_la_fecha(self):
        from .forms import ValidarSolicitudForm
        s = self.solicitud_aprobada()
        s.estado = "validacion"
        s.save()
        form = ValidarSolicitudForm({"cargo": "janotec", "modalidad": "prestamo"}, solicitud=s)
        self.assertFalse(form.is_valid())
        self.assertIn("fecha_devolucion", form.errors)
        form = ValidarSolicitudForm({"cargo": "janotec", "modalidad": "alquiler"}, solicitud=s)
        self.assertTrue(form.is_valid())  # en alquiler es opcional

    def test_del_prestamo_a_la_devolucion(self):
        from .models import MovimientoActivo
        s = self.solicitud_aprobada(cantidad=None)
        s.modalidad, s.fecha_devolucion = "prestamo", self.antier
        s.save()
        self.entregar(solicitud=s, modalidad="prestamo", insumo=[""], cantidad=[""])
        self.camara.refresh_from_db()
        self.assertEqual((self.camara.estado, self.camara.fecha_devolucion), (E.EN_TECNICO, self.antier))
        # El técnico la instala con el acta: queda prestada en el cliente con su fecha
        self.client.force_login(self.agente)
        self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(usar_activo=[self.camara.pk]))
        self.camara.refresh_from_db()
        self.assertEqual((self.camara.estado, self.camara.lugar, self.camara.fecha_devolucion), (E.PRESTADO, Lugar.CLIENTE, self.antier))
        self.assertTrue(self.camara.vencido)
        # Aparece en préstamos como vencido y en el panel
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:prestamos") + "?ver=vencidos")
        self.assertContains(r, "CAM-BAL-2MP002")
        self.assertContains(r, "Vencido hace 2 días")
        self.assertEqual(self.client.get(reverse("inventario:panel")).context["kpi"]["prestamos_vencidos"], 1)
        # Prórroga con motivo: queda en la hoja de vida
        nueva = self.hoy.replace(year=self.hoy.year + 1)
        self.client.post(reverse("inventario:cambiar_devolucion", args=[self.camara.placa]),
                         {"fecha": nueva.isoformat(), "motivo": "El cliente amplió la obra"})
        self.camara.refresh_from_db()
        self.assertEqual((self.camara.fecha_devolucion, self.camara.vencido), (nueva, False))
        self.assertIn("El cliente amplió la obra", self.camara.movimientos.get(tipo=T.AJUSTE).observaciones)
        r = self.client.post(reverse("inventario:cambiar_devolucion", args=[self.camara.placa]), {"fecha": "", "motivo": "x"}, follow=True)
        self.assertContains(r, "Indique la nueva fecha y el motivo")
        # Vuelve a bodega: deja de estar prestada y pierde la fecha
        self.client.post(reverse("inventario:cambiar_estado", args=[self.camara.placa]),
                         {"accion": "devolucion", "fecha": self.hoy.isoformat(), "observaciones": "Devuelta por el cliente"})
        self.camara.refresh_from_db()
        self.assertEqual((self.camara.estado, self.camara.fecha_devolucion), (E.DISPONIBLE, None))
        self.assertNotIn(self.camara, self.client.get(reverse("inventario:prestamos")).context["lista"])
        self.assertEqual(MovimientoActivo.objects.filter(activo=self.camara, tipo=T.INSTALACION).get().fecha_devolucion, self.antier)

    def test_alquiler_sin_fecha_se_lista_aparte(self):
        servicios.mover_activo(self.camara, T.SALIDA, Lugar.CLIENTE, self.bodega, empresa=self.emp_a, modalidad="alquiler")
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:prestamos") + "?ver=sin_fecha")
        self.assertContains(r, "Sin fecha de devolución")
        self.assertEqual(r.context["kpi"]["sin_fecha"], 1)


class CatalogosContactosFuncionariosTests(BaseInventario):
    """Fase 3: marcas y categorías centrales, clientes y proveedores con contacto, funcionarios."""

    def test_marcas_unificar_renombrar_eliminar_y_escritura_oficial(self):
        from . import catalogos as cat
        from .models import Herramienta, Marca
        Marca.objects.create(nombre="Hikvisión")
        Activo.objects.create(placa="CAM-X001", marca="Hikvision")
        Herramienta.objects.create(nombre="Probador", marca="HIKVISION")
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:catalogo", args=["marcas"]))
        self.assertContains(r, "escrito «Hikvision» en 1 registro")
        hik = Marca.objects.get(nombre="Hikvisión")
        self.client.post(reverse("inventario:catalogo", args=["marcas"]), {"accion": "unificar", "id": hik.pk})
        self.assertEqual(set(Activo.objects.values_list("marca", flat=True)) - {""}, {"Hikvisión"})
        self.assertEqual(Herramienta.objects.get().marca, "Hikvisión")
        # Crear una que ya existe con otra escritura avisa; lo que se escribe en compras toma la oficial
        r = self.client.post(reverse("inventario:catalogo", args=["marcas"]), {"accion": "crear", "nombre": "HIKVISION"}, follow=True)
        self.assertContains(r, "Ya existe como «Hikvisión»")
        self.assertEqual(cat.canonica("marcas", "hikvision "), "Hikvisión")
        # Renombrar cambia todos los registros; no se elimina mientras se use
        self.client.post(reverse("inventario:catalogo", args=["marcas"]), {"accion": "renombrar", "id": hik.pk, "nombre": "Hikvision Digital"})
        self.assertEqual(Activo.objects.get(placa="CAM-X001").marca, "Hikvision Digital")
        r = self.client.post(reverse("inventario:catalogo", args=["marcas"]), {"accion": "eliminar", "id": hik.pk}, follow=True)
        self.assertContains(r, "se usa en 3 registro")  # la cámara de la base, la nueva y la herramienta
        libre = Marca.objects.create(nombre="Sin uso")
        self.client.post(reverse("inventario:catalogo", args=["marcas"]), {"accion": "eliminar", "id": libre.pk})
        self.assertFalse(Marca.objects.filter(nombre="Sin uso").exists())
        self.client.force_login(self.agente)  # el técnico consulta pero no cambia
        self.assertEqual(self.client.post(reverse("inventario:catalogo", args=["marcas"]), {"accion": "crear", "nombre": "X"}).status_code, 403)

    def test_categorias_en_la_compra(self):
        from .models import CategoriaActivo
        CategoriaActivo.objects.create(nombre="ENERGÍA")
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:compra"), {
            "tipo": "activo", "a-prefijo": "UPS-1KVA", "a-cantidad_iguales": "1", "a-categoria": "energia", "a-tipo": "UPS",
            "a-marca": "powest", "a-descripcion": "UPS 1 kVA", "a-fecha_compra": "2026-10-22", "a-proveedor": "SYSCOM COLOMBIA",
            "a-factura": "9", "a-valor": "500000", "a-iva": "19"})
        a = Activo.objects.get(placa="UPS-1KVA001")
        self.assertEqual((a.categoria, a.marca), ("ENERGÍA", "powest"))

    def test_cliente_del_inventario_anterior_se_crea_y_se_asocia(self):
        from accounts.models import Empresa
        servicios.mover_activo(self.camara, T.SALIDA, Lugar.CLIENTE, self.bodega, cliente_texto="DIANA CORPORACION SAS", modalidad="venta")
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:contactos"))
        self.assertContains(r, "DIANA CORPORACION SAS")
        self.client.post(reverse("inventario:contacto_nuevo", args=["clientes"]), {
            "razon_social": "DIANA CORPORACION SAS", "nit": "860031606", "email_contacto": "compras@diana.co", "telefono": "6015550000",
            "direccion": "Calle 1", "ciudad": "Villavicencio", "activa": "on", "texto_anterior": "DIANA CORPORACION SAS"})
        diana = Empresa.objects.get(nit="860031606")
        self.camara.refresh_from_db()
        self.assertEqual(self.camara.empresa, diana)
        self.assertEqual(self.camara.movimientos.filter(empresa=diana).count(), 1)
        # Uno mal escrito se asocia a un cliente que ya existe
        servicios.mover_activo(Activo.objects.create(placa="SWC-1"), T.SALIDA, Lugar.CLIENTE, self.bodega,
                               cliente_texto="Venta para Diana", modalidad="venta")
        self.client.post(reverse("inventario:asociar_cliente"), {"texto": "Venta para Diana", "empresa": diana.pk})
        self.assertEqual(Activo.objects.get(placa="SWC-1").empresa, diana)

    def test_proveedor_con_datos_de_contacto(self):
        from .models import Proveedor
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:contacto_nuevo", args=["proveedores"]), {
            "razon_social": "SYSCOM COLOMBIA", "nit": "900000001", "correo": "ventas@syscom.co", "telefono": "3000000000"})
        p = Proveedor.objects.get(razon_social="SYSCOM COLOMBIA")
        self.assertEqual(p.correo, "ventas@syscom.co")
        self.assertContains(self.client.get(reverse("inventario:contactos") + "?tipo=proveedores"), "ventas@syscom.co")

    def test_funcionario_nuevo_sin_acceso_y_la_cedula_asocia_su_historial(self):
        mov = servicios.mover_activo(self.camara, T.ENTREGA, Lugar.TECNICO, self.bodega, modalidad="asignacion",
                                     tecnico_nombre="Chingate Silva Yuri Marcela", tecnico_documento="1121872004")
        self.client.force_login(self.supervisor)
        r = self.client.get(reverse("inventario:funcionarios"))
        self.assertContains(r, "Chingate Silva Yuri Marcela")
        r = self.client.get(reverse("inventario:funcionario_nuevo") + "?nombre=CHINGATE SILVA YURI MARCELA&cedula=1121872004")
        self.assertEqual((r.context["form"].initial["last_name"], r.context["form"].initial["first_name"]), ("Chingate Silva", "Yuri Marcela"))
        self.client.post(reverse("inventario:funcionario_nuevo"), {
            "first_name": "Yuri Marcela", "last_name": "Chingate Silva", "documento": "1.121.872.004", "cargo": "Coordinadora administrativa",
            "area": "Administrativa", "sede": "Principal", "rol": "bodega", "email": "yuri@janotec.test", "is_active": "on"})
        yuri = Usuario.objects.get(documento="1121872004")
        self.assertEqual((yuri.username, yuri.rol, yuri.has_usable_password()), ("ychingate", "bodega", False))
        mov.refresh_from_db()
        self.assertEqual(mov.tecnico, yuri)
        # Con la cédula también se asocia quien ya tenía usuario con otro orden del nombre
        otra = servicios.mover_activo(Activo.objects.create(placa="UPS-9"), T.ENTREGA, Lugar.TECNICO, self.bodega, modalidad="asignacion",
                                      tecnico_nombre="Prada Fierro Yeiver", tecnico_documento="1121839582")
        self.client.post(reverse("inventario:funcionario_editar", args=[self.agente.pk]), {
            "first_name": "Yeiver Arcenio", "last_name": "Prada Fierro", "documento": "1121839582", "rol": "agente", "is_active": "on"})
        otra.refresh_from_db()
        self.assertEqual(otra.tecnico, self.agente)
        r = self.client.post(reverse("inventario:funcionario_nuevo"), {"first_name": "X", "last_name": "Y", "documento": "1121839582", "rol": "agente"})
        self.assertContains(r, "Ya hay un funcionario con esa cédula")

    def test_quien_edita_funcionarios(self):
        director = Usuario.objects.create_user("miguel", "m@janotec.test", "Clave-segura-1", rol="direccion")
        self.client.force_login(self.bodega)
        self.assertEqual(self.client.get(reverse("inventario:funcionarios")).status_code, 200)
        self.assertEqual(self.client.get(reverse("inventario:funcionario_nuevo")).status_code, 403)
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("inventario:funcionario_editar", args=[director.pk])).status_code, 403)
        roles = [v for v, _ in self.client.get(reverse("inventario:funcionario_nuevo")).context["form"].fields["rol"].choices]
        self.assertNotIn("direccion", roles)
        self.assertNotIn("supervisor", roles)


class ProveedoresTests(BaseInventario):
    """Quién nos atiende en cada proveedor, qué marcas maneja y qué nos ha vendido."""

    def setUp(self):
        super().setUp()
        from .models import AsesorProveedor, Marca, Proveedor
        self.p = Proveedor.objects.create(razon_social="SYSCOM COLOMBIA", nit="900")
        self.otro = Proveedor.objects.create(razon_social="GVS COLOMBIA SAS")
        self.ajax = Marca.objects.create(nombre="Ajax")
        Marca.objects.create(nombre="Hikvisión")
        # Las compras guardan el proveedor como texto, a veces escrito distinto
        Activo.objects.filter(pk=self.camara.pk).update(proveedor="syscom  colombia", fecha_compra=date(2026, 5, 2))
        servicios.mover_insumo(self.cable, 10, T.COMPRA, Lugar.PROVEEDOR, Lugar.BODEGA, self.bodega,
                               valor_unitario=Decimal("1200"), iva=19, proveedor="SYSCOM COLOMBIA")
        self.asesor = AsesorProveedor.objects.create(proveedor=self.p, nombre="Laura Díaz", celular="3001112233")
        self.ficha = reverse("inventario:proveedor", args=[self.p.pk])
        self.client.force_login(self.bodega)

    def test_ficha_con_asesor_marcas_y_lo_vendido(self):
        self.p.marcas.add(self.ajax)
        r = self.client.get(self.ficha)
        self.assertContains(r, "Laura Díaz")
        self.assertContains(r, "3001112233")
        self.assertContains(r, "DS2CD1027G2HLIU")  # equipo comprado, aunque el nombre esté escrito distinto
        self.assertContains(r, "CAB-UTP-EXT")
        self.assertContains(r, "Hikvisión")  # marca que salió de la compra
        self.assertContains(r, "Ajax")  # marca indicada a mano
        self.assertNotContains(self.client.get(reverse("inventario:proveedor", args=[self.otro.pk])), "DS2CD1027G2HLIU")

    def test_buscar_por_marca_o_asesor(self):
        url = reverse("inventario:contactos") + "?tipo=proveedores&q="
        for q in ("hikvision", "laura", "syscom"):
            r = self.client.get(url + q)
            self.assertContains(r, "SYSCOM COLOMBIA")
            self.assertNotContains(r, "GVS COLOMBIA")
        self.otro.marcas.add(self.ajax)
        self.assertContains(self.client.get(url + "ajax"), "GVS COLOMBIA")

    def test_cambiar_de_asesor_conserva_el_anterior(self):
        from .models import AsesorProveedor
        self.client.post(reverse("inventario:asesor_editar", args=[self.p.pk, self.asesor.pk]),
                         {"nombre": "Laura Díaz", "celular": "3001112233"})  # desmarca «sigue atendiéndonos»
        self.client.post(reverse("inventario:asesor_nuevo", args=[self.p.pk]),
                         {"nombre": "Pedro Ruiz", "correo": "pedro@syscom.co", "activo": "on"})
        self.assertEqual(self.p.asesor.nombre, "Pedro Ruiz")
        self.assertEqual(AsesorProveedor.objects.filter(proveedor=self.p).count(), 2)
        self.assertContains(self.client.get(self.ficha), "Ya no nos atiende")

    def test_el_producto_dice_quien_lo_vende(self):
        r = self.client.get(reverse("inventario:modelo") + "?ref=DS2CD1027G2HLIU")
        self.assertContains(r, "syscom  colombia")
        self.assertContains(r, "Laura Díaz")
        self.assertContains(r, self.ficha)
        self.assertContains(self.client.get(self.cable.get_absolute_url()), "Laura Díaz")

    def test_guardar_marcas_del_proveedor(self):
        r = self.client.post(reverse("inventario:contacto_editar", args=["proveedores", self.p.pk]),
                             {"razon_social": "SYSCOM COLOMBIA", "nit": "900", "marcas": [self.ajax.pk]})
        self.assertRedirects(r, self.ficha)
        self.assertEqual(list(self.p.marcas.all()), [self.ajax])

    def test_la_orden_de_compra_muestra_al_asesor(self):
        from .models import OrdenCompra
        from .pdf import orden_compra_pdf
        orden = OrdenCompra.objects.create(proveedor=self.p)
        self.assertContains(self.client.get(orden.get_absolute_url()), "Laura Díaz")
        self.assertTrue(orden_compra_pdf(orden)[1].startswith(b"%PDF"))

    def test_el_tecnico_consulta_pero_no_edita(self):
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(self.ficha).status_code, 200)
        self.assertNotContains(self.client.get(self.ficha), "Agregar")
        self.assertEqual(self.client.post(reverse("inventario:asesor_nuevo", args=[self.p.pk]), {"nombre": "X"}).status_code, 403)


class ClientesEnInventarioTests(BaseInventario):
    """Cada cliente con su encargado y sus sedes, con el municipio donde quedan."""

    def setUp(self):
        super().setUp()
        from accounts.models import EncargadoCliente, Sede
        self.gases = Empresa.objects.create(razon_social="Gases del Llano", nit="77")
        self.colono = Sede.objects.create(empresa=self.gases, nombre="EDS Colono", ciudad="Granada", direccion="Km 1 vía San Juan")
        self.sin_ciudad = Sede.objects.create(empresa=self.gases, nombre="El Dorado")
        self.encargado = EncargadoCliente.objects.create(empresa=self.gases, nombre="Paula Parra", celular="3105550000")
        self.ficha = reverse("inventario:cliente", args=[self.gases.pk])
        self.client.force_login(self.bodega)

    def acta(self, numero, sede, ciudad, recibio="", dias=0):
        return ActaSoporte.objects.create(numero=numero, fecha=timezone.now() - timedelta(days=dias), empresa=self.gases,
                                          sede=sede, ciudad=ciudad, recibio=recibio)

    def test_ficha_con_encargado_y_sedes_donde_quedan(self):
        Activo.objects.filter(pk=self.camara.pk).update(lugar=Lugar.CLIENTE, empresa=self.gases, sede=self.colono)
        r = self.client.get(self.ficha)
        for texto in ("Paula Parra", "3105550000", "EDS Colono", "Granada", "Km 1 vía San Juan", "DS2CD1027G2HLIU"):
            self.assertContains(r, texto)
        self.assertContains(r, "1 sede sin municipio")

    def test_buscar_por_sede_municipio_o_encargado(self):
        otro = Empresa.objects.create(razon_social="Otra Empresa SAS", nit="78")
        url = reverse("inventario:contactos") + "?tipo=clientes&q="
        for q in ("colono", "granada", "paula", "gases"):
            r = self.client.get(url + q)
            self.assertContains(r, "Gases del Llano")
            self.assertNotContains(r, otro.razon_social)

    def test_el_municipio_de_las_actas_se_sugiere_y_no_se_guarda_solo(self):
        self.acta(90001, self.sin_ciudad, "Yopal")
        self.acta(90002, self.sin_ciudad, "Yopal", dias=1)
        self.acta(90003, self.sin_ciudad, "CAMARAS", dias=2)
        self.assertContains(self.client.get(self.ficha), "¿Yopal?")
        url = reverse("inventario:sede_editar", args=[self.gases.pk, self.sin_ciudad.pk])
        self.assertContains(self.client.get(url), 'value="Yopal"')
        self.sin_ciudad.refresh_from_db()
        self.assertEqual(self.sin_ciudad.ciudad, "")  # solo con ver no cambia nada
        r = self.client.post(url, {"nombre": "El Dorado", "departamento": "Casanare", "ciudad": "Yopal", "activa": "on"})
        self.assertRedirects(r, self.ficha)
        self.sin_ciudad.refresh_from_db()
        self.assertEqual(self.sin_ciudad.ciudad, "Yopal")

    def test_ultimo_que_recibio_sin_contar_al_personal_ni_pruebas(self):
        self.acta(90004, self.colono, "Granada", "María Paula Parra", dias=5)
        self.acta(90005, self.colono, "Granada", "DANIEL GUANA", dias=1)
        self.acta(90006, self.colono, "Granada", "PRUEBA")
        r = self.client.get(self.ficha)
        self.assertContains(r, "Última acta: María Paula Parra")
        self.assertNotContains(r, "Daniel Guana")

    def test_no_se_repite_el_nombre_de_una_sede_y_se_crea_en_su_cliente(self):
        url = reverse("inventario:sede_nueva", args=[self.gases.pk])
        self.assertContains(self.client.post(url, {"nombre": "eds colono", "activa": "on"}), "ya tiene una sede")
        self.client.post(url, {"nombre": "EDS Nueva", "departamento": "Meta", "ciudad": "Acacías", "activa": "on"})
        self.assertTrue(self.gases.sedes.filter(nombre="EDS Nueva", ciudad="Acacías").exists())

    def test_cambiar_de_encargado_conserva_el_anterior(self):
        self.client.post(reverse("inventario:encargado_editar", args=[self.gases.pk, self.encargado.pk]), {"nombre": "Paula Parra"})
        self.client.post(reverse("inventario:encargado_nuevo", args=[self.gases.pk]), {"nombre": "Luis Mora", "activo": "on"})
        self.assertEqual(self.gases.encargado.nombre, "Luis Mora")
        self.assertContains(self.client.get(self.ficha), "Ya no es el encargado")

    def test_no_se_edita_la_sede_por_el_cliente_equivocado(self):
        otro = Empresa.objects.create(razon_social="Otra", nit="79")
        self.assertEqual(self.client.get(reverse("inventario:sede_editar", args=[otro.pk, self.colono.pk])).status_code, 404)

    def test_el_tecnico_consulta_pero_no_edita(self):
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(self.ficha).status_code, 200)
        self.assertNotContains(self.client.get(self.ficha), "Nueva sede")
        self.assertEqual(self.client.post(reverse("inventario:sede_nueva", args=[self.gases.pk]), {"nombre": "X"}).status_code, 403)

    def test_prospecto_no_tiene_ficha(self):
        prospecto = Empresa.objects.create(razon_social="Prospecto", es_prospecto=True)
        self.assertEqual(self.client.get(reverse("inventario:cliente", args=[prospecto.pk])).status_code, 404)


class MaterialEscritoAManoTests(SolicitudesDeMaterialTests):
    """Lo que no está en el inventario no pasa como si hubiera: va a compra hasta que bodega confirme que lo tiene."""

    def test_escrito_a_mano_va_a_compra(self):
        from .models import SolicitudMaterial
        a = self.programar("3 Computador Lenovo ThinkCentre")
        s = SolicitudMaterial.objects.get(actividad=a)
        item = s.items.get()
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]),
                         {"cargo": "janotec", "modalidad": "venta", "tecnico": self.agente.pk})
        s.refresh_from_db()
        self.assertEqual(s.estado, "compra")
        self.assertContains(self.client.get(reverse("inventario:solicitud", args=[s.pk])), "ya lo tengo en bodega")
        # Ya lo compró: lo marca y la solicitud sigue a firmas
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "validar"]),
                         {"cargo": "janotec", "modalidad": "venta", "tecnico": self.agente.pk, f"listo_{item.pk}": "on"})
        s.refresh_from_db()
        self.assertEqual(s.estado, "aprobacion")


class CoordinacionSinModuloTests(SolicitudesDeMaterialTests):
    """Coordinación no entra al módulo de inventario: ve el alistamiento en el ticket y administra funcionarios."""

    def test_coordinacion_solo_ve_la_lista_en_el_ticket(self):
        self.client.force_login(self.supervisor)
        for nombre in ("inventario:panel", "inventario:solicitudes", "inventario:entrega", "inventario:compra",
                       "inventario:herramientas", "inventario:contactos"):
            self.assertEqual(self.client.get(reverse(nombre)).status_code, 403, nombre)
        inicio = self.client.get(reverse("panel"))
        self.assertNotContains(inicio, f'href="{reverse("inventario:panel")}"')
        self.assertContains(inicio, reverse("inventario:funcionarios"))
        self.assertEqual(self.client.get(reverse("inventario:funcionarios")).status_code, 200)
        # Desde el ticket sí ve la lista y autoriza la salida
        self.programar("1 Cámara Bala Hikvisión DS2CD1043G2LIU [DS2CD1043G2LIU]")
        self.assertContains(self.client.get(self.ticket.get_absolute_url()), "Alistamiento de materiales y equipos")


class ApartarYCuadrarTests(BaseInventario):
    """Lo validado por bodega queda apartado para su ticket; lo entregado se cuadra: instalado + devuelto."""

    def test_lo_validado_queda_apartado_para_su_ticket(self):
        from . import solicitudes as sol
        from .models import ItemSolicitud, SolicitudMaterial
        primera = self.solicitud_aprobada(cantidad="300", camaras=1)  # 300 de los 305 m y la única cámara
        self.assertEqual(sol.total_apartado(), {"CAB-UTP-EXT": 300, "DS2CD1027G2HLIU": 1})
        otra = SolicitudMaterial.objects.create(ticket=self.ticket, origen="tecnica", estado="validacion", cargo="janotec",
                                                modalidad="venta", tecnico=self.agente)
        ItemSolicitud.objects.create(solicitud=otra, insumo=self.cable, descripcion="Cable UTP", cantidad=10)
        ItemSolicitud.objects.create(solicitud=otra, referencia="DS2CD1027G2HLIU", descripcion="Cámara bala", cantidad=1)
        filas = {f["item"].descripcion: f for f in sol.existencias(otra)}
        self.assertEqual((filas["Cable UTP"]["hay"], filas["Cable UTP"]["falta"]), (5, 5))
        self.assertEqual((filas["Cámara bala"]["hay"], filas["Cámara bala"]["falta"]), (0, 1))
        self.assertEqual(filas["Cable UTP"]["para_quien"], [primera.numero])
        # Para su propio ticket sí está todo
        self.assertFalse(sol.faltantes(primera))
        # Lo que se puede planear en el cronograma descuenta lo apartado
        cable = next(i for i in servicios.catalogo_planeacion() if i["codigo"] == "CAB-UTP-EXT")
        self.assertEqual(cable["disponible"], 5)
        # Y la entrega de otra solicitud no se lo puede llevar
        otra.estado = "aprobada"
        otra.save()
        errores = sol.permitido_entregar(otra, [self.camara], [(self.cable, Decimal(10))])
        self.assertTrue(any("apartado" in e for e in errores), errores)
        # Cuando la primera sale de bodega, deja de estar apartado: la otra solo aparta lo que de verdad queda
        self.entregar(solicitud=primera, cantidad=["300"], modalidad="alquiler")
        self.assertEqual(sol.total_apartado(), {"CAB-UTP-EXT": 5})

    def test_cuadre_del_material_del_ticket(self):
        from tickets.diagnostico import material_sin_cuadrar

        from . import solicitudes as sol
        s = self.solicitud_aprobada(cantidad="40", camaras=1)
        self.entregar(solicitud=s)
        c = sol.cuadre(self.ticket)
        self.assertEqual(len(c["pendientes"]), 2)
        # El técnico descuenta en el acta: instaló la cámara y 30 m
        servicios.mover_activo(self.camara, T.INSTALACION, Lugar.CLIENTE, self.agente, tecnico=self.agente, ticket=self.ticket)
        servicios.mover_insumo(self.cable, 30, T.INSTALACION, Lugar.TECNICO, Lugar.CLIENTE, self.agente, tecnico=self.agente,
                               ticket=self.ticket)
        c = sol.cuadre(self.ticket)
        self.assertEqual([(f["codigo"], f["pendiente"]) for f in c["pendientes"]], [("CAB-UTP-EXT", 10)])
        # Si se cierra así, «Salud de los procesos» lo señala
        self.ticket.estado = Ticket.Estado.CERRADO
        self.ticket.save()
        self.assertEqual(len(material_sin_cuadrar()), 1)
        self.assertIn("10 Cable UTP exterior", sol.resumen_pendiente(self.ticket))
        # Bodega recibe el sobrante indicando el ticket (viene precargado) y queda cuadrado
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:devolucion", args=[self.agente.pk]) + f"?ticket={self.ticket.pk}")
        self.assertEqual(r.context["sugerido"], {"CAB-UTP-EXT": 10})
        self.client.post(reverse("inventario:devolucion", args=[self.agente.pk]),
                         {"ticket": self.ticket.pk, f"cantidad_{self.cable.pk}": "10", "observaciones": "Sobró"})
        c = sol.cuadre(self.ticket)
        self.assertTrue(c["cuadrado"])
        self.assertEqual(material_sin_cuadrar(), [])


class EquiposRetiradosTests(BaseInventario):
    """Desmonte: el técnico registra en el acta lo que retiró. Lo del cliente se le entrega; lo de JANOTEC vuelve a bodega."""

    def test_retirados_del_cliente_y_de_janotec(self):
        from tickets.models import ActaSoporte, EquipoActa
        # Una cámara de JANOTEC que estaba alquilada en el cliente
        servicios.mover_activo(self.camara, T.SALIDA, Lugar.CLIENTE, self.bodega, estado=E.ALQUILADO, empresa=self.ticket.empresa)
        self.client.force_login(self.agente)
        # En «Equipos intervenidos», con «¿Qué se hizo? → Se retiró»
        filas = {"equipos-TOTAL_FORMS": "4",
                 "equipos-1-equipo": "Panel de alarma", "equipos-1-marca": "Honeywell", "equipos-1-serial": "D60433586",
                 "equipos-1-accion": "retirado", "equipos-1-propiedad": "cliente", "equipos-1-observaciones": "Funciona",
                 "equipos-2-equipo": "Cámara bala", "equipos-2-accion": "retirado", "equipos-2-propiedad": "janotec",
                 "equipos-2-placa": "cam-bal-2mp002",
                 "equipos-3-equipo": "Router", "equipos-3-accion": "retirado", "equipos-3-propiedad": "janotec",
                 "equipos-3-placa": "NO-EXISTE"}
        # De JANOTEC sin placa ni serial no se puede devolver al inventario
        r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(**{
            **filas, "equipos-2-placa": ""}))
        self.assertContains(r, "escriba la placa")
        r = self.client.post(reverse("acta_nueva", args=[self.ticket.radicado]), self.datos_acta(**filas))
        self.assertEqual(r.status_code, 302)
        acta = ActaSoporte.objects.get(ticket=self.ticket)
        self.assertEqual([(e.equipo, e.propiedad, e.placa) for e in acta.equipos_retirados],
                         [("Panel de alarma", "cliente", ""), ("Cámara bala", "janotec", "CAM-BAL-2MP002"),
                          ("Router", "janotec", "NO-EXISTE")])
        # La cámara de JANOTEC queda a nombre del técnico hasta que bodega la recibe
        self.camara.refresh_from_db()
        self.assertEqual((self.camara.lugar, self.camara.tecnico, self.camara.estado), (Lugar.TECNICO, self.agente, E.EN_TECNICO))
        self.assertContains(self.client.get(acta.get_absolute_url()), "Equipos retirados")
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:devolucion", args=[self.agente.pk]), {"activo": [self.camara.pk]})
        self.camara.refresh_from_db()
        self.assertEqual((self.camara.lugar, self.camara.estado), (Lugar.BODEGA, E.DISPONIBLE))
        self.assertEqual(acta.equipos.filter(clase=EquipoActa.Clase.RETIRADO).count(), 3)
        self.assertEqual([e.equipo for e in acta.equipos_intervenidos], ["Cámara"])  # la fila que solo se revisó


class StockMinimoTests(BaseInventario):
    """Bodega fija el mínimo; una vez por semana le llega lo que hay que reponer (contando lo libre)."""

    def test_minimo_y_alerta_semanal(self):
        from django.core import mail

        from . import alertas
        self.client.force_login(self.bodega)
        self.assertEqual(self.client.get(reverse("inventario:stock_minimo")).status_code, 200)
        mail.outbox.clear()
        self.client.post(reverse("inventario:stock_minimo"), {f"minimo_{self.cable.pk}": "400"})
        self.cable.refresh_from_db()
        self.assertEqual(self.cable.stock_minimo, 400)
        self.assertEqual([f["insumo"] for f in alertas.por_reponer()], [self.cable])
        self.assertEqual(alertas.enviar(), 1)
        self.assertTrue(any("b@janotec.test" in m.to and "por reponer" in m.subject for m in mail.outbox))
        self.assertEqual(alertas.enviar(), 0)  # una sola vez por semana
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("inventario:stock_minimo")).status_code, 403)


class CotizacionesTests(BaseInventario):
    """Preguntar precios no compromete: de la cotización elegida sale la orden de compra."""

    def preparar(self):
        from .models import Proveedor, SolicitudCotizacion
        s = self.solicitud_aprobada(cantidad="400", camaras=0)  # hay 305 m: faltan 95
        s.estado = "compra"
        s.save()
        self.a = Proveedor.objects.create(razon_social="PROVEEDOR A", nit="901")
        self.b = Proveedor.objects.create(razon_social="PROVEEDOR B", nit="902")
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:cotizacion_nueva"), {"solicitud": s.pk})
        return s, SolicitudCotizacion.objects.get()

    def envio(self, p, accion="enviar"):
        l = p.lineas.get()
        return {"lineas-TOTAL_FORMS": "1", "lineas-INITIAL_FORMS": "1", "lineas-MIN_NUM_FORMS": "0", "lineas-MAX_NUM_FORMS": "1000",
                "lineas-0-id": l.pk, "lineas-0-solicitud": p.pk, "lineas-0-descripcion": l.descripcion, "lineas-0-cantidad": "95",
                "lineas-0-adicional": "0", "accion": accion}

    def responder(self, c, **precios):
        return self.client.post(reverse("inventario:cotizacion_respuesta", args=[c.solicitud_id, c.pk]),
                                {"fecha": "2026-10-30", "fecha_entrega": "2026-11-05", "condiciones": "Pago a 30 días", **precios}, follow=True)

    def test_de_la_solicitud_a_la_orden(self):
        from .models import OrdenCompra
        s, p = self.preparar()
        self.assertEqual([(l.descripcion, l.cantidad) for l in p.lineas.all()], [("Cable UTP", 95)])
        # Cada elemento va a un proveedor: sin elegirlo no se envía
        self.assertContains(self.client.post(p.get_absolute_url(), self.envio(p), follow=True), "Elija el proveedor de: Cable UTP")
        self.client.post(p.get_absolute_url(), {**self.envio(p), "lineas-0-proveedor": self.a.pk})
        p.refresh_from_db()
        self.assertEqual(p.estado, "enviada")
        a = p.cotizaciones.get()  # el proveedor sale de la línea
        self.assertEqual(a.proveedor, self.a)
        self.assertEqual(self.client.get(reverse("inventario:cotizacion_pdf", args=[p.pk, a.pk]))["Content-Type"], "application/pdf")
        # Sin respuesta del proveedor no hay de dónde sacar la orden
        self.client.post(reverse("inventario:cotizacion_ordenar", args=[p.pk, a.pk]))
        self.assertFalse(OrdenCompra.objects.exists())
        linea = p.lineas.get()
        for malo in ("abc", "NaN", "Infinity", "-5", "0"):
            self.assertContains(self.responder(a, **{f"valor_{linea.pk}": malo, f"iva_{linea.pk}": "19"}), "Revise los precios")
        self.assertContains(self.responder(a), "al menos un producto")
        self.responder(a, **{f"valor_{linea.pk}": "900", f"iva_{linea.pk}": "19"})
        self.client.post(reverse("inventario:cotizacion_ordenar", args=[p.pk, a.pk]))
        orden = OrdenCompra.objects.get()
        self.assertEqual((orden.proveedor, orden.estado, orden.cotizacion, orden.solicitud, str(orden.fecha_estimada), orden.notas),
                         (self.a, "borrador", a, s, "2026-11-05", "Pago a 30 días"))
        self.assertEqual([(l.cantidad, l.valor_unitario, l.item_id) for l in orden.lineas.all()], [(95, 900, linea.item_id)])
        p.refresh_from_db()
        self.assertEqual(p.estado, "cerrada")
        # Doble clic: no sale una segunda orden, ni se cambia la cotización ya ordenada
        self.client.post(reverse("inventario:cotizacion_ordenar", args=[p.pk, a.pk]))
        self.assertEqual(OrdenCompra.objects.count(), 1)
        self.responder(a, **{f"valor_{linea.pk}": "1", f"iva_{linea.pk}": "19"})
        self.assertEqual(a.precios.get().valor_unitario, 900)
        # Si se cancela la orden, se puede volver a elegir
        self.client.post(orden.get_absolute_url(), {"accion": "cancelar"})
        p.refresh_from_db()
        self.assertEqual(p.estado, "enviada")
        self.client.post(reverse("inventario:cotizacion_ordenar", args=[p.pk, a.pk]))
        self.assertEqual(OrdenCompra.objects.exclude(estado="cancelada").get().proveedor, self.a)

    def test_comprarle_a_dos_proveedores(self):
        from .models import CotizacionProveedor, LineaSolicitudCotizacion, OrdenCompra
        s, p = self.preparar()
        l1 = p.lineas.get()
        l2 = LineaSolicitudCotizacion.objects.create(solicitud=p, descripcion="Canaleta", adicional=10)
        a, b = (CotizacionProveedor.objects.create(solicitud=p, proveedor=x) for x in (self.a, self.b))
        p.estado = "enviada"
        p.save()
        self.responder(a, **{f"valor_{l1.pk}": "1000", f"iva_{l1.pk}": "19", f"valor_{l2.pk}": "500", f"iva_{l2.pk}": "19"})
        self.responder(b, **{f"valor_{l1.pk}": "900", f"iva_{l1.pk}": "19"})
        self.client.post(reverse("inventario:cotizacion_ordenar", args=[p.pk, b.pk]))
        p.refresh_from_db()
        self.assertEqual(p.estado, "enviada")
        self.client.post(reverse("inventario:cotizacion_ordenar", args=[p.pk, a.pk]))
        p.refresh_from_db()
        self.assertEqual(p.estado, "cerrada")
        self.assertEqual({(o.proveedor, l.descripcion, l.cantidad, l.adicional) for o in OrdenCompra.objects.all() for l in o.lineas.all()},
                         {(self.b, "Cable UTP", 95, 0), (self.a, "Canaleta", 0, 10)})

    def test_quitar_de_la_orden_lo_que_conviene_comprarle_a_otro(self):
        from .models import CotizacionProveedor, LineaSolicitudCotizacion, OrdenCompra
        s, p = self.preparar()
        l1 = p.lineas.get()
        l2 = LineaSolicitudCotizacion.objects.create(solicitud=p, descripcion="Canaleta", adicional=10)
        a, b = (CotizacionProveedor.objects.create(solicitud=p, proveedor=x) for x in (self.a, self.b))
        p.estado = "enviada"
        p.save()
        for c, v1, v2 in ((a, "1000", "500"), (b, "900", "600")):
            self.responder(c, **{f"valor_{l1.pk}": v1, f"iva_{l1.pk}": "19", f"valor_{l2.pk}": v2, f"iva_{l2.pk}": "19"})
        self.client.post(reverse("inventario:cotizacion_ordenar", args=[p.pk, b.pk]))
        p.refresh_from_db()
        self.assertEqual(p.estado, "cerrada")
        orden = OrdenCompra.objects.get()
        cable, canaleta = orden.lineas.all()
        fila = {"id": "", "orden": orden.pk, "cantidad": "0", "iva": "19"}
        datos = {"lineas-TOTAL_FORMS": "2", "lineas-INITIAL_FORMS": "2", "lineas-MIN_NUM_FORMS": "0", "lineas-MAX_NUM_FORMS": "1000",
                 "accion": "guardar"}
        for i, (l, extra) in enumerate(((cable, {}), (canaleta, {"DELETE": "on"}))):
            datos.update({f"lineas-{i}-{k}": v for k, v in {**fila, "id": l.pk, "descripcion": l.descripcion, "cantidad": l.cantidad,
                                                             "adicional": l.adicional, "valor_unitario": l.valor_unitario, **extra}.items()})
        self.client.post(orden.get_absolute_url(), datos)
        self.assertEqual(orden.lineas.count(), 1)
        p.refresh_from_db()
        self.assertEqual(p.estado, "enviada")
        self.client.post(reverse("inventario:cotizacion_ordenar", args=[p.pk, a.pk]))
        p.refresh_from_db()
        self.assertEqual(p.estado, "cerrada")
        self.assertEqual([l.descripcion for l in OrdenCompra.objects.get(proveedor=self.a).lineas.all()], ["Canaleta"])

    def test_solo_bodega_entra_a_las_cotizaciones(self):
        from .models import CotizacionProveedor
        s, p = self.preparar()
        c = CotizacionProveedor.objects.create(solicitud=p, proveedor=self.a)
        self.client.force_login(self.agente)
        self.assertEqual(self.client.get(reverse("inventario:cotizaciones")).status_code, 403)
        self.assertEqual(self.client.get(reverse("inventario:cotizacion_pdf", args=[p.pk, c.pk])).status_code, 403)

    def test_trae_del_informe_del_ticket_y_marca_en_rojo_lo_que_falta(self):
        from decimal import Decimal

        from comercial.models import FichaComercial, InformeRequerimiento, ItemCatalogo
        from .models import SolicitudCotizacion
        ficha = FichaComercial.objects.create(ticket=self.ticket, etapa="elaborando")
        InformeRequerimiento.objects.create(ficha=ficha, necesidad="Cámaras en el parqueadero", servicios="1 Instalación",
                                            materiales="4 Cámara IP 4 MP bala\n120 m Cable UTP cat 6\n1 Switch PoE 8 puertos")
        ItemCatalogo.objects.create(descripcion="Cable UTP cat 6 exterior", valor_unitario=Decimal("1500"))
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:cotizaciones"))
        self.assertContains(r, self.ticket.radicado)
        self.assertContains(r, "Pedir cotización a proveedores")
        self.client.post(reverse("inventario:cotizacion_nueva"), {"propuesta": ficha.pk})
        p = SolicitudCotizacion.objects.get()
        # Lo que pide el informe (sin los servicios), con su cantidad y si es equipo o material
        self.assertEqual([(l.tipo, l.descripcion, l.cantidad) for l in p.lineas.all()],
                         [("equipo", "Cámara IP 4 MP bala", 4), ("material", "Cable UTP cat 6", 120),
                          ("equipo", "Switch PoE 8 puertos", 1)])
        # El cable trae el precio del catálogo; lo que no tiene precio ni proveedor sale en rojo
        self.assertEqual([l.precio_referencia for l in p.lineas.all()], [None, Decimal("1500"), None])
        self.assertTrue(self.ticket.seguimientos.filter(interno=True, mensaje__contains=p.numero).exists())
        r = self.client.get(p.get_absolute_url())
        self.assertContains(r, "Para armar la propuesta del ticket")
        self.assertContains(r, "table-danger", count=3)
        self.assertContains(r, "3 elementos en rojo")

    def test_un_pdf_y_un_correo_por_proveedor_solo_con_lo_suyo(self):
        from django.core import mail

        from .models import AsesorProveedor, LineaOrdenCompra, LineaSolicitudCotizacion, OrdenCompra, Proveedor
        # Historial: el cable UTP se le compró a este proveedor a $1.000
        habitual = Proveedor.objects.create(razon_social="PROVEEDOR HABITUAL", nit="903", correo="ventas@habitual.test")
        historial = OrdenCompra.objects.create(proveedor=habitual, estado="recibida")
        LineaOrdenCompra.objects.create(orden=historial, descripcion="Cable UTP", cantidad=10, valor_unitario=1000)
        s, p = self.preparar()
        cable = p.lineas.get()
        self.assertEqual((cable.proveedor, cable.precio_referencia), (habitual, 1000))
        AsesorProveedor.objects.create(proveedor=self.b, nombre="Laura", celular="300 111 2233")
        datos = {**self.envio(p), "lineas-0-tipo": "material", "lineas-0-proveedor": habitual.pk, "lineas-0-precio_referencia": "1000"}
        # Cámara y canaleta a B, cable al habitual: dos solicitudes, cada una con lo suyo
        camara = LineaSolicitudCotizacion.objects.create(solicitud=p, tipo="equipo", descripcion="Cámara bala", adicional=2,
                                                         proveedor=self.b)
        canaleta = LineaSolicitudCotizacion.objects.create(solicitud=p, descripcion="Canaleta", adicional=10, proveedor=self.b)
        mail.outbox.clear()
        r = self.client.post(p.get_absolute_url(), datos, follow=True)
        self.assertEqual(set(p.cotizaciones.values_list("proveedor__razon_social", flat=True)), {"PROVEEDOR HABITUAL", "PROVEEDOR B"})
        self.assertEqual(p.lineas_para(habitual), [cable])
        self.assertEqual(p.lineas_para(self.b), [camara, canaleta])
        # Al habitual le llega su PDF por correo; B no tiene correo: se avisa y queda su WhatsApp
        self.assertEqual([m.to for m in mail.outbox], [["ventas@habitual.test"]])
        self.assertEqual(mail.outbox[0].attachments[0][0], f"Solicitud_de_cotizacion_{p.numero}_PROVEEDOR_HABITUAL.pdf")
        self.assertContains(r, "Sin correo")
        self.assertContains(r, "https://wa.me/573001112233")
        b = p.cotizaciones.get(proveedor=self.b)
        self.assertEqual(self.client.get(reverse("inventario:cotizacion_pdf", args=[p.pk, b.pk]))["Content-Type"], "application/pdf")

class OrdenesDeCompraTests(BaseInventario):
    """Bodega compra: la orden lleva proveedor, costo y fecha estimada, y coordinación ve cuándo llega."""

    def test_orden_desde_lo_que_falta(self):
        from datetime import timedelta

        from django.utils import timezone

        from .models import OrdenCompra, Proveedor
        s = self.solicitud_aprobada(cantidad="400", camaras=0)  # hay 305 m: faltan 95
        s.estado = "compra"
        s.save()
        proveedor = Proveedor.objects.create(razon_social="SYSCOM COLOMBIA", nit="900")
        self.client.force_login(self.bodega)
        r = self.client.post(reverse("inventario:orden_nueva"), {"solicitud": s.pk}, follow=True)
        orden = OrdenCompra.objects.get()
        self.assertEqual([(l.descripcion, l.cantidad) for l in orden.lineas.all()], [("Cable UTP", 95)])
        datos = {"lineas-TOTAL_FORMS": "1", "lineas-INITIAL_FORMS": "1", "lineas-MIN_NUM_FORMS": "0", "lineas-MAX_NUM_FORMS": "1000",
                 "lineas-0-id": orden.lineas.get().pk, "lineas-0-orden": orden.pk, "lineas-0-descripcion": "Cable UTP exterior",
                 "lineas-0-cantidad": "95", "lineas-0-valor_unitario": "1000", "lineas-0-iva": "19", "accion": "a_direccion"}
        r = self.client.post(orden.get_absolute_url(), datos, follow=True)
        self.assertContains(r, "falta el proveedor")
        llega = timezone.localdate() + timedelta(days=3)
        # Urgente (dirección no está): pasa con el motivo, sin esperar; financiera paga y bodega la envía
        datos.update(accion="a_direccion", urgente="1", motivo_urgencia="La instalación es el lunes")
        self.client.post(orden.get_absolute_url(), {**datos, "proveedor": proveedor.pk, "fecha_estimada": f"{llega:%Y-%m-%d}"})
        orden.refresh_from_db()
        self.assertEqual((orden.estado, orden.totales()["total"], orden.motivo_urgencia), ("por_pagar", 113050, "La instalación es el lunes"))
        from .financiera import enviar_al_proveedor, registrar_pago
        from django.core.files.uploadedfile import SimpleUploadedFile
        registrar_pago(orden, self.bodega, timezone.localdate(), Decimal("113050"), "Transferencia",
                       SimpleUploadedFile("pago.pdf", b"%PDF-1.4 pago", content_type="application/pdf"))
        enviar_al_proveedor(orden, self.bodega)
        orden.refresh_from_db()
        self.assertEqual(orden.estado, "enviada")
        self.assertEqual(self.client.get(reverse("inventario:orden_archivo", args=[orden.pk, "pdf"]))["Content-Type"], "application/pdf")
        # Coordinación lo ve en la lista de alistamiento del ticket
        self.client.force_login(self.supervisor)
        self.assertContains(self.client.get(self.ticket.get_absolute_url()), f"llega el {llega:%d/%m/%Y}")
        # Llegó la factura: lo del ticket, 10 m adicionales para bodega, una canaleta nueva y 2 cámaras que pidió logística
        from .models import Activo, Insumo
        self.client.force_login(self.bodega)
        linea = orden.lineas.get()
        linea.adicional = 10
        linea.save()
        url = reverse("inventario:orden_recibir", args=[orden.pk])
        r = self.client.get(url)
        self.assertEqual(r.context["filas"].initial[0]["cantidad"], 105)  # 95 del ticket + 10 adicionales
        self.assertEqual(r.context["filas"].initial[0]["codigo"], "CAB-UTP-EXT")
        fila = {"linea": "", "clase": "insumo", "descripcion": "", "cantidad": "", "valor_unitario": "", "iva": "19",
                "codigo": "", "unidad": "Unidad", "prefijo": "", "referencia": "", "marca": "", "seriales": ""}
        filas = [{**fila, "linea": linea.pk, "descripcion": "Cable UTP exterior", "cantidad": "105", "valor_unitario": "1000",
                  "codigo": "CAB-UTP-EXT"},
                 {**fila, "descripcion": "Canaleta 20x12", "cantidad": "10", "valor_unitario": "9635", "codigo": "CAN-20X12",
                  "unidad": "Metro"},
                 {**fila, "clase": "equipo", "descripcion": "Cámara bala 2MP", "cantidad": "2", "valor_unitario": "180000",
                  "prefijo": "CAM-PRUEBA-", "referencia": "REF-NUEVA", "seriales": "S-001\nS-002"},
                 fila]
        datos = {"filas-TOTAL_FORMS": str(len(filas)), "filas-INITIAL_FORMS": str(len(filas)), "filas-MIN_NUM_FORMS": "0",
                 "filas-MAX_NUM_FORMS": "1000", "factura": "FE-4512", "fecha": f"{timezone.localdate():%Y-%m-%d}",
                 "proveedor": "SYSCOM COLOMBIA"}
        for i, f in enumerate(filas):
            datos.update({f"filas-{i}-{k}": v for k, v in f.items()})
        r = self.client.post(url, datos, follow=True)
        self.assertContains(r, "Factura FE-4512 recibida")
        orden.refresh_from_db()
        s.refresh_from_db()
        self.assertEqual((orden.estado, orden.factura, orden.lineas.count()), ("recibida", "FE-4512", 3))
        self.assertEqual(s.estado, "aprobacion")  # ya tiene lo del ticket: pasa a firmas
        self.assertEqual(servicios.disponible_en(self.cable, Lugar.BODEGA), 410)  # 305 + 105
        self.assertEqual(servicios.disponible_en(Insumo.objects.get(codigo="CAN-20X12"), Lugar.BODEGA), 10)
        self.assertEqual(sorted(Activo.objects.filter(referencia="REF-NUEVA").values_list("placa", "serial")),
                         [("CAM-PRUEBA-001", "S-001"), ("CAM-PRUEBA-002", "S-002")])
        # Lo adicional queda libre: de los 410 m, 400 están apartados para la solicitud y 10 quedan para otro ticket
        from . import solicitudes as sol
        self.assertEqual(sol.apartados()["CAB-UTP-EXT"][0][1], 400)
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("inventario:ordenes")).status_code, 403)


class CompraConFinancieraTests(BaseInventario):
    """La orden de compra pasa por dirección, financiera paga y valida la factura, bodega envía y recibe."""

    def setUp(self):
        super().setUp()
        from .models import AsesorProveedor, LineaOrdenCompra, OrdenCompra, Proveedor
        self.miguel = Usuario.objects.create_user("miguel", "dir@janotec.test", "Clave-segura-1", rol="direccion")
        self.financiera = Usuario.objects.create_user("fin", "fin@janotec.test", "Clave-segura-1", rol="financiera")
        self.proveedor = Proveedor.objects.create(razon_social="SYSCOM COLOMBIA", nit="900", correo="ventas@syscom.test")
        AsesorProveedor.objects.create(proveedor=self.proveedor, nombre="Laura", celular="300 111 2233")
        self.orden = OrdenCompra.objects.create(proveedor=self.proveedor, creado_por=self.bodega,
                                                fecha_estimada=timezone.localdate())
        self.linea = LineaOrdenCompra.objects.create(orden=self.orden, descripcion="Cable UTP", cantidad=100, valor_unitario=1000)

    def datos(self, **extra):
        o, l = self.orden, self.linea
        return {"proveedor": self.proveedor.pk, "fecha_estimada": f"{o.fecha_estimada:%Y-%m-%d}", "forma_pago": o.forma_pago,
                "dias_credito": o.dias_credito or "", "notas": "",
                "lineas-TOTAL_FORMS": "1", "lineas-INITIAL_FORMS": "1", "lineas-MIN_NUM_FORMS": "0", "lineas-MAX_NUM_FORMS": "1000",
                "lineas-0-id": l.pk, "lineas-0-orden": o.pk, "lineas-0-descripcion": l.descripcion, "lineas-0-cantidad": "100",
                "lineas-0-adicional": "0", "lineas-0-valor_unitario": "1000", "lineas-0-iva": "19", **extra}

    def pdf(self, nombre="soporte.pdf"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        return SimpleUploadedFile(nombre, b"%PDF-1.4 x", content_type="application/pdf")

    def test_anticipada_de_bodega_a_la_factura(self):
        from django.core import mail
        url, hoy = self.orden.get_absolute_url(), timezone.localdate()
        paso = lambda cual: reverse("inventario:orden_paso", args=[self.orden.pk, cual])  # noqa: E731
        # Bodega la envía a dirección; le llega el aviso y bodega ya no la modifica
        self.client.force_login(self.bodega)
        mail.outbox.clear()
        self.client.post(url, self.datos(accion="a_direccion"))
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, "revision")
        self.assertTrue(any("dir@janotec.test" in m.to and "para aprobar" in m.subject for m in mail.outbox))
        self.client.post(url, self.datos(**{"lineas-0-valor_unitario": "5"}))
        self.linea.refresh_from_db()
        self.assertEqual(self.linea.valor_unitario, 1000)
        # Dirección la devuelve con observaciones; bodega corrige y la vuelve a enviar; dirección la aprueba
        self.client.force_login(self.miguel)
        self.client.post(paso("devolver"), {"observaciones": "Pedir 10 m más para bodega"})
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.observaciones_direccion), ("borrador", "Pedir 10 m más para bodega"))
        self.client.force_login(self.bodega)
        self.assertContains(self.client.get(url), "Pedir 10 m más para bodega")
        self.client.post(url, self.datos(accion="a_direccion", **{"lineas-0-adicional": "10"}))
        self.client.force_login(self.miguel)
        mail.outbox.clear()
        self.client.post(url, self.datos(accion="aprobar", **{"lineas-0-adicional": "10"}))
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.aprobada_por), ("por_pagar", self.miguel))
        aviso = next(m for m in mail.outbox if "fin@janotec.test" in m.to)
        self.assertIn("por pagar", aviso.subject)
        self.assertEqual(aviso.attachments[0][2], "application/pdf")  # la orden para pagar
        # Bodega no puede enviarla al proveedor ni recibir sin el pago
        self.client.force_login(self.bodega)
        self.client.post(paso("proveedor"))
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, "por_pagar")
        self.assertEqual(self.client.get(reverse("inventario:orden_recibir", args=[self.orden.pk])).status_code, 302)
        # Financiera: la ve en su módulo y registra el pago con el soporte
        self.client.force_login(self.financiera)
        r = self.client.get(reverse("inventario:financiera"))
        self.assertEqual(r.context["anticipadas"], [self.orden])
        self.assertContains(r, 'title="Órdenes de compra por pagar">1<')
        self.assertEqual(self.client.get(reverse("inventario:ordenes")).status_code, 403)  # no es bodega
        self.client.post(paso("pago"), {"fecha": f"{hoy:%Y-%m-%d}", "valor": "130900", "medio": "Transferencia"})
        self.orden.refresh_from_db()
        self.assertFalse(self.orden.pagada)  # sin soporte no
        mail.outbox.clear()
        self.client.post(paso("pago"), {"fecha": f"{hoy:%Y-%m-%d}", "valor": "130900", "medio": "Transferencia",
                                        "soporte_pago": self.pdf("pago.pdf")})
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.valor_pagado, self.orden.pago_por), ("lista", 130900, self.financiera))
        self.assertTrue(any("b@janotec.test" in m.to and "reenvíe el soporte" in m.subject for m in mail.outbox))
        # Bodega se la envía al proveedor con la orden y el soporte de pago
        self.client.force_login(self.bodega)
        self.assertContains(self.client.get(url), "https://wa.me/573001112233")
        mail.outbox.clear()
        self.client.post(paso("proveedor"))
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, "enviada")
        correo = next(m for m in mail.outbox if "ventas@syscom.test" in m.to)
        self.assertEqual(len(correo.attachments), 2)  # la orden y el soporte de pago
        # Llega: se recibe en bodega y financiera recibe el aviso para validar la factura
        from . import recepcion
        mail.outbox.clear()
        recepcion.recibir(self.orden, {"proveedor": "SYSCOM COLOMBIA", "factura": "FE-1", "fecha": hoy},
                          [{"linea": self.linea.pk, "clase": "insumo", "descripcion": "Cable UTP", "cantidad": Decimal("110"),
                            "valor_unitario": Decimal("1000"), "iva": Decimal("19"), "codigo": "CAB-UTP-EXT", "unidad": "Metro"}],
                          self.bodega)
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, "recibida")
        # Factura con diferencia: queda marcada; financiera la acepta con una nota y la compra queda cerrada
        self.client.force_login(self.financiera)
        self.client.post(paso("factura"), {"numero": "FE-1", "fecha": f"{hoy:%Y-%m-%d}", "valor": "150000",
                                           "factura_pdf": self.pdf("fe1.pdf")})
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.factura_cuadra, self.orden.factura_validada_en, self.orden.cerrada), (False, None, False))
        self.assertEqual(self.client.get(reverse("inventario:financiera")).context["con_diferencia"], [self.orden])
        self.client.post(paso("factura"), {"numero": "FE-1", "fecha": f"{hoy:%Y-%m-%d}", "valor": "150000", "acepta": "1",
                                           "nota": "Incluye el flete"})
        self.orden.refresh_from_db()
        self.assertTrue(self.orden.cerrada)
        self.assertEqual(self.client.get(reverse("inventario:financiera")).context["cerradas"], [self.orden])

    def test_credito_va_al_proveedor_sin_pagar_y_vence_con_la_factura(self):
        from datetime import timedelta
        self.orden.forma_pago, self.orden.dias_credito = "credito", None
        self.orden.save()
        url = self.orden.get_absolute_url()
        self.client.force_login(self.miguel)  # dirección la arma y la aprueba de una vez
        self.assertContains(self.client.post(url, self.datos(accion="aprobar"), follow=True), "los días de crédito")
        self.client.post(url, self.datos(accion="aprobar", dias_credito="30"))
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.dias_credito), ("lista", 30))
        self.client.force_login(self.bodega)
        self.client.post(reverse("inventario:orden_paso", args=[self.orden.pk, "proveedor"]))
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, "enviada")
        # La factura llega y cuadra: el pago vence 30 días después
        hace = timezone.localdate() - timedelta(days=2)
        self.client.force_login(self.financiera)
        self.client.post(reverse("inventario:orden_paso", args=[self.orden.pk, "factura"]),
                         {"numero": "FE-9", "fecha": f"{hace:%Y-%m-%d}", "valor": "119000"})
        self.orden.refresh_from_db()
        self.assertTrue(self.orden.factura_cuadra and self.orden.factura_validada_en)
        self.assertEqual(self.orden.vence, hace + timedelta(days=30))
        self.assertEqual(self.client.get(reverse("inventario:financiera")).context["credito"], [self.orden])

    def test_urgente_sin_direccion_le_avisa_y_sin_financiera_le_llega_a_direccion(self):
        from django.core import mail
        self.financiera.delete()
        self.client.force_login(self.bodega)
        url = self.orden.get_absolute_url()
        self.client.post(url, self.datos(accion="a_direccion", urgente="1", motivo_urgencia="corto"))
        self.orden.refresh_from_db()
        self.assertEqual(self.orden.estado, "borrador")  # el motivo es obligatorio
        mail.outbox.clear()
        self.client.post(url, self.datos(accion="a_direccion", urgente="1", motivo_urgencia="El proveedor solo lo separa hoy"))
        self.orden.refresh_from_db()
        self.assertEqual((self.orden.estado, self.orden.aprobada_por), ("por_pagar", None))
        asuntos = [m.subject for m in mail.outbox if "dir@janotec.test" in m.to]
        self.assertTrue(any("urgencia" in a for a in asuntos))
        self.assertTrue(any("por pagar" in a for a in asuntos))  # dirección cubre a financiera
        self.client.force_login(self.miguel)
        self.assertEqual(self.client.get(reverse("inventario:financiera")).status_code, 200)
        self.client.force_login(self.bodega)
        self.assertEqual(self.client.get(reverse("inventario:financiera")).status_code, 403)


class BandejaBodegaTests(BaseInventario):
    """Bodega gestiona por ticket, paso a paso, como técnica y comercial."""

    def test_cada_ticket_en_su_paso_y_lo_que_sigue(self):
        from django.utils import timezone

        from . import solicitudes as sol
        from .models import ActaEntrega, ItemSolicitud, SolicitudMaterial
        s = self.solicitud_aprobada()
        otro = Ticket.objects.create(empresa=self.ticket.empresa, solicitante=self.ticket.solicitante, categoria=self.ticket.categoria,
                                     asunto="Instalar 3 cámaras", descripcion="x", estado=Ticket.Estado.EN_INVENTARIO)
        v = SolicitudMaterial.objects.create(ticket=otro, origen="comercial", estado="validacion", cargo="cliente")
        ItemSolicitud.objects.create(solicitud=v, insumo=self.cable, descripcion="Cable UTP", cantidad=10)
        sin_sm = Ticket.objects.create(empresa=self.ticket.empresa, solicitante=self.ticket.solicitante,
                                       categoria=self.ticket.categoria, asunto="Recién aprobado", descripcion="x",
                                       estado=Ticket.Estado.EN_INVENTARIO)
        pasos = {f["ticket"].pk: f for f in sol.bandeja()}
        self.assertEqual(pasos[self.ticket.pk]["paso"], "alistar")
        self.assertEqual(pasos[otro.pk]["paso"], "recibido")
        self.assertEqual(pasos[otro.pk]["quien"], "Bodega")
        self.assertEqual(pasos[sin_sm.pk]["paso"], "recibido")
        # Entregada sin confirmar: espera al técnico; confirmada: sale de la bandeja
        s.estado = "entregada"
        s.save()
        acta = ActaEntrega.objects.create(solicitud=s, tecnico=self.agente, entregado_por=self.bodega, detalle=[])
        self.assertEqual({f["ticket"].pk: f for f in sol.bandeja()}[self.ticket.pk]["paso"], "confirmacion")
        acta.recibido_en = timezone.now()
        acta.save()
        self.assertNotIn(self.ticket.pk, {f["ticket"].pk for f in sol.bandeja()})
        self.assertIn(self.ticket.pk, {f["ticket"].pk for f in sol.bandeja("terminados")})

        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:bandeja"))
        self.assertContains(r, otro.radicado)
        self.assertContains(r, "Revisar la lista")
        self.assertNotContains(self.client.get(reverse("inventario:bandeja") + "?paso=compra"), otro.radicado)
        r = self.client.get(reverse("inventario:solicitud", args=[v.pk]))
        self.assertContains(r, "Paso a paso en bodega")
        self.assertContains(r, "Lo que sigue:")
        self.client.force_login(self.cli_a)
        self.assertEqual(self.client.get(reverse("inventario:bandeja")).status_code, 403)


class MenuYListadoDeComprasTests(BaseInventario):
    """El inventario se ordena en 5 grupos y las compras se listan por factura."""

    def test_menu_en_grupos_y_listado_de_facturas(self):
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:facturas"))
        for grupo in ("Mi trabajo", "Inventario", "Compras", "Movimientos", "Configuración"):
            self.assertContains(r, grupo)
        self.assertContains(r, "227065")  # la factura de la compra de cable del setUp
        self.assertContains(r, "Cable UTP exterior [305")
        self.assertContains(self.client.get(reverse("inventario:facturas") + "?q=no-existe"), "No hay compras con esos filtros")
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("inventario:facturas")).status_code, 403)


class EtiquetasQRTests(BaseInventario):
    """Etiquetas con QR para marcar los equipos con su placa."""

    def test_por_placas_y_por_factura(self):
        from .models import Activo
        self.client.force_login(self.bodega)
        url = reverse("inventario:etiquetas")
        r = self.client.get(url, {"placas": "CAM-BAL-2MP002, NO-EXISTE-1"})
        self.assertEqual([a.placa for a in r.context["activos"]], ["CAM-BAL-2MP002"])
        self.assertContains(r, "No existen estas placas: NO-EXISTE-1")
        pdf = self.client.get(url, {"placas": "CAM-BAL-2MP002", "imprimir": "1", "desde": "5", "copias": "2"})
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        Activo.objects.filter(placa="CAM-BAL-2MP002").update(factura="FE-77")
        self.assertEqual([a.placa for a in self.client.get(url, {"factura": "fe-77"}).context["activos"]], ["CAM-BAL-2MP002"])
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(url).status_code, 403)


class GarantiasProveedorTests(BaseInventario):
    """Un equipo va al proveedor por garantía y vuelve reparado, cambiado o como nota crédito; todo queda en su kardex."""

    def abrir(self, placa="CAM-BAL-2MP002", **kw):
        datos = {"placa": f"{placa} · Cámara", "motivo": "No enciende después de la tormenta", "fecha": "2026-10-01",
                 "proveedor": "SYSCOM COLOMBIA", "radicado": "RMA-123"}
        datos.update(kw)
        self.client.force_login(self.bodega)
        return self.client.post(reverse("inventario:garantias"), datos, follow=True)

    def test_enviar_y_volver_cambiado(self):
        from .models import Activo, GarantiaProveedor
        self.abrir()
        g = GarantiaProveedor.objects.get()
        a = Activo.objects.get(placa="CAM-BAL-2MP002")
        self.assertEqual((a.estado, a.lugar, g.proveedor.razon_social, g.radicado), ("garantia", "proveedor", "SYSCOM COLOMBIA", "RMA-123"))
        self.assertContains(self.abrir(), "ya está en garantía")  # no se abre dos veces
        self.assertContains(self.client.get(reverse("inventario:garantias")), g.numero)
        url = reverse("inventario:garantia", args=[g.pk])
        self.assertContains(self.client.post(url, {"estado": "reemplazado", "fecha": "2026-10-20"}, follow=True),
                            "Escriba el serial del equipo que llegó de reemplazo")
        serial_viejo = a.serial
        self.client.post(url, {"estado": "reemplazado", "fecha": "2026-10-20", "serial_nuevo": "NUEVO-777"})
        g.refresh_from_db()
        a.refresh_from_db()
        self.assertEqual((g.estado, g.serial_anterior, g.dias), ("reemplazado", serial_viejo, 19))
        self.assertEqual((a.estado, a.lugar, a.serial), ("disponible", "bodega", "NUEVO-777"))
        self.assertEqual(list(a.movimientos.order_by("pk").values_list("tipo", flat=True))[-2:], ["garantia", "garantia"])

    def test_nota_credito_da_de_baja(self):
        from .models import Activo, GarantiaProveedor
        self.abrir()
        g = GarantiaProveedor.objects.get()
        self.client.post(reverse("inventario:garantia", args=[g.pk]), {"estado": "nota_credito", "fecha": "2026-10-05",
                                                                       "valor_nota": "180000"})
        g.refresh_from_db()
        self.assertEqual((g.estado, g.valor_nota), ("nota_credito", 180000))
        self.assertEqual(Activo.objects.get(placa="CAM-BAL-2MP002").estado, "baja")
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("inventario:garantias")).status_code, 403)


class FueraDeServicioTests(BaseInventario):
    """Lo que ya no se usa sale de las listas (sin borrarse), como los «productos eliminados» de GESADMIN."""

    def test_dados_de_baja_e_insumos_inactivos_aparte(self):
        from .models import Activo
        Activo.objects.filter(placa="CAM-BAL-2MP002").update(estado="baja")
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:panel"))
        self.assertNotIn("CAM-BAL-2MP002", [a.placa for a in r.context["activos"]])
        self.assertContains(r, "ver dados de baja")
        r = self.client.get(reverse("inventario:panel"), {"ver": "activos", "estado": "baja"})
        self.assertIn("CAM-BAL-2MP002", [a.placa for a in r.context["activos"]])
        self.client.post(self.cable.get_absolute_url())
        self.cable.refresh_from_db()
        self.assertFalse(self.cable.activo)
        r = self.client.get(reverse("inventario:panel"), {"ver": "insumos"})
        self.assertNotIn(self.cable, [f["insumo"] for f in r.context["filas"]])
        r = self.client.get(reverse("inventario:panel"), {"ver": "insumos", "inactivos": "1"})
        self.assertIn(self.cable, [f["insumo"] for f in r.context["filas"]])


class FirmasAManoBodegaTests(BaseInventario):
    """Como el acta de soporte: se firma a mano la aprobación de salida y el acta de entrega (entrega y recibe)."""

    def test_aprobar_y_entregar_con_firmas(self):
        from .models import ActaEntrega
        s = self.solicitud_aprobada()
        s.estado = "aprobacion"
        s.save()
        self.client.force_login(self.bodega)
        r = self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "firmar"]), {"decision": "aprobar"}, follow=True)
        self.assertContains(r, "Firme en el recuadro para aprobar")
        self.assertFalse(s.firmas.exists())
        self.client.post(reverse("inventario:solicitud_accion", args=[s.pk, "firmar"]), {"decision": "aprobar", "firma": ActaBase.FIRMA})
        f = s.firmas.get()
        self.assertTrue(f.aprobada and f.imagen)
        self.assertEqual(self.client.get(reverse("inventario:firma", args=["aprobacion", f.pk]))["Content-Type"], "image/png")
        # Entrega sin la firma de quien entrega: no sale
        s.estado = "aprobada"
        s.save()
        r = self.entregar(solicitud=s, firma_entrega="")
        self.assertContains(r, "Falta la firma de quien entrega")
        self.assertFalse(ActaEntrega.objects.exists())
        # El técnico firma ahí mismo en bodega: el acta queda recibida, sin botón de confirmar después
        self.entregar(solicitud=s, firma_recibe=ActaBase.FIRMA)
        acta = ActaEntrega.objects.get()
        self.assertTrue(acta.firma_entrega and acta.firma_recibe)
        self.assertEqual(acta.recibido_por, self.agente)
        self.client.force_login(self.cli_b)
        self.assertEqual(self.client.get(reverse("inventario:firma", args=["recibe", acta.pk])).status_code, 404)


class ProductosTests(BaseInventario):
    """El inventario como catálogo: equipos por modelo con cada unidad (placa y serial) y materiales por cantidad."""

    def test_modelos_unidades_editar_y_eliminar(self):
        from .models import Activo
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:productos"))
        self.assertIn(("equipo", "DS2CD1027G2HLIU"), [(f["tipo"], f["clave"]) for f in r.context["filas"]])
        self.assertIn(("material", "CAB-UTP-EXT"), [(f["tipo"], f["clave"]) for f in r.context["filas"]])  # todo en un panel
        # Stock mínimo desde la fila: el modelo queda «por reponer»
        self.client.post(reverse("inventario:productos"), {"accion": "minimo", "tipo": "equipo", "clave": "DS2CD1027G2HLIU", "minimo": "50"})
        r = self.client.get(reverse("inventario:productos") + "?bajos=1")
        self.assertEqual([f["clave"] for f in r.context["filas"] if f["tipo"] == "equipo"], ["DS2CD1027G2HLIU"])
        from . import alertas
        self.assertEqual([m["clave"] for m in alertas.modelos_por_reponer()], ["DS2CD1027G2HLIU"])
        # Un modelo con sus unidades; lo vendido se oculta
        Activo.objects.filter(placa="CAM-BAL-2MP002").update(estado="vendido")
        r = self.client.get(reverse("inventario:modelo") + "?ref=DS2CD1027G2HLIU")
        self.assertNotIn("CAM-BAL-2MP002", [a.placa for a in r.context["unidades"]])
        r = self.client.get(reverse("inventario:modelo") + "?ref=DS2CD1027G2HLIU&todos=1")
        self.assertIn("CAM-BAL-2MP002", [a.placa for a in r.context["unidades"]])
        # Corregir la descripción del modelo: queda en todas sus unidades
        total = Activo.objects.filter(referencia="DS2CD1027G2HLIU").count()
        self.client.post(reverse("inventario:modelo"), {"ref": "DS2CD1027G2HLIU", "referencia": "DS2CD1027G2HLIU",
                                                        "marca": "Hikvision", "descripcion": "Cámara bala 2 MP", "categoria": "CCTV", "tipo": "Cámara"})
        self.assertEqual(Activo.objects.filter(referencia="DS2CD1027G2HLIU", descripcion="Cámara bala 2 MP").count(), total)
        # Editar una unidad: el serial no se repite
        otra = Activo.objects.create(placa="OTRA-001", referencia="Y-1", descripcion="Otra cámara", serial="SER-REPETIDO")
        a = Activo.objects.get(placa="CAM-BAL-2MP002")
        datos = {"serial": otra.serial, "referencia": a.referencia, "marca": a.marca, "descripcion": a.descripcion, "iva": "19"}
        r = self.client.post(reverse("inventario:activo_editar", args=[a.placa]), datos)
        self.assertContains(r, "Ya hay otro equipo con el serial")
        # Eliminar: solo lo registrado por error (en bodega y sin más movimientos que el ingreso)
        r = self.client.post(reverse("inventario:activo_eliminar", args=[a.placa]), follow=True)
        self.assertContains(r, "Solo se elimina un equipo que está en bodega")
        nuevo = Activo.objects.create(placa="ERR-001", referencia="X-1", descripcion="Registrado por error")
        self.client.post(reverse("inventario:activo_eliminar", args=["ERR-001"]))
        self.assertFalse(Activo.objects.filter(pk=nuevo.pk).exists())

    def test_materiales_editar_y_eliminar(self):
        from .models import Insumo
        self.client.force_login(self.bodega)
        r = self.client.get(reverse("inventario:productos") + "?tipo=materiales")
        self.assertIn(self.cable, [f["insumo"] for f in r.context["filas"]])
        self.client.post(reverse("inventario:insumo_nuevo"), {"codigo": "can 20x12", "descripcion": "Canaleta 20x12", "unidad": "Metro",
                                                               "stock_minimo": "10"})
        self.assertTrue(Insumo.objects.filter(codigo="CAN-20X12").exists())
        self.client.post(reverse("inventario:insumo_editar", args=[self.cable.codigo]),
                         {"descripcion": "Cable UTP cat 6 exterior", "unidad": self.cable.unidad, "marca": "", "stock_minimo": "0", "activo": "on"})
        self.cable.refresh_from_db()
        self.assertEqual(self.cable.descripcion, "Cable UTP cat 6 exterior")
        self.client.post(reverse("inventario:insumo_editar", args=[self.cable.codigo]), {"accion": "eliminar"})
        self.assertTrue(Insumo.objects.filter(pk=self.cable.pk).exists())  # tiene movimientos: no se borra
        vacio = Insumo.objects.create(codigo="ERR-INS", descripcion="Creado por error")
        self.client.post(reverse("inventario:productos"), {"accion": "eliminar", "tipo": "material", "clave": "ERR-INS"})
        self.assertFalse(Insumo.objects.filter(pk=vacio.pk).exists())
        self.client.force_login(self.supervisor)
        self.assertEqual(self.client.get(reverse("inventario:productos")).status_code, 403)


class IngresoSinFacturaTests(BaseInventario):
    """Lo que entra sin compra (inventario inicial, devolución, cortesía) no pide proveedor, factura ni valor."""

    def test_equipo_y_material_sin_factura(self):
        from .models import Activo, Insumo
        self.client.force_login(self.bodega)
        datos = {"tipo": "activo", "a-prefijo": "BIO-FAC-HUE", "a-cantidad_iguales": "1", "a-seriales": "GW1215486",
                 "a-categoria": "CCTV", "a-tipo": "Biometrico", "a-subtipo": "Facial", "a-marca": "Hikvision",
                 "a-referencia": "DS-K1T344MBFWX-E1", "a-descripcion": "Terminal de reconocimiento facial MinMoe", "a-iva": "19"}
        r = self.client.post(reverse("inventario:compra"), datos)
        self.assertContains(r, "marque «Ingreso sin factura»")  # una compra sí exige la factura
        self.assertFalse(Activo.objects.filter(serial="GW1215486").exists())
        self.client.post(reverse("inventario:compra"), {**datos, "a-sin_factura": "on", "a-motivo_sin_factura": "inventario_inicial",
                                                        "a-stock_minimo": "2"})
        a = Activo.objects.get(serial="GW1215486")
        from .models import MinimoModelo
        self.assertEqual(MinimoModelo.objects.get(referencia="DS-K1T344MBFWX-E1").minimo, 2)  # el mínimo se fija al ingresarlo
        self.assertEqual((a.placa, a.estado, a.factura), ("BIO-FAC-HUE001", "disponible", ""))
        self.assertIn("Ingreso sin factura", a.observaciones)
        self.assertEqual(a.movimientos.get().tipo, "ajuste")  # no cuenta como compra
        facturas = self.client.get(reverse("inventario:facturas")).context["facturas"]
        self.assertFalse(any("MinMoe" in p for f in facturas for p in f["productos"]))
        self.client.post(reverse("inventario:compra"), {"tipo": "insumo", "i-codigo": "CAN-20X12", "i-descripcion": "Canaleta 20x12",
                                                        "i-unidad": "Metro", "i-cantidad": "30", "i-iva": "19",
                                                        "i-sin_factura": "on", "i-motivo_sin_factura": "inventario_inicial"})
        self.assertEqual(servicios.disponible_en(Insumo.objects.get(codigo="CAN-20X12"), Lugar.BODEGA), 30)
        self.assertEqual(Insumo.objects.get(codigo="CAN-20X12").stock_minimo, 1)  # sin escribirlo: 1 por defecto
