"""Lo semanal del cronograma: insumos consolidados, indicadores, correos de la semana y aviso de cambios.

Reproduce lo que hacía cronogramajanotec.netlify.app (allí los correos salían por n8n; aquí los envía
la plataforma, a mano desde «Correos y envío» o los jueves con ``manage.py enviar_cronograma``).
"""
import logging
import re
import unicodedata
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import formats, timezone

from .models import Actividad, ConfiguracionEnvio, Contacto, ProgramacionSemana, PublicacionSemana

log = logging.getLogger(__name__)


def actividades_de(lunes):
    return (Actividad.objects.filter(fecha__range=(lunes, lunes + timedelta(days=6)))
            .select_related("ticket").prefetch_related("encargados", "acompanantes"))


MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]


def semana_larga(lunes):
    """«Semana del 19 al 23 de octubre de 2026» (como titulaba los correos la plataforma anterior)."""
    viernes = lunes + timedelta(days=4)
    fin = f"{viernes.day} de {MESES[viernes.month - 1]} de {viernes.year}"
    if lunes.month == viernes.month:
        return f"Semana del {lunes.day} al {fin}"
    return f"Semana del {lunes.day} de {MESES[lunes.month - 1]} al {fin}"


def etiqueta_semana(lunes):
    viernes = lunes + timedelta(days=4)
    return f"{lunes:%d/%m} → {viernes:%d/%m}"


def nombre(usuario):
    return usuario.get_full_name() or usuario.username


def _norm(texto):
    texto = unicodedata.normalize("NFD", texto.lower())
    return re.sub(r"\s+", " ", "".join(c for c in texto if unicodedata.category(c) != "Mn")).strip()


# --- Insumos ---------------------------------------------------------------------------
def parse_insumos(texto):
    """«3 batería 12V 7.5 A, 20 m UTP; cinta» → [{cantidad, unidad, descripcion}] (igual que la app anterior)."""
    items = []
    for frag in re.split(r"[·;\n]|,(?!\d)", texto or ""):
        frag = frag.strip()
        if not frag:
            continue
        m = re.match(r"^(\d+(?:[.,]\d+)?)\s*(m\b|mts?\b|metros?\b)?\s*(.+)$", frag, re.I)
        if m:
            items.append({"cantidad": float(m.group(1).replace(",", ".")), "unidad": "m" if m.group(2) else "und",
                          "descripcion": m.group(3).strip()})
        else:
            items.append({"cantidad": 1.0, "unidad": "und", "descripcion": frag})
    return items


def consolidar_insumos(actividades):
    """Suma por insumo (y unidad) y dice en qué sedes se usa."""
    mapa = {}
    for a in actividades:
        lugar = " · ".join(x for x in (a.cliente, a.sede) if x) or "(sin sede)"
        for it in parse_insumos(a.insumos):
            codigo = re.search(r"\[([^\]]+)\]\s*$", it["descripcion"])
            clave = (codigo.group(1).upper() if codigo else _norm(it["descripcion"]), it["unidad"])
            fila = mapa.setdefault(clave, {"descripcion": it["descripcion"], "unidad": it["unidad"], "cantidad": 0.0,
                                           "sedes": [], "codigo": codigo.group(1).upper() if codigo else ""})
            fila["cantidad"] += it["cantidad"]
            if lugar not in fila["sedes"]:
                fila["sedes"].append(lugar)
    filas = sorted(mapa.values(), key=lambda f: -f["cantidad"])
    for f in filas:
        f["cantidad_texto"] = f"{f['cantidad']:g}"
    return filas


