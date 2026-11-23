"""Ayuda de redacción para los textos que se escriben en la plataforma: el acta de soporte (con guía y frases) y,
desde el 18/11/2026, cualquier campo de texto largo del personal de JANOTEC (tipo «general»).

Tres niveles:

1. Ayuda fija, sin costo: a cada campo le corresponde una guía corta, frases rápidas para tocar y un mínimo de
   palabras para avisar cuando el texto queda muy pobre; además el corrector en español del navegador o del
   celular subraya las palabras mal escritas. Lo usa static/js/redaccion.js.
2. Botón «Revisar ortografía», gratuito: LanguageTool señala tildes, ortografía, gramática y puntuación, y el técnico
   aplica cada corrección con un toque (o todas a la vez).
3. Botón «Mejorar redacción» (opcional, con costo): le pide a Claude (Anthropic) que reescriba el texto del técnico en español formal
   y claro, sin agregar hechos. Solo se activa si el servidor tiene REDACCION_API_KEY en el .env y la librería
   «anthropic» instalada; si falta cualquiera de las dos, el botón no aparece y lo demás sigue funcionando.
   La clave tiene un nombre propio (no ANTHROPIC_API_KEY) para que ningún otro programa del servidor la tome por su
   cuenta: el saldo se usa única y exclusivamente para redactar y mejorar textos de la plataforma (18/11/2026).

Al servicio externo solo viaja el párrafo del campo: ni el cliente, ni la sede, ni el ticket.
"""

import logging
import os

log = logging.getLogger(__name__)

MODELO = "claude-haiku-5-5"  # 23/11/2026: Haiku 5.5, unas 40 veces más barato que Opus; alcanza de sobra para redactar
LARGO_MAXIMO = 4000  # caracteres que se aceptan para mejorar (un campo de la plataforma no debería pasar de esto)
CLAVE = "REDACCION_API_KEY"  # variable del .env, solo para este botón
MEJORAS_POR_DIA = 100  # por persona: cuida el saldo prepagado (con Haiku cada mejora cuesta menos de 0,1 centavos de dólar)

# Guía, frases rápidas y mínimo de palabras por tipo de campo
TIPOS = {
    "diagnostico": {
        "contexto": "el diagnóstico de la visita: qué se encontró, en qué equipo y la causa probable de la falla",
        "guia": "Qué encontró, en qué equipo y cuál es la causa probable.",
        "frases": ["Se encontró ", "El equipo no presentaba ", "La falla se debe a ", "Se verificó ",
                   "Sin novedad en "],
        "minimo": 6,
    },
    "resultado": {
        "contexto": "las observaciones del resultado del soporte: qué se hizo, cómo quedó el sistema y qué queda pendiente",
        "guia": "Qué se hizo, cómo quedó el sistema y si queda algo pendiente.",
        "frases": ["Se realizó ", "El sistema quedó operativo. ", "Queda pendiente ", "Se recomienda ",
                   "Se dejó en funcionamiento "],
        "minimo": 6,
    },
    "equipo": {
        "contexto": "las observaciones de un equipo del acta: qué se le hizo, para qué se usó o en qué estado quedó",
        "guia": "Qué se le hizo al equipo y cómo quedó.",
        "frases": ["Se realizó revisión general. ", "Se ajustó ", "Se reemplazó ", "Se configuró ",
                   "Quedó funcionando correctamente. "],
        "minimo": 3,
    },
    "oportunidad": {
        "contexto": "una oportunidad comercial que el técnico vio en la visita: qué necesita el cliente y dónde",
        "guia": "Qué necesita el cliente y en qué lugar de la sede.",
        "frases": ["El cliente solicita cotizar ", "Se observó que ", "Se recomienda instalar "],
        "minimo": 4,
    },
    # Cualquier otro campo de texto largo: sin guía ni frases; el contexto lo arman el nombre del campo y la página
    "general": {"contexto": "", "guia": "", "frases": [], "minimo": 0},
}

