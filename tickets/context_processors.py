"""Datos que reciben todas las plantillas (nombre y lema de la marca)."""

def empresa(request):
    """Datos de marca disponibles en todas las plantillas."""
    return {"MARCA": {"nombre": "JANOTEC", "lema": "Seguridad electrónica"}}
