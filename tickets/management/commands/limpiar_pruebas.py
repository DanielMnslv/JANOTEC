"""Deja en cero lo que fue prueba y conserva lo real: lo que hizo jguana y el área técnica (18/11/2026).

Pedido del usuario: inventario, catálogo de precios, propuestas, órdenes de compra y proveedores en cero; lo que hizo
jguana y el área técnica (tickets técnicos, actas, cronograma, horas extras, viáticos y lo de inventario ligado a
ellos) se queda. Antes de borrar se saca un respaldo completo para poder consultar cualquier dato después.

Uso:
    manage.py limpiar_pruebas              # SIMULACIÓN: dice qué se conserva y cuánto se borraría; no toca nada
    manage.py limpiar_pruebas --confirmar  # respaldo completo (respaldos/*.json.gz) y luego borra

Quién cuenta como «real» (``usuarios_reales``): jguana, los técnicos (rol Encargado), la coordinación técnica y la
central de atención, sin los usuarios prueba.*. Un ticket se conserva si es técnico (mantenimiento, soporte,
instalación), si lo radicó o solicitó alguien real, o si el área técnica ya trabajó en él (acta o visita en el
cronograma); nunca los de clientes PRUEBA. Lo que no se toca: usuarios, clientes y sedes, actas, cronograma, horas
extras, viáticos, contratos de monitoreo archivados, marcas y categorías de activos.

Los archivos subidos (PDF, fotos) no se borran del disco: quedan en media/ como parte del respaldo.
"""
import gzip
import json
from pathlib import Path

from django.conf import settings
from django.core import serializers
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from accounts.models import Usuario
from accounts.prueba import es_de_prueba

TIPOS_TECNICOS = ("mantenimiento", "soporte", "instalacion")
ROLES_REALES = ("agente", "supervisor", "central")


def usuarios_reales():
    """jguana, técnicos, coordinación técnica y central de atención (sin los usuarios de prueba)."""
    candidatos = Usuario.objects.filter(Q(username="jguana") | Q(rol__in=ROLES_REALES))
    return [u.pk for u in candidatos if not es_de_prueba(u)]