INSTRUCCIONES = (
    "Eres el corrector de estilo de los textos de JANOTEC, empresa colombiana de seguridad electrónica "
    "(CCTV, alarmas, control de acceso, redes y cableado). Una persona del equipo (técnico, comercial, administrativa, "
    "bodega o coordinación) escribió el texto en la plataforma, a veces desde el celular y con prisa. Reescríbelo para "
    "que quede bien presentado: puede terminar en un acta, un informe o una propuesta para el cliente.\n\n"
    "Reglas:\n"
    "- Español de Colombia, formal y claro. En actas, informes y diagnósticos usa forma impersonal («Se realizó…», "
    "«Se encontró…»); en un mensaje, motivo o comentario conserva a quién se dirige y su propósito.\n"
    "- Corrige ortografía, tildes, puntuación y mayúsculas; ordena las ideas en oraciones completas.\n"
    "- No agregues hechos, causas, cifras, equipos, recomendaciones ni conclusiones que no estén en el texto.\n"
    "- Conserva tal cual marcas, referencias, modelos, seriales, cantidades, medidas, zonas y nombres.\n"
    "- Si el texto ya es breve y claro, solo corrígelo: no lo alargues.\n"
    "- Devuelve únicamente el texto final, sin comillas, títulos, viñetas ni comentarios."
)


def ia_disponible():
    """True si se puede usar el botón «Mejorar redacción»: hay clave en el .env y la librería está instalada."""
    if not os.environ.get(CLAVE):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def configuracion(url_mejorar="", url_ortografia=""):
    """Lo que necesita static/js/redaccion.js: guías, frases, mínimos, la dirección del botón «Revisar ortografía»
    (gratuito, siempre disponible) y, si hay clave, la del botón de IA."""
    return {
        "tipos": {clave: {k: t[k] for k in ("guia", "frases", "minimo")} for clave, t in TIPOS.items()},
        "url": url_mejorar if ia_disponible() else "",
        "ortografia": url_ortografia,
    }


class RedaccionNoDisponible(Exception):
    """El texto no se pudo mejorar (sin clave, sin conexión, servicio ocupado…): el técnico deja el suyo."""


# --- Revisión de ortografía gratuita (LanguageTool) ----------------------------------
LANGUAGETOOL_URL = "https://api.languagetool.org/v2/check"  # servicio público: gratis, sin clave, ~20 revisiones/min


def revisar_ortografia(texto):
    """Errores de ortografía, tildes, gramática y puntuación del texto, según LanguageTool (gratuito).

    Devuelve una lista de {inicio, largo, error, mensaje, sugerencias (hasta 3)}; lista vacía si no hay errores.
    Solo viaja el párrafo del campo. Lanza RedaccionNoDisponible si el servicio no responde.
    """
    import json
    import urllib.error
    import urllib.parse
    import urllib.request

    texto = texto or ""
    if not texto.strip():
        raise RedaccionNoDisponible("Escriba primero el texto que quiere revisar.")
    if len(texto) > LARGO_MAXIMO:
        raise RedaccionNoDisponible(f"El texto es muy largo para revisarlo (máximo {LARGO_MAXIMO} caracteres).")
    datos = urllib.parse.urlencode({"text": texto, "language": "es"}).encode()
    pedido = urllib.request.Request(LANGUAGETOOL_URL, data=datos, headers={"User-Agent": "JANOTEC-soporte/1.0"})
    try:
        with urllib.request.urlopen(pedido, timeout=20) as r:
            respuesta = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RedaccionNoDisponible("El revisor gratuito está ocupado. Intente en un minuto.")
        log.warning("Ortografía: LanguageTool respondió %s", e.code)
        raise RedaccionNoDisponible("El revisor de ortografía no respondió bien. Intente de nuevo.")
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise RedaccionNoDisponible("No hay conexión con el revisor de ortografía. Revise el internet e intente de nuevo.")
    errores = []
    for m in respuesta.get("matches", []):
        inicio, largo = m.get("offset", 0), m.get("length", 0)
        errores.append({
            "inicio": inicio, "largo": largo, "error": texto[inicio:inicio + largo],
            "mensaje": m.get("shortMessage") or m.get("message", ""),
            "sugerencias": [s["value"] for s in m.get("replacements", [])[:3]],
        })
    return errores


