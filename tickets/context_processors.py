"""Datos que reciben todas las plantillas (nombre y lema de la marca)."""

# Páginas del módulo administrativo (propuestas): para marcar activo su menú y no el de comercial
MODULO_ADMINISTRATIVO = ("propuestas", "cotizador", "catalogo")


def empresa(request):
    """Datos de marca disponibles en todas las plantillas y, para dirección, cuántas propuestas esperan su aprobación."""
    datos = {"MARCA": {"nombre": "JANOTEC", "lema": "Seguridad electrónica"}, "modulo_administrativo": MODULO_ADMINISTRATIVO}
    usuario = getattr(request, "user", None)
    if usuario is not None and usuario.is_authenticated and usuario.es_direccion:
        from comercial.models import Cotizacion
        datos["propuestas_por_aprobar"] = Cotizacion.objects.filter(en_revision=True).count()
    return datos
