"""Validación de las horas extras (pedida el 13/11/2026): certeza de que lo que indica el técnico es real.

Cada momento (inicio y regreso) se registra de una de dos formas:

* **En el momento**: el técnico no escribe la hora; la plataforma toma la fecha y la hora del servidor, la ubicación
  del celular y la foto (obligatoria). La ubicación se confronta con el punto donde debería estar:

  - el conductor al iniciar y todos al regresar: la **oficina de JANOTEC** (entrega de llaves y regreso);
  - el acompañante al iniciar: el **cliente** de su visita del día en el cronograma (la ubicación del ticket o la
    de la última acta de esa sede).

* **Tardío**: lo registra después porque se le olvidó. Escribe la fecha y la hora reales y el **motivo**; queda la hora
  en que de verdad lo registró.

El reporte queda «Verificado» solo si los dos momentos se hicieron en el momento, con ubicación y cerca del punto.
Con cualquier alerta (tardío, sin ubicación, lejos, registrado por otra persona, más de 16 horas) queda «Por validar»
y la coordinación lo aprueba o lo rechaza; lo rechazado no suma horas.
"""
import math
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.utils import timezone

from .models import ReporteHorasExtra, UbicacionOficina

V = ReporteHorasExtra.Validacion
MOMENTOS = {"salida": "inicio", "llegada": "regreso"}


def radio_metros():
    """Hasta cuántos metros del punto se considera «en el sitio» (más la imprecisión del GPS)."""
    return int(getattr(settings, "HORAS_EXTRA_RADIO_METROS", 300))


def distancia(lat1, lon1, lat2, lon2):
    """Metros entre dos puntos (fórmula del haversine)."""
    r = 6371000
    f1, f2 = math.radians(float(lat1)), math.radians(float(lat2))
    df, dl = f2 - f1, math.radians(float(lon2) - float(lon1))
    a = math.sin(df / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)))


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


def oficina():
    """La ubicación de la oficina (None si la coordinación aún no la ha fijado)."""
    return UbicacionOficina.objects.order_by("pk").first()


def punto_del_cliente(reporte):
    """(nombre, lat, lon) del cliente que visita el colaborador ese día según el cronograma; None si no se sabe."""
    from tickets.models import ActaSoporte

    from .models import Actividad

    actividades = (Actividad.objects.filter(fecha=reporte.fecha, encargados=reporte.colaborador)
                   .select_related("ticket__sede", "ticket__empresa").order_by("hora_inicio"))
    for a in actividades:
        t = a.ticket
        if t is None:
            continue
        nombre = f"{t.empresa}" + (f" · {t.sede.nombre}" if t.sede else "")
        if t.latitud is not None and t.longitud is not None:
            return nombre, t.latitud, t.longitud
        acta = (ActaSoporte.objects.filter(empresa=t.empresa, sede=t.sede, latitud__isnull=False, longitud__isnull=False)
                .order_by("-fecha").first()) if t.sede_id else None
        if acta:
            return nombre, acta.latitud, acta.longitud
    return None


def punto_de_referencia(reporte, momento):
    """(nombre, lat, lon) contra el que se confronta ese momento; None si no hay con qué comparar."""
    if momento == "salida" and reporte.rol == ReporteHorasExtra.Rol.ACOMPANANTE:
        return punto_del_cliente(reporte)
    o = oficina()
    return ("Oficina JANOTEC", o.latitud, o.longitud) if o else None


def registrar_ubicacion(reporte, momento, ubicacion):
    """Guarda la ubicación del momento y la distancia al punto de referencia (no guarda el reporte)."""
    if ubicacion is None:
        return
    lat, lon, precision = ubicacion
    setattr(reporte, f"lat_{momento}", lat)
    setattr(reporte, f"lon_{momento}", lon)
    setattr(reporte, f"precision_{momento}", precision)
    punto = punto_de_referencia(reporte, momento)
    if punto:
        nombre, plat, plon = punto
        setattr(reporte, f"referencia_{momento}", nombre[:200])
        setattr(reporte, f"distancia_{momento}", distancia(lat, lon, plat, plon))


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
            continue
        dist = getattr(reporte, f"distancia_{momento}")
        tolerancia = radio_metros() + (getattr(reporte, f"precision_{momento}") or 0)
        if dist is not None and dist > tolerancia:
            km = f"{dist / 1000:.1f} km".replace(".", ",") if dist >= 1000 else f"{dist} m"
            lista.append(f"{nombre.capitalize()} a {km} de {getattr(reporte, f'referencia_{momento}')}")
    if reporte.registrado_por_id and reporte.registrado_por_id != reporte.colaborador_id:
        lista.append(f"El inicio lo registró {reporte.registrado_por.get_full_name() or reporte.registrado_por.username}, "
                     "no el colaborador")
    if reporte.cerrado_por_id and reporte.cerrado_por_id != reporte.colaborador_id:
        lista.append(f"El regreso lo registró {reporte.cerrado_por.get_full_name() or reporte.cerrado_por.username}, "
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