def plan():
    """[(nombre, queryset a borrar)] en el orden en que se puede borrar, y un resumen de lo que se conserva."""
    from cronograma.models import Actividad
    from comercial.models import (ConsecutivoPropuesta, Cotizacion, FichaComercial, InformeRequerimiento,
                                  ItemCatalogo, PropuestaVersion)
    from inventario.models import (Activo, AuditoriaHerramientas, AvisoSemanal, CompraHistorica, CotizacionProveedor,
                                   EntregaHerramienta, GarantiaProveedor, Herramienta, Insumo, MinimoModelo,
                                   MovimientoActivo, MovimientoInsumo, OrdenCompra, Proveedor, SolicitudCotizacion,
                                   SolicitudMaterial)
    from tickets.models import ActaSoporte, OportunidadComercial, Ticket

    reales = usuarios_reales()
    prueba = Q(empresa__razon_social__istartswith="PRUEBA")
    con_tecnica = set(ActaSoporte.objects.exclude(ticket=None).values_list("ticket_id", flat=True))
    con_tecnica |= set(Actividad.objects.exclude(ticket=None).values_list("ticket_id", flat=True))
    tickets_reales = set(
        Ticket.objects.exclude(prueba)
        .filter(Q(tipo__in=TIPOS_TECNICOS) | Q(radicado_por__in=reales) | Q(solicitante__in=reales) | Q(pk__in=con_tecnica))
        .values_list("pk", flat=True))
    actas_reales = set(ActaSoporte.objects.exclude(prueba).values_list("pk", flat=True))

    # Inventario: lo ligado a los tickets reales y pedido o registrado por alguien real
    solicitudes = set(SolicitudMaterial.objects.filter(ticket__in=tickets_reales, solicitado_por__in=reales)
                      .values_list("pk", flat=True))
    mov_real = Q(registrado_por__in=reales) | Q(solicitud__in=solicitudes) | Q(acta__in=actas_reales)
    mov_insumo = set(MovimientoInsumo.objects.filter(mov_real).values_list("pk", flat=True))
    mov_activo = set(MovimientoActivo.objects.filter(mov_real).values_list("pk", flat=True))
    insumos = set(MovimientoInsumo.objects.filter(pk__in=mov_insumo).values_list("insumo_id", flat=True))
    insumos |= set(SolicitudMaterial.objects.filter(pk__in=solicitudes).exclude(items__insumo=None)
                   .values_list("items__insumo", flat=True))
    activos = set(MovimientoActivo.objects.filter(pk__in=mov_activo).values_list("activo_id", flat=True))
    ordenes = set(OrdenCompra.objects.filter(creado_por__in=reales).values_list("pk", flat=True))
    cotizaciones_prov = set(SolicitudCotizacion.objects.filter(creado_por__in=reales).values_list("pk", flat=True))
    proveedores = set(OrdenCompra.objects.filter(pk__in=ordenes).exclude(proveedor=None).values_list("proveedor_id", flat=True))
    proveedores |= set(CotizacionProveedor.objects.filter(solicitud__in=cotizaciones_prov).values_list("proveedor_id", flat=True))

    pasos = [
        # Comercial y administrativa
        ("Propuestas (versiones)", PropuestaVersion.objects.exclude(enviada_por__in=reales)),
        ("Cotizaciones del cotizador", Cotizacion.objects.exclude(elaborada_por__in=reales)),
        ("Informes de requerimiento", InformeRequerimiento.objects.exclude(Q(elaborado_por__in=reales)
                                                                           & Q(ficha__ticket__in=tickets_reales))),
        ("Fichas comerciales sin trabajo técnico", FichaComercial.objects.exclude(ticket__in=tickets_reales)),
        ("Catálogo de precios", ItemCatalogo.objects.all()),
        ("Oportunidades comerciales", OportunidadComercial.objects.exclude(Q(detectada_por__in=reales) & ~prueba)),
        # Compras
        ("Órdenes de compra", OrdenCompra.objects.exclude(pk__in=ordenes)),
        ("Solicitudes de cotización a proveedores", SolicitudCotizacion.objects.exclude(pk__in=cotizaciones_prov)),
        ("Garantías con proveedores", GarantiaProveedor.objects.exclude(Q(creado_por__in=reales) & Q(activo__in=activos))),
        ("Historial de compras (Excel)", CompraHistorica.objects.all()),
        # Bodega
        ("Solicitudes de material", SolicitudMaterial.objects.exclude(pk__in=solicitudes)),
        ("Movimientos de insumos", MovimientoInsumo.objects.exclude(pk__in=mov_insumo)),
        ("Movimientos de equipos con placa", MovimientoActivo.objects.exclude(pk__in=mov_activo)),
        ("Productos (insumos)", Insumo.objects.exclude(pk__in=insumos)),
        ("Equipos con placa", Activo.objects.exclude(pk__in=activos)),
        ("Stock mínimo por modelo", MinimoModelo.objects.all()),
        ("Avisos semanales de bodega", AvisoSemanal.objects.all()),
        ("Auditorías de herramientas", AuditoriaHerramientas.objects.exclude(auditor__in=reales)),
        ("Entregas de herramientas", EntregaHerramienta.objects.all()),
        ("Herramientas", Herramienta.objects.all()),
        ("Proveedores", Proveedor.objects.exclude(pk__in=proveedores)),
        # Tickets que no son del área técnica (comerciales de prueba y los de clientes PRUEBA)
        ("Tickets que no son reales", Ticket.objects.exclude(pk__in=tickets_reales)),
        ("Actas de clientes PRUEBA", ActaSoporte.objects.filter(prueba)),
    ]
    if not PropuestaVersion.objects.filter(enviada_por__in=reales).exists():
        pasos.insert(1, ("Consecutivo de propuestas (PE)", ConsecutivoPropuesta.objects.all()))
    conserva = {
        "Usuarios reales (jguana, técnicos, coordinación, central)": len(reales),
        "Tickets": len(tickets_reales),
        "Actas": len(actas_reales),
        "Solicitudes de material": len(solicitudes),
        "Movimientos de inventario": len(mov_insumo) + len(mov_activo),
        "Productos (insumos)": len(insumos),
        "Equipos con placa": len(activos),
        "Órdenes de compra": len(ordenes),
        "Proveedores": len(proveedores),
    }
    return pasos, conserva


