"""Archivos que se suben para revisar antes de guardar (Excel de compras, factura del proveedor).

Entre la revisión y la confirmación quedan en la carpeta temporal del servidor, no en media (que es pública), con un
nombre al azar. Se borran al confirmar; los que nadie confirmó se borran solos al día siguiente.
"""
import re
import tempfile
import time
import uuid
from pathlib import Path

CARPETA = "janotec_importaciones"


def ruta(token, prefijo, extension):
    """Ruta del archivo de ese token, o None si el token no es válido (evita que manden rutas)."""
    if not re.fullmatch(r"[0-9a-f]{32}", token or "") or not re.fullmatch(r"[a-z]{1,10}", extension or ""):
        return None
    carpeta = Path(tempfile.gettempdir()) / CARPETA
    carpeta.mkdir(exist_ok=True)
    return carpeta / f"{prefijo}_{token}.{extension}"


def limpiar(horas=24):
    """Borra lo que se subió para revisar y nunca se confirmó."""
    limite = time.time() - horas * 3600
    carpeta = Path(tempfile.gettempdir()) / CARPETA
    for viejo in carpeta.glob("*_*.*") if carpeta.exists() else []:
        try:
            if viejo.stat().st_mtime < limite:
                viejo.unlink()
        except OSError:
            pass


def guardar(archivo, prefijo, extension):
    """Guarda el archivo subido y devuelve (token, ruta)."""
    limpiar()
    token = uuid.uuid4().hex
    destino = ruta(token, prefijo, extension)
    with open(destino, "wb") as salida:
        for trozo in archivo.chunks():
            salida.write(trozo)
    return token, destino
