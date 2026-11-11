"""Deja listos los casos de la guía de validación de Yuri y Tatiana (área administrativa e inventario).

Hace todo lo que en la prueba le toca a las otras áreas (comercial, dirección, financiera, coordinación y el cliente)
y deja cada caso detenido justo donde actúa la persona que valida. Así cada una hace sus pasos sin esperar a nadie.

Por cada persona (Yuri y Tatiana) crea, con el cliente PRUEBA:
    1. Armar la propuesta ............ requerimiento con informe, «Por armar» en Administrativo
    2. Propuesta devuelta ............ dirección la devolvió: «PRUEBA suba el margen a 25»
    3. Cambios del cliente ........... propuesta enviada; el cliente pidió quitar el botón de salida
    4. Cotizar con proveedores ....... informe con elementos que no están en el catálogo (en rojo)
    5. Validar y comprar lo que falta  propuesta aprobada: material por validar, faltan la terminal y el botón
    6. Entregar al técnico ........... material validado y con las firmas: listo para entregar
    Orden devuelta (proveedor «PRUEBA PROVEEDOR <NOMBRE>»): dirección pidió 4 cajas con anticipo del 50 %
    Orden pagada (mismo proveedor): aprobada y pagada por financiera, para enviar al proveedor y recibir

También completa lo que falte del entorno PRUEBA (cliente, sede, catálogo, productos con stock, equipos, proveedores
sin correo y un técnico ficticio que no puede iniciar sesión). No envía ningún correo mientras prepara.

Uso:
    python manage.py preparar_prueba_administrativa                      # prepara (falla si ya hay casos)
    python manage.py preparar_prueba_administrativa --rehacer            # borra los casos anteriores y los vuelve a crear
    python manage.py preparar_prueba_administrativa --eliminar JNT-2026-000028   # además borra tickets PRUEBA viejos
    python manage.py preparar_prueba_administrativa --ensayo             # hace todo y lo deshace (para revisar)
"""
import base64
from datetime import timedelta
from decimal import Decimal

from django.contrib.admin.models import ADDITION
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test.utils import override_settings
from django.utils import timezone

from accounts.models import Empresa, Sede, Usuario

PERSONAS = [("ychingate", "Yuri"), ("smedina", "Tatiana")]
MOTIVO = "Entorno de la guía de validación de administrativa e inventario (PRUEBA)"
FUENTE = "Guía de validación (prueba)"
# (descripción en el catálogo, tipo, unidad, costo sin IVA): el cotizador toma el valor del catálogo como costo y le suma el margen
CATALOGO = [
    ("PRUEBA Terminal reconocimiento facial Min Moe WiFi IP65 [PRUEBA-TERMINAL]", "material", "und", "597462"),
    ("PRUEBA Cable Cat6+ CM calibre 23 azul [PRUEBA-CABLE]", "material", "m", "1984.04"),
    ("PRUEBA Boton de salida sin contacto IR [PRUEBA-BOTON]", "material", "und", "24928"),
    ("PRUEBA Caja de sobreponer [PRUEBA-CAJA]", "material", "und", "8291.25"),
    ("PRUEBA Servicio de instalacion y configuracion de control de acceso", "servicio", "global", "175605"),
    ("PRUEBA Terminal CON stock [PRUEBA-TERMINAL-HAY]", "material", "und", "597462"),
    ("PRUEBA Cable con POCO stock [PRUEBA-CABLE-POCO]", "material", "m", "1984.04"),
]
# (código, descripción, unidad, mínimo que debe haber en bodega)
INSUMOS = [("PRUEBA-CABLE", "PRUEBA Cable Cat6+ CM calibre 23 azul", "Metro", 100),
           ("PRUEBA-CAJA", "PRUEBA Caja de sobreponer", "Unidad", 5),
           ("PRUEBA-BOTON", "PRUEBA Boton de salida sin contacto IR", "Unidad", 0),
           ("PRUEBA-CABLE-POCO", "PRUEBA Cable con POCO stock", "Metro", 25)]