def _limpio(valor, largo=80):
    """Nombre de campo o página que manda el navegador: una línea corta, sin signos que se confundan con etiquetas."""
    return " ".join(str(valor or "").replace("<", " ").replace(">", " ").split())[:largo]


def descripcion(tipo, campo="", pagina=""):
    """A qué corresponde el texto, para que Claude sepa el tono: el del tipo del acta o, en general, campo y página."""
    conocido = TIPOS.get(tipo, {}).get("contexto")
    if conocido:
        return conocido
    campo, pagina = _limpio(campo), _limpio(pagina)
    if campo and pagina:
        return f"el campo «{campo}» de la página «{pagina}» de la plataforma"
    if campo or pagina:
        return f"el campo «{campo or pagina}» de la plataforma"
    return "un texto escrito en la plataforma"


def gastar_cupo(usuario):
    """Cuenta una mejora del día para la persona; False si ya llegó a MEJORAS_POR_DIA (cuida el saldo prepagado)."""
    from django.core.cache import cache
    from django.utils import timezone

    clave = f"redaccion:{usuario.pk}:{timezone.localdate():%Y%m%d}"
    usadas = cache.get_or_set(clave, 0, 60 * 60 * 26)
    if usadas >= MEJORAS_POR_DIA:
        return False
    try:
        cache.incr(clave)
    except ValueError:  # se venció justo ahora
        cache.set(clave, 1, 60 * 60 * 26)
    return True


def mejorar(texto, tipo, campo="", pagina=""):
    """Devuelve el texto reescrito por Claude, o lanza RedaccionNoDisponible con un mensaje para mostrar."""
    texto = (texto or "").strip()
    if not texto:
        raise RedaccionNoDisponible("Escriba primero el texto que quiere mejorar.")
    if len(texto) > LARGO_MAXIMO:
        raise RedaccionNoDisponible(f"El texto es muy largo para mejorarlo (máximo {LARGO_MAXIMO} caracteres).")
    if not ia_disponible():
        raise RedaccionNoDisponible("La mejora de redacción no está activada en el servidor.")
    import anthropic

    contexto = descripcion(tipo, campo, pagina)
    cliente = anthropic.Anthropic(api_key=os.environ[CLAVE], timeout=60.0, max_retries=1)
    try:
        respuesta = cliente.messages.create(
            model=MODELO,
            max_tokens=8000,
            output_config={"effort": "medium"},  # equilibrio entre buena ortografía y costo
            system=INSTRUCCIONES,
            messages=[{"role": "user", "content": f"El texto corresponde a {contexto}.\n\n<texto>\n{texto}\n</texto>"}],
        )
    except anthropic.AuthenticationError:
        log.error("Redacción: la clave REDACCION_API_KEY no es válida")
        raise RedaccionNoDisponible("La clave del servicio de redacción no es válida. Avise a sistemas.")
    except anthropic.BadRequestError as e:
        if "credit" in str(e).lower():  # se acabó el saldo prepagado
            log.error("Redacción: sin saldo en la cuenta de Anthropic")
            raise RedaccionNoDisponible("Se acabó el saldo de la mejora de redacción. Avise a sistemas para recargarlo.")
        log.error("Redacción: solicitud rechazada (%s)", e.status_code)
        raise RedaccionNoDisponible("El servicio de redacción no respondió bien. Intente de nuevo.")
    except anthropic.RateLimitError:
        raise RedaccionNoDisponible("El servicio de redacción está ocupado. Intente en un minuto.")
    except anthropic.APIStatusError as e:
        log.error("Redacción: error %s del servicio", e.status_code)
        raise RedaccionNoDisponible("El servicio de redacción no respondió bien. Intente de nuevo.")
    except anthropic.APIConnectionError:
        raise RedaccionNoDisponible("No hay conexión con el servicio de redacción. Revise el internet e intente de nuevo.")

    if respuesta.stop_reason == "refusal":
        raise RedaccionNoDisponible("No se pudo mejorar este texto. Déjelo como lo escribió.")
    mejorado = "".join(b.text for b in respuesta.content if b.type == "text").strip().strip("«»\"")
    if not mejorado:
        raise RedaccionNoDisponible("No llegó una propuesta. Intente de nuevo.")
    return mejorado