# --- Indicadores -------------------------------------------------------------------------
def indicadores(actividades):
    actividades = list(actividades)

    def contar(clave, filtro=lambda a: True):
        c = Counter()
        for a in actividades:
            if filtro(a):
                valores = clave(a)
                for v in (valores if isinstance(valores, list) else [valores]):
                    if v:
                        c[v] += 1
        maximo = max(c.values(), default=1)
        return [{"etiqueta": k, "n": n, "pct": round(100 * n / maximo)} for k, n in c.most_common(10)]

    soporte_o_mant = lambda a: a.tipo in (Actividad.Tipo.SOPORTE, Actividad.Tipo.MANTENIMIENTO)  # noqa: E731
    por_tipo = contar(lambda a: a.get_tipo_display())
    por_cliente = contar(lambda a: a.cliente)
    por_sede = contar(lambda a: f"{a.cliente} · {a.sede}" if a.sede else a.cliente, soporte_o_mant)
    return {
        "total": len(actividades),
        "tipo_top": por_tipo[0] if por_tipo else None,
        "cliente_top": por_cliente[0] if por_cliente else None,
        "sede_top": por_sede[0] if por_sede else None,
        "por_tipo": por_tipo, "por_cliente": por_cliente, "por_sede": por_sede,
        "por_tecnico": contar(lambda a: [nombre(u) for u in a.tecnicos()]),
        "por_linea": contar(lambda a: a.linea),
        "soportes_por_cliente": contar(lambda a: a.cliente, lambda a: a.tipo == Actividad.Tipo.SOPORTE),
        "en_rojo": sum(1 for a in actividades if a.en_rojo),
        "autorizadas": sum(1 for a in actividades if a.autorizacion == Actividad.Autorizacion.ACEPTADA),
    }


# --- Foto de lo enviado y cambios ------------------------------------------------------------
CAMPOS_FOTO = {"fecha": "Fecha", "inicio": "Inicio", "fin": "Fin", "cliente": "Cliente", "sede": "Sede",
               "actividad": "Actividad", "tipo": "Tipo", "tecnicos": "Técnicos", "insumos": "Insumos"}


def foto(actividades):
    return {str(a.pk): {"fecha": a.fecha.isoformat(), "inicio": a.hora_inicio.strftime("%H:%M"),
                        "fin": a.hora_fin.strftime("%H:%M"), "cliente": a.cliente, "sede": a.sede,
                        "actividad": a.actividad, "tipo": a.get_tipo_display(),
                        "tecnicos": ", ".join(nombre(u) for u in a.tecnicos()), "insumos": a.insumos}
            for a in actividades}


def cambios(lunes):
    """Qué cambió desde el último envío de la semana (None si nunca se envió)."""
    pub = PublicacionSemana.objects.filter(lunes=lunes).first()
    if not pub:
        return None
    antes, ahora = pub.foto, foto(actividades_de(lunes))
    nuevas = [ahora[k] for k in ahora if k not in antes]
    quitadas = [antes[k] for k in antes if k not in ahora]
    modificadas = []
    for k in ahora.keys() & antes.keys():
        campos = [(CAMPOS_FOTO[c], antes[k].get(c, ""), ahora[k][c]) for c in CAMPOS_FOTO if antes[k].get(c, "") != ahora[k][c]]
        if campos:
            modificadas.append({"actividad": ahora[k], "campos": campos})
    # Técnicos a los que les toca saberlo (antes o ahora)
    afectados = set()
    for fila in nuevas + quitadas + [m["actividad"] for m in modificadas]:
        afectados.update(n.strip() for n in fila["tecnicos"].split(",") if n.strip())
    for m in modificadas:
        for etiqueta, viejo, _ in m["campos"]:
            if etiqueta == "Técnicos":
                afectados.update(n.strip() for n in viejo.split(",") if n.strip())
    return {"publicacion": pub, "nuevas": nuevas, "quitadas": quitadas, "modificadas": modificadas,
            "hay": bool(nuevas or quitadas or modificadas), "afectados": afectados}


# --- Correos ----------------------------------------------------------------------------------
def _url(lunes):
    return f"{settings.SITIO_URL.rstrip('/')}{reverse('cronograma:semana')}?d={lunes:%Y-%m-%d}"


def tecnicos_de(actividades):
    vistos = {}
    for a in actividades:
        for u in a.tecnicos():
            vistos.setdefault(u.pk, u)
    return sorted(vistos.values(), key=nombre)


def _filas_tecnico(actividades, tecnico):
    filas = []
    for a in actividades:
        if tecnico in a.tecnicos():
            filas.append({"a": a, "rol": "Encargado" if tecnico in a.encargados.all() else "Apoyo",
                          "insumos": parse_insumos(a.insumos)})
    return filas


