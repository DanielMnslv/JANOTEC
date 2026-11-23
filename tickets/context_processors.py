"""Datos que reciben todas las plantillas (nombre y lema de la marca)."""

# Páginas del módulo administrativo (propuestas): para marcar activo su menú y no el de comercial
MODULO_ADMINISTRATIVO = ("propuestas", "cotizador", "catalogo")


def empresa(request):
    """Datos de marca disponibles en todas las plantillas; para dirección, cuántas propuestas esperan su aprobación, y
    para financiera (y dirección), cuántas órdenes de compra están por pagar; si el usuario abre en «Mi día» y, para
    dirección, cuántas cosas esperan su firma («Por aprobar»), y el menú lateral según su rol (tickets/menu.py)."""
    datos = {"MARCA": {"nombre": "JANOTEC", "lema": "Seguridad electrónica"}, "modulo_administrativo": MODULO_ADMINISTRATIVO}
    usuario = getattr(request, "user", None)
    if usuario is not None and usuario.is_authenticated and usuario.es_direccion:
        from comercial.models import Cotizacion
        datos["propuestas_por_aprobar"] = Cotizacion.objects.filter(en_revision=True).count()
    if usuario is not None and usuario.is_authenticated:
        from . import mi_dia
        datos["tiene_mi_dia"] = mi_dia.tiene_mi_dia(usuario)
        if usuario.es_direccion:
            datos["por_aprobar_total"] = mi_dia.total_por_aprobar(usuario)
    if usuario is not None and usuario.is_authenticated and usuario.ve_financiera:
        from inventario.models import OrdenCompra
        datos["compras_por_pagar"] = OrdenCompra.objects.filter(estado=OrdenCompra.Estado.POR_PAGAR).count()
    if usuario is not None and usuario.is_authenticated and usuario.es_interno:
        from django.urls import reverse

        from . import redaccion
        # «Revisar ortografía» y «Mejorar redacción» en todos los textos largos del personal (static/js/redaccion.js)
        datos["redaccion"] = redaccion.configuracion(reverse("mejorar_redaccion"), reverse("revisar_ortografia"))
        from .ensenar_redaccion import puede_ensenar
        if datos["redaccion"]["url"] and puede_ensenar(usuario):  # «No quedó como quería» → Ejemplos de redacción
            datos["redaccion"]["ensenar"] = reverse("redaccion_ejemplos")
    if usuario is not None and usuario.is_authenticated:
        from . import menu
        datos["menu"] = menu.construir(request, datos)  # menú lateral: lo de su rol desplegado, lo demás en «Más áreas»
    return datos
