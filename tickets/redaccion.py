"""Ayuda de redacción para los campos que escriben los técnicos en el acta de soporte.

Tres niveles:

1. Ayuda fija, sin costo: a cada campo le corresponde una guía corta, frases rápidas para tocar y un mínimo de
   palabras para avisar cuando el texto queda muy pobre; además el corrector en español del navegador o del
   celular subraya las palabras mal escritas. Lo usa static/js/redaccion.js.
2. Botón «Revisar ortografía», gratuito: LanguageTool señala tildes, ortografía, gramática y puntuación, y el técnico
   aplica cada corrección con un toque (o todas a la vez).
3. Botón «Mejorar redacción» (opcional, con costo): le pide a Claude (Anthropic) que reescriba el texto del técnico en español formal
   y claro, sin agregar hechos. Solo se activa si el servidor tiene ANTHROPIC_API_KEY en el .env y la librería
   «anthropic» instalada; si falta cualquiera de las dos, el botón no aparece y lo demás sigue funcionando.

Al servicio externo solo viaja el párrafo del campo: ni el cliente, ni la sede, ni el ticket.
"""

import logging
import os

log = logging.getLogger(__name__)

MODELO = "claude-opus-5-5"
LARGO_MAXIMO = 2000  # caracteres que se aceptan para mejorar (un campo del acta no debería pasar de esto)

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
}

INSTRUCCIONES = (
    "Eres el corrector de estilo de las actas técnicas de JANOTEC, empresa colombiana de seguridad electrónica "
    "(CCTV, alarmas, control de acceso, redes y cableado). Un técnico escribió el texto en campo, a veces desde el "
    "celular y con prisa. Reescríbelo para que quede bien presentado en un acta y en un informe para el cliente.\n\n"
    "Reglas:\n"
    "- Español de Colombia, formal y claro, en forma impersonal («Se realizó…», «Se encontró…»).\n"
    "- Corrige ortografía, tildes, puntuación y mayúsculas; ordena las ideas en oraciones completas.\n"
    "- No agregues hechos, causas, cifras, equipos, recomendaciones ni conclusiones que no estén en el texto.\n"
    "- Conserva tal cual marcas, referencias, modelos, seriales, cantidades, medidas, zonas y nombres.\n"
    "- Si el texto ya es breve y claro, solo corrígelo: no lo alargues.\n"
    "- Devuelve únicamente el texto final, sin comillas, títulos, viñetas ni comentarios."
)


def ia_disponible():
    """True si se puede usar el botón «Mejorar redacción»: hay clave en el .env y la librería está instalada."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
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


def mejorar(texto, tipo):
    """Devuelve el texto del técnico reescrito por Claude, o lanza RedaccionNoDisponible con un mensaje para mostrar."""
    texto = (texto or "").strip()
    if not texto:
        raise RedaccionNoDisponible("Escriba primero el texto que quiere mejorar.")
    if len(texto) > LARGO_MAXIMO:
        raise RedaccionNoDisponible(f"El texto es muy largo para mejorarlo (máximo {LARGO_MAXIMO} caracteres).")
    if not ia_disponible():
        raise RedaccionNoDisponible("La mejora de redacción no está activada en el servidor.")
    import anthropic

    contexto = TIPOS.get(tipo, TIPOS["resultado"])["contexto"]
    cliente = anthropic.Anthropic(timeout=40.0, max_retries=1)
    try:
        respuesta = cliente.beta.messages.create(
            model=MODELO,
            max_tokens=4000,
            output_config={"effort": "low"},  # corregir estilo no necesita razonar a fondo
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # si el modelo se niega, la API reintenta sola con otro modelo
            system=INSTRUCCIONES,
            messages=[{"role": "user", "content": f"El texto corresponde a {contexto}.\n\n<texto>\n{texto}\n</texto>"}],
        )
    except anthropic.AuthenticationError:
        log.error("Redacción: la clave ANTHROPIC_API_KEY no es válida")
        raise RedaccionNoDisponible("La clave del servicio de redacción no es válida. Avise a sistemas.")
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