def correos_semana(lunes, cambio=None):
    """Los correos de la semana, como en «Correos y envío» de la plataforma anterior.

    - Uno por técnico con sus visitas e insumos, y el consolidado para la administrativa.
    - Las solicitudes SST de la semana que aún no se han enviado.
    - Con `cambio` (resultado de cambios()), el aviso «IMPORTANTE – Cambios en el cronograma» solo a los afectados.

    Quien no tiene correo también aparece (con `email` vacío) para verlo y completar el Directorio;
    al enviar se salta. Devuelve (correos, faltan).
    """
    actividades = list(actividades_de(lunes))
    semana = semana_larga(lunes)
    base = {"semana": semana, "url": _url(lunes), "cambio": cambio, "remitente": ConfiguracionEnvio.actual().remitente,
            "generado": timezone.localtime()}
    prefijo = "IMPORTANTE – Cambios en el cronograma" if cambio else "Cronograma de actividades"
    tecnicos = tecnicos_de(actividades)
    if cambio:
        # Solo los afectados, incluidos los que ya no tienen visitas pero estaban en la versión enviada
        from .forms import tecnicos_queryset
        tecnicos += [u for u in tecnicos_queryset().exclude(pk__in=[t.pk for t in tecnicos])]
        tecnicos = [t for t in tecnicos if nombre(t) in cambio["afectados"]]
    correos = []
    for t in tecnicos:
        filas = _filas_tecnico(actividades, t)
        ctx = dict(base, destinatario=nombre(t), filas=filas)
        correos.append({"tipo": "Técnico", "nombre": nombre(t), "email": t.email, "n": len(filas),
                        "asunto": f"{prefijo} · {nombre(t)} · {semana}",
                        "html": render_to_string("cronograma/correo_semana_tecnico.html", ctx)})
    ctx = dict(base, destinatario="Área administrativa", actividades=actividades, insumos=consolidar_insumos(actividades))
    correos.append({"tipo": "Administrativa", "nombre": "Administrativa", "email": correo_administrativa(),
                    "n": len(actividades), "asunto": f"{prefijo} consolidado · {semana}",
                    "html": render_to_string("cronograma/correo_semana_admin.html", ctx)})
    if not cambio:
        from .servicios import correo_sst
        destino = correo_sst()
        for a in actividades:
            if a.sst and not a.sst_enviada:
                correos.append(correo_sst_de(a, destino))
        if ConfiguracionEnvio.actual().avisar_contactos:
            correos += _correos_contactos(actividades, base, semana)
    faltan = [c["nombre"] + (" (agréguela en el Directorio)" if c["tipo"] in ("Administrativa", "SST") else "")
              for c in correos if not c["email"]]
    return correos, faltan


def correo_sst_de(actividad, destino):
    """La solicitud de acompañamiento HSE como correo (vista previa y envío)."""
    from .servicios import cuando, texto_sst

    lunes = Actividad.lunes_de(actividad.fecha)
    ctx = {"semana": semana_larga(lunes), "url": "", "cambio": None, "remitente": ConfiguracionEnvio.actual().remitente,
           "generado": timezone.localtime(), "texto": texto_sst(actividad), "a": actividad}
    lugar = " ".join(x for x in (actividad.cliente, actividad.sede) if x)
    return {"tipo": "SST", "nombre": lugar, "email": destino, "n": 1, "actividad_sst": actividad.pk,
            "detalle": f"{actividad.sst_persona or 'HSE'} · {cuando(actividad)}",
            "asunto": f"Solicitud de acompañamiento HSE · {lugar} · {fecha_larga(actividad.fecha)}",
            "html": render_to_string("cronograma/correo_sst_email.html", ctx)}


def correo_administrativa():
    return Contacto.correo_de(Contacto.Tipo.ADMINISTRATIVA, settings.NOTIFICAR_ADMINISTRATIVA_EMAIL)


def _correos_contactos(actividades, base, semana):
    """Aviso a cada contacto (cliente o almacén) de las visitas en las que figura en «Notificar a»."""
    por_correo = {}
    for a in actividades:
        for c in a.correos:
            por_correo.setdefault(c.lower(), []).append(a)
    nombres = {c.email.lower(): c.nombre for c in Contacto.objects.filter(activo=True).exclude(email="")}
    correos = []
    for email, visitas in sorted(por_correo.items()):
        ctx = dict(base, destinatario=nombres.get(email, email), visitas=visitas, url="")
        correos.append({"tipo": "Contacto", "nombre": nombres.get(email, email), "email": email,
                        "asunto": f"Visitas programadas de JANOTEC · Semana {semana}",
                        "html": render_to_string("cronograma/correo_semana_contacto.html", ctx)})
    return correos