EQUIPOS_HAY = 4  # unidades PRUEBA-HAY disponibles: una por persona para el caso 6 y una para el préstamo


# Imagen PNG de 1×1 para las firmas a mano que hace el comando (en la prueba real firman en el recuadro)
FIRMA_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=")


def _pdf(nombre):
    """Un PDF mínimo para los soportes de la prueba (pago)."""
    return SimpleUploadedFile(nombre, b"%PDF-1.4\n% Soporte ficticio de PRUEBA\n%%EOF", content_type="application/pdf")


class Command(BaseCommand):
    help = "Deja listos los casos de la guía de validación de Yuri y Tatiana con el cliente PRUEBA."

    def add_arguments(self, parser):
        parser.add_argument("--rehacer", action="store_true", help="Borra los casos anteriores de la guía y los crea de nuevo.")
        parser.add_argument("--eliminar", nargs="*", default=[], metavar="RADICADO",
                            help="Tickets viejos del cliente PRUEBA que se borran antes (solo PRUEBA).")
        parser.add_argument("--autor", default="", help="Usuario que hace de comercial, dirección y financiera "
                                                        "(por defecto, el primer superusuario activo).")
        parser.add_argument("--ensayo", action="store_true", help="Hace todo y lo deshace al final.")

    # --- Entrada ---------------------------------------------------------------------------------
    def handle(self, *args, **op):
        """Prepara todo en una sola transacción y sin enviar correos; con --ensayo la deshace al terminar."""
        self.autor = self._autor(op["autor"])
        self.personas = []
        for usuario, nombre in PERSONAS:
            u = Usuario.objects.filter(username=usuario, is_active=True).first()
            if u is None:
                raise CommandError(f"No existe el usuario activo {usuario} ({nombre}).")
            self.personas.append((u, nombre))
        with override_settings(EMAIL_BACKEND="django.core.mail.backends.dummy.EmailBackend"), transaction.atomic():
            self._eliminar_viejos(op["eliminar"])
            self._limpiar(op["rehacer"])
            self._entorno()
            resumen = []
            for persona, nombre in self.personas:
                resumen += self._casos(persona, nombre)
            for linea in resumen:
                self.stdout.write(linea)
            if op["ensayo"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Ensayo: todo se deshizo, no quedó nada creado."))
            else:
                self.stdout.write(self.style.SUCCESS("Casos listos para Yuri y Tatiana."))

    def _autor(self, username):
        """Quien hace los pasos de las otras áreas: el usuario indicado o el primer superusuario activo."""
        qs = Usuario.objects.filter(is_active=True, is_superuser=True)
        autor = qs.filter(username=username).first() if username else qs.order_by("pk").first()
        if autor is None:
            raise CommandError("No hay un superusuario activo para hacer de dirección, comercial y financiera.")
        return autor

    # --- Limpieza ---------------------------------------------------------------------------------
    def _eliminar_viejos(self, radicados):
        """Borra los tickets PRUEBA indicados (los de una prueba anterior que quedó a medias)."""
        from tickets import eliminar
        from tickets.models import Ticket

        for radicado in radicados:
            t = Ticket.objects.filter(radicado=radicado.strip().upper()).first()
            if t is None:
                self.stdout.write(f"{radicado}: no existe, nada que borrar.")
                continue
            if not eliminar.es_de_prueba(t):
                raise CommandError(f"{radicado} es de {t.empresa}, no del cliente PRUEBA: no se borra desde aquí.")
            eliminar.eliminar(t, self.autor, "Prueba anterior que quedó a medias; se rehace con la guía de validación.")
            self.stdout.write(f"{radicado}: borrado.")

    def _limpiar(self, rehacer):
        """Si ya hay casos de la guía: con --rehacer los borra (tickets y órdenes); si no, se detiene."""
        from inventario.models import OrdenCompra
        from tickets import eliminar
        from tickets.models import Ticket

        prefijos = [f"PRUEBA {nombre} · " for _u, nombre in PERSONAS]
        tickets = [t for t in Ticket.objects.filter(empresa__razon_social="PRUEBA") if t.asunto.startswith(tuple(prefijos))]
        ordenes = OrdenCompra.objects.filter(proveedor__razon_social__in=[f"PRUEBA PROVEEDOR {n.upper()}" for _u, n in PERSONAS])
        if not tickets and not ordenes.exists():
            return
        if not rehacer:
            raise CommandError(f"Ya hay {len(tickets)} casos y {ordenes.count()} órdenes de la guía. Use --rehacer para "
                               "borrarlos y crearlos de nuevo.")
        for t in tickets:
            eliminar.eliminar(t, self.autor, "Se rehacen los casos de la guía de validación.")
        ordenes.delete()
        self.stdout.write(f"Borrados {len(tickets)} casos y las órdenes anteriores de la guía.")

    # --- Entorno PRUEBA ----------------------------------------------------------------------------
    def _registro(self, objeto):
        """Deja constancia en Configuración → Historial de lo que se creó para la prueba."""
        from inventario.productos import _registro
        _registro(self.autor, objeto, ADDITION, MOTIVO)

    def _a_direccion(self, orden, persona):
        """La persona pasa la orden a dirección. Si la coordinación administrativa revisa las órdenes, la prueba la da
        por revisada (la revisión no es parte de lo que valida la persona)."""
        from inventario import financiera

        if financiera.requiere_revision(orden, persona):
            orden.revisada_por = Usuario.objects.filter(is_active=True, revisa_compras=True).first()
            orden.revisada_en = timezone.now()
            orden.save(update_fields=["revisada_por", "revisada_en"])
        financiera.enviar_a_direccion(orden, persona)

    def _entorno(self):
        """Completa lo que falte: cliente y sede, catálogo, productos con stock, equipos, proveedores y técnico."""
        from comercial.models import ItemCatalogo
        from inventario import servicios
        from inventario.models import Activo, AsesorProveedor, CodigoActivo, Insumo, Lugar, MovimientoActivo, Proveedor

        self.empresa = Empresa.objects.filter(razon_social="PRUEBA").order_by("pk").first()
        if self.empresa is None:
            self.empresa = Empresa.objects.create(razon_social="PRUEBA", ciudad="Villavicencio")
            self._registro(self.empresa)
        self.sede = self.empresa.sedes.filter(nombre="PRUEBA").first() or Sede.objects.create(
            empresa=self.empresa, nombre="PRUEBA", ciudad="Villavicencio", direccion="Dirección de prueba")
        self.cliente = Usuario.objects.filter(username="cliente.prueba", empresa=self.empresa).first()
        self.catalogo = {}
        for descripcion, tipo, unidad, valor in CATALOGO:
            item, nuevo = ItemCatalogo.objects.get_or_create(descripcion=descripcion, defaults=dict(
                tipo=tipo, unidad=unidad, valor_unitario=Decimal(valor), iva=19, fuente=FUENTE))
            if nuevo:
                self._registro(item)
            elif not item.activo or item.valor_unitario != Decimal(valor):
                # Ítem PRUEBA de una prueba anterior: activo y con el costo que dice la guía
                item.activo, item.valor_unitario = True, Decimal(valor)
                item.save(update_fields=["activo", "valor_unitario"])
            self.catalogo[descripcion] = item
        hoy = timezone.localdate()
        for codigo, descripcion, unidad, minimo in INSUMOS:
            insumo, nuevo = Insumo.objects.get_or_create(codigo=codigo, defaults=dict(
                descripcion=descripcion, unidad=unidad, stock_minimo=0, marca="PRUEBA"))
            if nuevo:
                self._registro(insumo)
            hay = servicios.disponible_en(insumo, Lugar.BODEGA)
            if hay < minimo:
                servicios.mover_insumo(insumo, minimo - hay, MovimientoActivo.Tipo.AJUSTE, Lugar.FUERA, Lugar.BODEGA, self.autor,
                                       fecha=hoy, observaciones="Saldo de PRUEBA para la guía de validación: no es material real")
            elif minimo == 0 and hay > 0:
                # El botón debe faltar para que el caso 5 tenga que comprarlo (una prueba anterior pudo dejar uno)
                servicios.mover_insumo(insumo, hay, MovimientoActivo.Tipo.AJUSTE, Lugar.BODEGA, Lugar.FUERA, self.autor,
                                       fecha=hoy, observaciones="Se deja en cero para la guía de validación (PRUEBA)")
        for codigo, descripcion in (("PRUEBA-HAY", "PRUEBA Terminal CON stock"), ("PRUEBA-TER", "PRUEBA Terminal reconocimiento facial")):
            CodigoActivo.objects.get_or_create(codigo=codigo, defaults=dict(descripcion=descripcion, marca="PRUEBA"))
        equipos = Activo.objects.filter(referencia="PRUEBA-TERMINAL-HAY")
        n = equipos.count()
        while equipos.filter(estado=Activo.Estado.DISPONIBLE).count() < EQUIPOS_HAY:
            n += 1
            while Activo.objects.filter(placa=f"PRUEBA-HAY{n:03d}").exists():
                n += 1
            a = Activo.objects.create(placa=f"PRUEBA-HAY{n:03d}", serial=f"PRUEBA-SERIAL-HAY-{n}", marca="PRUEBA",
                                      referencia="PRUEBA-TERMINAL-HAY", descripcion="PRUEBA Terminal CON stock",
                                      observaciones="Equipo ficticio de prueba")
            self._registro(a)
        nombres = ["PRUEBA PROVEEDOR A", "PRUEBA PROVEEDOR B"] + [f"PRUEBA PROVEEDOR {n.upper()}" for _u, n in PERSONAS]
        self.proveedores = {}
        for nombre in nombres:
            p, nuevo = Proveedor.objects.get_or_create(razon_social=nombre, defaults=dict(
                nit=nombre.replace("PRUEBA PROVEEDOR ", "PRUEBA-"), notas="Proveedor ficticio de prueba: no le ponga correo."))
            if nuevo:
                AsesorProveedor.objects.create(proveedor=p, nombre="Asesor de prueba", cargo="Prueba", celular="0000000")
                self._registro(p)
            # Sin correo: lo que se le «envíe» no sale a ninguna parte
            if p.correo:
                p.correo = ""
                p.save(update_fields=["correo"])
            p.asesores.exclude(correo="").update(correo="")
            self.proveedores[nombre] = p
        self.tecnico, nuevo = Usuario.objects.get_or_create(username="tecnico.prueba", defaults=dict(
            first_name="PRUEBA", last_name="Técnico ficticio", rol=Usuario.Rol.AGENTE, email="tecnico.prueba@janotec.test"))
        if nuevo:
            self.tecnico.set_unusable_password()  # nadie inicia sesión con él: solo recibe material de prueba
            self.tecnico.save()
            self._registro(self.tecnico)

    # --- Casos -------------------------------------------------------------------------------------
    def _requerimiento(self, nombre, numero, titulo, materiales, servicios="1 PRUEBA Servicio de instalacion y configuracion de control de acceso"):
        """Lo que hace comercial: radica el requerimiento PRUEBA y escribe el informe (pasa a «Elaborando propuesta»)."""
        from comercial import servicios as srv

        ficha = srv.crear_ticket_comercial(
            empresa=self.empresa, sede=self.sede, solicitante=self.cliente or self.autor, autor=self.autor,
            asunto=f"PRUEBA {nombre} · {numero}. {titulo}", descripcion=f"Caso {numero} de la guía de validación de {nombre}.",
            impacto="", contacto_nombre="Encargado de prueba", contacto_telefono="0000000",
            contacto_email="cliente.prueba@janotec.test", no_disponible={}, restricciones="", origen="comercial",
            ficha_datos={"comercial": None, "servicios": [], "otras_sedes": False, "fecha_max_propuesta": None})
        srv.registrar_informe(ficha, self.autor, {"necesidad": f"PRUEBA control de acceso ({titulo.lower()})",
                                                  "solucion": "Terminal facial con botón de salida", "servicios": servicios,
                                                  "materiales": materiales, "observaciones": "Caso de prueba"})
        ficha.refresh_from_db()
        return ficha

    def _cotizacion(self, ficha, lineas, margen=25):
        """Lo que haría administrativa en el cotizador: modalidad venta, el margen y una línea por ítem del catálogo."""
        from comercial import cotizador
        from comercial.models import LineaCotizacion

        cot = cotizador.borrador(ficha)
        cot.margen, cot.modalidad = Decimal(margen), "venta"
        cot.save()
        for i, (descripcion, cantidad) in enumerate(lineas):
            item = self.catalogo[descripcion]
            LineaCotizacion.objects.create(cotizacion=cot, item=item, descripcion=descripcion, cantidad=Decimal(cantidad),
                                           valor_unitario=item.valor_unitario, iva=19, orden=i)
        return cot

    def _aprobada(self, ficha, lineas):
        """Dirección aprueba la propuesta y el cliente la aprueba: el material queda en bodega por validar."""
        from comercial import cotizador
        from comercial import servicios as srv

        cot = self._cotizacion(ficha, lineas)
        cotizador.enviar(cot, self.autor)
        ficha.refresh_from_db()
        srv.registrar_respuesta(ficha, self.cliente or self.autor, True)
        return ficha.ticket.solicitudes_material.get()

    def _casos(self, persona, nombre):
        """Los seis requerimientos y las dos órdenes de compra de una persona (las órdenes las «creó» ella). Devuelve las líneas del resumen."""
        from comercial import cotizador
        from comercial import servicios as srv
        from inventario import financiera
        from inventario import solicitudes as sol
        from inventario.models import LineaOrdenCompra, OrdenCompra

        C = {k.split(" [")[0].split(" de control")[0]: k for k, *_x in CATALOGO}
        TERMINAL, CABLE, BOTON, CAJA = (C["PRUEBA Terminal reconocimiento facial Min Moe WiFi IP65"], C["PRUEBA Cable Cat6+ CM calibre 23 azul"],
                                        C["PRUEBA Boton de salida sin contacto IR"], C["PRUEBA Caja de sobreponer"])
        SERVICIO, HAY = C["PRUEBA Servicio de instalacion y configuracion"], C["PRUEBA Terminal CON stock"]
        base = [(TERMINAL, "1"), (CABLE, "40"), (BOTON, "1"), (CAJA, "1"), (SERVICIO, "1")]
        texto_base = "1 PRUEBA Terminal reconocimiento facial\n40 m PRUEBA Cable Cat6+\n1 PRUEBA Botón de salida\n1 PRUEBA Caja de sobreponer"
        r = []

        # 1. Por armar
        f = self._requerimiento(nombre, 1, "Armar la propuesta", texto_base)
        r.append(f"{nombre} · 1. Armar la propuesta ........ {f.ticket.radicado} (Por armar)")
        # 2. Devuelta por dirección
        f = self._requerimiento(nombre, 2, "Propuesta devuelta por dirección", texto_base)
        cot = self._cotizacion(f, base, margen=20)
        cotizador.enviar_a_direccion(cot, self.autor)
        cotizador.devolver(cot, self.autor, "PRUEBA suba el margen a 25 y vuelva a enviarla")
        r.append(f"{nombre} · 2. Propuesta devuelta ........ {f.ticket.radicado} (Devueltas por dirección)")
        # 3. Cambios del cliente
        f = self._requerimiento(nombre, 3, "Cambios que pidió el cliente", texto_base)
        cotizador.enviar(self._cotizacion(f, base), self.autor)
        f.refresh_from_db()
        srv.solicitar_cambios(f, self.cliente or self.autor, "PRUEBA quitar el botón de salida")
        r.append(f"{nombre} · 3. Cambios del cliente ....... {f.ticket.radicado} (El cliente pidió cambios)")
        # 4. Cotizar con proveedores para armar la propuesta (elementos que no están en el catálogo)
        f = self._requerimiento(nombre, 4, "Cotizar con proveedores", f"2 PRUEBA Sensor magnético de puerta {nombre}\n"
                                                                     f"1 PRUEBA Sirena de 30 W {nombre}")
        r.append(f"{nombre} · 4. Cotizar con proveedores ... {f.ticket.radicado} (Tickets para propuesta)")
        # 5. Material por validar, falta la terminal y el botón
        f = self._requerimiento(nombre, 5, "Validar y comprar lo que falta", texto_base)
        s = self._aprobada(f, [(TERMINAL, "1"), (CABLE, "10"), (BOTON, "1"), (SERVICIO, "1")])
        r.append(f"{nombre} · 5. Validar y comprar ......... {f.ticket.radicado} · {s.numero} (por validar)")
        # 6. Material validado y firmado, listo para entregar
        f = self._requerimiento(nombre, 6, "Entregar al técnico", texto_base)
        s = self._aprobada(f, [(HAY, "1"), (CABLE, "20"), (CAJA, "1"), (SERVICIO, "1")])
        sol.validar(s, self.autor, cargo="cliente", modalidad="venta", tecnico=self.tecnico)
        s.refresh_from_db()
        while sol.roles_que_puede_firmar(s, self.autor):
            sol.firmar(s, self.autor, True, rol=sol.roles_que_puede_firmar(s, self.autor)[0], comentario="PRUEBA",
                      firma=ContentFile(FIRMA_PNG, name="firma.png"))
            s.refresh_from_db()
        if s.estado != "aprobada":
            raise CommandError(f"El caso 6 de {nombre} quedó en «{s.get_estado_display()}» y no en aprobada.")
        r.append(f"{nombre} · 6. Entregar al técnico ...... {f.ticket.radicado} · {s.numero} (aprobada, por entregar)")

        # Órdenes de compra (sin ticket) con el proveedor de la persona
        proveedor = self.proveedores[f"PRUEBA PROVEEDOR {nombre.upper()}"]
        llega = timezone.localdate() + timedelta(days=3)
        devuelta = OrdenCompra.objects.create(proveedor=proveedor, creado_por=persona, fecha_estimada=llega,
                                              notas=f"PRUEBA {nombre} · orden devuelta por dirección")
        LineaOrdenCompra.objects.create(orden=devuelta, descripcion=CAJA, cantidad=2, valor_unitario=Decimal("8291.25"))
        self._a_direccion(devuelta, persona)  # la persona la pasó a dirección
        financiera.devolver(devuelta, self.autor, "PRUEBA pida 4 cajas en lugar de 2 y cámbiela a anticipo del 50 %")
        pagada = OrdenCompra.objects.create(proveedor=proveedor, creado_por=persona, fecha_estimada=llega,
                                            notas=f"PRUEBA {nombre} · orden aprobada y pagada")
        LineaOrdenCompra.objects.create(orden=pagada, descripcion=C["PRUEBA Cable con POCO stock"], cantidad=10,
                                        valor_unitario=Decimal("1984.04"))
        self._a_direccion(pagada, persona)
        financiera.aprobar(pagada, self.autor)
        pagada.refresh_from_db()
        financiera.registrar_pago(pagada, self.autor, timezone.localdate(), pagada.totales()["total"].quantize(Decimal(1)),
                                  "Transferencia", _pdf(f"pago-prueba-{nombre.lower()}.pdf"))
        devuelta.refresh_from_db()
        pagada.refresh_from_db()
        r.append(f"{nombre} · Orden devuelta .............. {devuelta.numero} ({devuelta.get_estado_display()}) · {proveedor}")
        r.append(f"{nombre} · Orden pagada ................ {pagada.numero} ({pagada.get_estado_display()}) · {proveedor}")
        return r
