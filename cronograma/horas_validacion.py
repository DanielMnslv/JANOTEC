"""Validación de las horas extras (13/11/2026; simplificada el 18/11/2026): certeza de que lo que indica el técnico es real.

Cada momento (inicio y fin) se registra de una de dos formas:

* **En el momento**: el técnico, esté donde esté, toma la foto (obligatoria); la plataforma pone la fecha y la hora
  del servidor y guarda la ubicación del celular. La coordinación la ve en el mapa con «Ver en el mapa».
  Ya no se compara con un punto fijo (oficina o cliente): el técnico puede iniciar o terminar en cualquier lugar.

* **Tardío**: lo registra después porque se le olvidó. Escribe la fecha y la hora reales y el **motivo**; queda la hora
  en que de verdad lo registró.

El reporte queda «Verificado» si los dos momentos se hicieron en el momento y con ubicación. Con cualquier alerta
(tardío, sin ubicación, registrado por otra persona, más de 16 horas) queda «Por validar» y la coordinación lo aprueba
o lo rechaza; lo rechazado no suma horas.
"""
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from .models import ReporteHorasExtra

V = ReporteHorasExtra.Validacion
MOMENTOS = {"salida": "inicio", "llegada": "fin"}


def leer_ubicacion(datos, prefijo=""):
    """(lat, lon, precisión) que envió el celular en los campos ocultos; None si no llegó o no es válida."""
    try:
        lat = Decimal(datos.get(f"{prefijo}lat") or "")
        lon = Decimal(datos.get(f"{prefijo}lon") or "")
    except InvalidOperation:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    try:
        precision = max(0, int(float(datos.get(f"{prefijo}precision") or 0)))
    except ValueError:
        precision = 0
    return lat.quantize(Decimal("0.0000001")), lon.quantize(Decimal("0.0000001")), precision


def registrar_ubicacion(reporte, momento, ubicacion):
    """Guarda la ubicación del celular en ese momento (no guarda el reporte)."""
    if ubicacion is None:
        return
    lat, lon, precision = ubicacion
    setattr(reporte, f"lat_{momento}", lat)
    setattr(reporte, f"lon_{momento}", lon)
    setattr(reporte, f"precision_{momento}", precision)


def mapa(lat, lon):
    """Enlace a Google Maps del punto."""
    return f"https://www.google.com/maps?q={lat},{lon}" if lat is not None and lon is not None else ""


def alertas(reporte):
    """Lo que la coordinación debe revisar antes de aprobar las horas (lista de textos; vacía si todo está bien)."""
    if reporte.validacion == V.ANTERIOR:
        return []
    lista = []
    for momento, nombre in MOMENTOS.items():
        if momento == "llegada" and reporte.abierto:
            continue
        if getattr(reporte, f"{momento}_tardia"):
            lista.append(f"{nombre.capitalize()} registrado tarde: {getattr(reporte, f'motivo_{momento}')}")
            continue
        if getattr(reporte, f"lat_{momento}") is None:
            lista.append(f"{nombre.capitalize()} sin ubicación del celular")
    if reporte.registrado_por_id and reporte.registrado_por_id != reporte.colaborador_id:
        lista.append(f"El inicio lo registró {reporte.registrado_por.get_full_name() or reporte.registrado_por.username}, "
                     "no el colaborador")
    if reporte.cerrado_por_id and reporte.cerrado_por_id != reporte.colaborador_id:
        lista.append(f"El fin lo registró {reporte.cerrado_por.get_full_name() or reporte.cerrado_por.username}, "
                     "no el colaborador")
    if not reporte.abierto and reporte.minutos > 16 * 60:
        lista.append(f"Más de 16 horas ({reporte.duracion} h)")
    return lista


def actualizar_estado(reporte):
    """Verificado si no hay alertas; por validar si las hay. No toca lo que ya decidió la coordinación."""
    if reporte.validacion in (V.APROBADO, V.RECHAZADO, V.ANTERIOR):
        return
    reporte.validacion = V.POR_VALIDAR if (reporte.abierto or alertas(reporte)) else V.VERIFICADO


def hora_valida_tardia(fecha, hora):
    """Un registro tardío debe ser del pasado y de los últimos 31 días; devuelve el error o ""."""
    from datetime import datetime

    ahora = timezone.localtime()
    momento = timezone.make_aware(datetime.combine(fecha, hora))
    if momento > ahora:
        return "La fecha y la hora no pueden ser posteriores a este momento."
    if momento < ahora - timedelta(days=31):
        return "Solo se pueden registrar horas de los últimos 31 días; para algo más antiguo hable con la coordinación."
    return ""