def _enviar(correos):
    enviados = 0
    for c in correos:
        if not c.get("email"):
            continue
        texto = re.sub(r"<[^>]+>", " ", c["html"])
        texto = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", texto)).strip()
        msg = EmailMultiAlternatives(c["asunto"], texto, settings.DEFAULT_FROM_EMAIL, [c["email"]])
        msg.attach_alternative(c["html"], "text/html")
        try:
            msg.send(fail_silently=False)
            enviados += 1
            if c.get("actividad_sst"):
                Actividad.objects.filter(pk=c["actividad_sst"]).update(sst_enviada=timezone.now())
        except Exception:  # noqa: BLE001 — un correo malo no detiene los demás
            log.exception("No se pudo enviar «%s» a %s", c["asunto"], c["email"])
    return enviados


def _publicar(lunes, autor, automatica):
    PublicacionSemana.objects.update_or_create(lunes=lunes, defaults={
        "enviada_en": timezone.now(), "enviada_por": autor, "automatica": automatica,
        "foto": foto(actividades_de(lunes))})


def enviar_semana(lunes, autor=None, automatica=False):
    """Envía el cronograma de la semana a los técnicos, la administrativa y SST. Devuelve (enviados, faltan)."""
    correos, faltan = correos_semana(lunes)
    enviados = _enviar(correos)
    _publicar(lunes, autor, automatica)
    ProgramacionSemana.objects.filter(lunes=lunes).delete()
    return enviados, faltan


def enviar_uno(lunes, indice, autor=None):
    """«Enviar ahora» de un solo correo de la lista (no marca la semana como enviada)."""
    cambio = cambios(lunes)
    correos, _ = correos_semana(lunes, cambio=cambio if cambio and cambio["hay"] else None)
    if not 0 <= indice < len(correos) or not correos[indice]["email"]:
        return None
    _enviar([correos[indice]])
    return correos[indice]


def notificar_cambios(lunes, autor=None, automatica=False):
    """Avisa a los técnicos afectados y a la administrativa qué cambió desde el último envío."""
    cambio = cambios(lunes)
    if not cambio or not cambio["hay"]:
        return 0, []
    correos, faltan = correos_semana(lunes, cambio=cambio)
    enviados = _enviar(correos)
    _publicar(lunes, autor, automatica)
    return enviados, faltan


DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def fecha_larga(dia):
    """«viernes, 14 de agosto de 2026» (en minúscula, como la plataforma anterior)."""
    return f"{DIAS[dia.weekday()]}, {dia.day} de {MESES[dia.month - 1]} de {dia.year}"


def hora_corta(hora):
    """08:00 → «8:00 a. m.», 12:00 → «12:00 m.», 14:30 → «2:30 p. m.»"""
    if hora.hour == 12 and hora.minute == 0:
        return "12:00 m."
    return f"{(hora.hour % 12) or 12}:{hora.minute:02d} {'a. m.' if hora.hour < 12 else 'p. m.'}"


def enviar_actividad(actividad):
    """El correo de una sola actividad (botón de la fila): a sus técnicos y copia a la administrativa."""
    lunes = Actividad.lunes_de(actividad.fecha)
    base = {"semana": etiqueta_semana(lunes), "url": _url(lunes), "cambio": None,
            "remitente": ConfiguracionEnvio.actual().remitente, "a": actividad, "generado": timezone.localtime(),
            "insumos": parse_insumos(actividad.insumos), "cuando": fecha_larga(actividad.fecha)}
    asunto = f"Actividad {fecha_larga(actividad.fecha)} · {actividad.cliente} {actividad.sede}".strip()
    correos, faltan = [], []
    for t in actividad.tecnicos():
        if not t.email:
            faltan.append(nombre(t))
            continue
        rol = "Encargado" if t in actividad.encargados.all() else "Apoyo"
        correos.append({"tipo": "Técnico", "nombre": nombre(t), "email": t.email, "asunto": asunto,
                        "html": render_to_string("cronograma/correo_actividad.html", dict(base, destinatario=nombre(t), rol=rol))})
    admin = correo_administrativa()
    if admin:
        correos.append({"tipo": "Administrativa", "nombre": "Área administrativa", "email": admin,
                        "asunto": f"Aviso de actividad · {actividad.cliente} {actividad.sede} · {fecha_larga(actividad.fecha)}",
                        "html": render_to_string("cronograma/correo_actividad.html", dict(base, destinatario="Área administrativa"))})
    else:
        faltan.append("Área administrativa (agréguela en el Directorio)")
    return _enviar(correos), faltan