class Command(BaseCommand):
    help = "Deja en cero lo de prueba y conserva lo de jguana y el área técnica. Sin --confirmar solo simula."

    def add_arguments(self, parser):
        parser.add_argument("--confirmar", action="store_true", help="Saca el respaldo y BORRA. Sin esto solo simula.")
        parser.add_argument("--carpeta", default=str(Path(settings.BASE_DIR) / "respaldos"),
                            help="Dónde guardar el respaldo (por defecto respaldos/ junto a manage.py).")

    def handle(self, *args, confirmar=False, carpeta=None, **opciones):
        from django.core.management.base import CommandError
        from django.db.migrations.executor import MigrationExecutor

        # Con migraciones pendientes faltan tablas y el respaldo no estaría completo: primero migrate
        pendientes = MigrationExecutor(connection).migration_plan(
            MigrationExecutor(connection).loader.graph.leaf_nodes())
        if pendientes:
            nombres = ", ".join(f"{m.app_label}.{m.name}" for m, _ in pendientes)
            raise CommandError(f"Hay migraciones sin aplicar ({nombres}). Corra primero «manage.py migrate».")
        pasos, conserva = plan()
        self.stdout.write(self.style.MIGRATE_HEADING("SE CONSERVA"))
        for nombre, n in conserva.items():
            self.stdout.write(f"  {nombre:<58} {n:>6}")
        self.stdout.write(self.style.MIGRATE_HEADING("SE BORRA (con lo que depende de cada uno)"))
        for nombre, qs in pasos:
            self.stdout.write(f"  {nombre:<58} {qs.count():>6}")
        if not confirmar:
            self.stdout.write(self.style.WARNING(
                "\nSIMULACIÓN: no se borró nada. Revise los números y, si están bien, repita con --confirmar."))
            return

        archivo = self.respaldar(Path(carpeta))
        self.stdout.write(self.style.SUCCESS(f"\nRespaldo completo: {archivo}"))
        with transaction.atomic():
            for nombre, qs in pasos:
                total, _ = qs.delete()
                self.stdout.write(f"  Borrado: {nombre} ({total} registros en total)")
        self.reiniciar_consecutivos()
        self.stdout.write(self.style.SUCCESS("Listo. Lo de prueba quedó en cero."))

    def respaldar(self, carpeta):
        """Toda la base en JSON comprimido (se puede consultar o recuperar con loaddata)."""
        from django.apps import apps

        carpeta.mkdir(parents=True, exist_ok=True)
        archivo = carpeta / f"antes_de_limpiar_{timezone.localtime():%Y-%m-%d_%H%M}.json.gz"
        omitir = {"contenttypes", "sessions", "admin"}
        with gzip.open(archivo, "wt", encoding="utf-8") as salida:
            salida.write("[")
            primero = True
            for modelo in apps.get_models():
                if modelo._meta.app_label in omitir or modelo._meta.proxy:
                    continue
                for objeto in modelo._default_manager.order_by("pk").iterator():
                    texto = serializers.serialize("json", [objeto], use_natural_foreign_keys=False)
                    salida.write(("" if primero else ",") + texto[1:-1])
                    primero = False
            salida.write("]")
        json.load(gzip.open(archivo, "rt", encoding="utf-8"))  # que se pueda leer antes de borrar nada
        return archivo

    def reiniciar_consecutivos(self):
        """OC-00001, AE-00001…: las tablas que quedaron vacías vuelven a numerar desde 1 (MySQL)."""
        from inventario.models import ActaEntrega, OrdenCompra, SolicitudCotizacion, SolicitudMaterial

        if connection.vendor != "mysql" or connection.in_atomic_block:  # ALTER cierra la transacción (pruebas)
            return
        with connection.cursor() as cursor:
            for modelo in (OrdenCompra, ActaEntrega, SolicitudMaterial, SolicitudCotizacion):
                if not modelo.objects.exists():
                    cursor.execute(f"ALTER TABLE {connection.ops.quote_name(modelo._meta.db_table)} AUTO_INCREMENT = 1")
