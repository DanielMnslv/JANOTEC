"""Herramientas de la plataforma anterior: respaldo / importación (mismo JSON), PDF de la semana,
historial por rango de fechas y visor de solo lectura para la administrativa."""
import re
import unicodedata
from datetime import date, datetime, time, timedelta
from io import BytesIO

from django.core import signing
from django.db import transaction
from django.utils import timezone


from . import semanal
from .forms import tecnicos_queryset
from .models import Actividad, Contacto

# Nombres de la plataforma anterior ↔ valores de esta
TIPOS = {"Mantenimiento": "mantenimiento", "Soporte Técnico": "soporte", "Instalación": "instalacion",
         "Visita Técnica": "visita"}
TIPOS_INV = {v: k for k, v in TIPOS.items()}
TIPOS_CONTACTO = {"Administrativa": "administrativa", "SST": "sst", "Contacto": "contacto"}
TIPOS_CONTACTO_INV = {v: k for k, v in TIPOS_CONTACTO.items()}


def _norm(texto):
    texto = unicodedata.normalize("NFD", str(texto or "").lower())
    return re.sub(r"\s+", " ", "".join(c for c in texto if unicodedata.category(c) != "Mn")).strip()


# --- Respaldo (exportar) -------------------------------------------------------------------
def respaldo(lunes):
    """Mismo formato que «Respaldo» de cronogramajanotec (rows, dir, week) para poder volver a importarlo."""
    filas = []
    for a in semanal.actividades_de(lunes):
        filas.append({
            "fecha": a.fecha.isoformat(), "hi": a.hora_inicio.strftime("%H:%M"), "hf": a.hora_fin.strftime("%H:%M"),
            "cliente": a.cliente, "sede": a.sede, "actividad": a.actividad, "tipo": TIPOS_INV.get(a.tipo, "Visita Técnica"),
            "linea": a.linea, "enc": [semanal.nombre(u) for u in a.encargados.all()],
            "aco": [semanal.nombre(u) for u in a.acompanantes.all()], "ins": a.insumos, "dest": a.correos,
            "sst": {"req": a.sst}, "veh": a.vehiculo, "via": a.viaticos, "sstPersona": a.sst_persona,
            "sstTrabajo": a.sst_trabajo, "sstDuracion": a.sst_duracion, "alturas": a.trabajo_alturas,
            "radicado": a.ticket.radicado if a.ticket else "",
        })
    directorio = [{"nombre": semanal.nombre(u), "email": u.email, "tipo": "Técnico"} for u in tecnicos_queryset()]
    directorio += [{"nombre": c.nombre, "email": c.email, "tipo": TIPOS_CONTACTO_INV[c.tipo]}
                   for c in Contacto.objects.filter(activo=True)]
    return {"plataforma": "JANOTEC tickets", "week": lunes.isoformat(), "rows": filas, "dir": directorio}


# --- Importar ----------------------------------------------------------------------------------
def _buscar_tecnico(nombre, tecnicos):
    """«Alberto», «Freyder Gordillo» o el usuario: el técnico que coincide (o None)."""
    n = _norm(nombre)
    if not n:
        return None
    for u in tecnicos:
        if n in (_norm(u.username), _norm(u.get_full_name()), _norm(u.first_name)):
            return u
    coinciden = [u for u in tecnicos if _norm(u.first_name).split(" ")[:1] == n.split(" ")[:1]
                 or _norm(u.get_full_name()).startswith(n)]
    return coinciden[0] if len(coinciden) == 1 else None


def _hora(texto, defecto):
    try:
        h, m = (str(texto or "").strip() or defecto).split(":")[:2]
        return time(int(h), int(m))
    except (ValueError, TypeError):
        return time.fromisoformat(defecto)


@transaction.atomic
def importar(datos, autor):
    """Crea las actividades (y contactos) de un respaldo. Devuelve un resumen para mostrar al usuario."""
    tecnicos = list(tecnicos_queryset())
    resumen = {"creadas": 0, "repetidas": 0, "sin_fecha": 0, "vacias": 0, "contactos": 0,
               "tecnicos_no_encontrados": set(), "semanas": set()}
    # Directorio: los contactos que traiga (los técnicos son usuarios de la plataforma)
    correos_dir = {}
    for c in datos.get("dir") or []:
        if not isinstance(c, dict):
            continue
        nombre, email, tipo = str(c.get("nombre", "")).strip(), str(c.get("email") or "").strip(), c.get("tipo")
        if email:
            correos_dir[_norm(nombre)] = email
        if tipo in TIPOS_CONTACTO and nombre:
            _, creado = Contacto.objects.get_or_create(nombre=nombre, tipo=TIPOS_CONTACTO[tipo],
                                                       defaults={"email": email if "@" in email else ""})
            resumen["contactos"] += creado
    for c in Contacto.objects.exclude(email=""):
        correos_dir.setdefault(_norm(c.nombre), c.email)

    for r in datos.get("rows") or []:
        if not isinstance(r, dict):
            continue
        if not str(r.get("cliente") or "").strip() and not str(r.get("actividad") or "").strip():
            resumen["vacias"] += 1  # fila en blanco de la plataforma anterior
            continue
        try:
            fecha = date.fromisoformat(str(r.get("fecha") or ""))
        except ValueError:
            resumen["sin_fecha"] += 1
            continue
        inicio, fin = _hora(r.get("hi"), "08:00"), _hora(r.get("hf"), "12:00")
        if fin <= inicio:
            fin = (datetime.combine(fecha, inicio) + timedelta(hours=1)).time()
        cliente = str(r.get("cliente") or "").strip()[:200] or "(sin cliente)"
        sede = str(r.get("sede") or "").strip()[:200]
        texto = str(r.get("actividad") or "").strip() or "(sin descripción)"
        if Actividad.objects.filter(fecha=fecha, hora_inicio=inicio, cliente=cliente, sede=sede,
                                    actividad__startswith=texto).exists():
            resumen["repetidas"] += 1
            continue
        destinos = []
        for d in r.get("dest") or []:
            d = str(d).strip()
            correo = d if "@" in d else correos_dir.get(_norm(d), "")
            if correo and correo not in destinos:
                destinos.append(correo)
        sst = r.get("sst") or {}
        linea = str(r.get("linea") or "").strip()[:60] or "Otro"
        duracion = r.get("sstDuracion") if r.get("sstDuracion") in Actividad.DURACIONES_SST else "medio día"
        actividad = Actividad.objects.create(
            fecha=fecha, hora_inicio=inicio, hora_fin=fin, cliente=cliente, sede=sede, actividad=texto,
            tipo=TIPOS.get(r.get("tipo"), Actividad.Tipo.VISITA), linea=linea, insumos=str(r.get("ins") or ""),
            notificar_a=" ; ".join(destinos)[:500], sst=bool(sst.get("req") if isinstance(sst, dict) else sst),
            sst_persona=str(r.get("sstPersona") or "")[:150], sst_trabajo=str(r.get("sstTrabajo") or "")[:200],
            sst_duracion=duracion, vehiculo=bool(r.get("veh")), viaticos=bool(r.get("via")),
            trabajo_alturas=bool(r.get("alturas")), creado_por=autor,
        )
        sin_usuario = []
        for campo, relacion, rol in (("enc", actividad.encargados, "Encargado"), ("aco", actividad.acompanantes, "Acompañante")):
            for nombre in r.get(campo) or []:
                u = _buscar_tecnico(nombre, tecnicos)
                if u:
                    relacion.add(u)
                else:
                    resumen["tecnicos_no_encontrados"].add(str(nombre))
                    sin_usuario.append(f"{rol}: {nombre}")
        if sin_usuario:  # queda anotado para asignarlo cuando exista el usuario
            actividad.actividad = f"{texto}\n[Plataforma anterior · {', '.join(sin_usuario)}]"
        actividad.calcular_choque()
        actividad.save(update_fields=["choque", "actividad"])
        resumen["creadas"] += 1
        resumen["semanas"].add(Actividad.lunes_de(fecha))
    return resumen


# --- Historial por rango -----------------------------------------------------------------------
def rango_rapido(clave, hoy=None):
    hoy = hoy or timezone.localdate()
    if clave == "mes":
        inicio = hoy.replace(day=1)
        fin = (inicio + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    elif clave == "trimestre":
        mes = (hoy.month - 1) // 3 * 3 + 1
        inicio = hoy.replace(month=mes, day=1)
        fin = (inicio + timedelta(days=95)).replace(day=1) - timedelta(days=1)
    else:
        inicio, fin = hoy.replace(month=1, day=1), hoy.replace(month=12, day=31)
    return inicio, fin


def historial(desde, hasta):
    actividades = list(Actividad.objects.filter(fecha__range=(desde, hasta))
                       .select_related("ticket").prefetch_related("encargados", "acompanantes"))
    ind = semanal.indicadores(actividades)
    semanas = {}
    for a in actividades:
        lunes = Actividad.lunes_de(a.fecha)
        semanas[lunes] = semanas.get(lunes, 0) + 1
    maximo = max(semanas.values(), default=1)
    ind["por_semana"] = [{"etiqueta": f"Semana {l:%d/%m}", "n": n, "pct": round(100 * n / maximo)}
                         for l, n in sorted(semanas.items())]
    ind["semanas"] = len(semanas)
    ind["soportes"] = sum(1 for a in actividades if a.tipo == Actividad.Tipo.SOPORTE)
    return ind


# --- Visor de solo lectura ------------------------------------------------------------------------
SAL_VISOR = "cronograma.visor"
DIAS_VISOR = 60


def token_visor(lunes):
    return signing.dumps({"l": lunes.isoformat()}, salt=SAL_VISOR)


def lunes_de_token(token):
    try:
        return date.fromisoformat(signing.loads(token, salt=SAL_VISOR, max_age=DIAS_VISOR * 24 * 3600)["l"])
    except (signing.BadSignature, KeyError, ValueError, TypeError):
        return None


# --- PDF de la semana (para WhatsApp) ---------------------------------------------------------------
def pdf_semana(lunes):
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    from comercial.pdf import BORDE, E, GRIS, OSCURO, VERDE, _esc, _logo

    actividades = list(semanal.actividades_de(lunes))
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=10 * mm, bottomMargin=12 * mm, title=f"Cronograma semana {semanal.etiqueta_semana(lunes)}",
                            author="JANOTEC S.A.S.")
    chico = ParagraphStyle("chico", parent=E, fontSize=7.8, leading=9.6)
    blanco = ParagraphStyle("blanco", parent=chico, fontName="Helvetica-Bold", textColor="white")
    titulo = ParagraphStyle("titulo", parent=E, fontName="Helvetica-Bold", fontSize=14, leading=17, textColor=OSCURO)
    logo = _logo()
    encabezado = Table([[logo, [Paragraph("Cronograma técnico semanal", titulo),
                                Paragraph(f"Semana del {lunes:%d/%m/%Y} al {lunes + timedelta(days=6):%d/%m/%Y} · "
                                          f"{len(actividades)} actividades", E)]]], colWidths=[52 * mm, None])
    encabezado.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LINEBELOW", (0, 0), (-1, 0), 2, VERDE)]))
    filas = [[Paragraph(t, blanco) for t in ("Día / hora", "Cliente / sede", "Actividad", "Técnicos", "Insumos")]]
    for a in actividades:
        marcas = [m for m, si in (("ALTURAS", a.trabajo_alturas), ("SST", a.sst), ("Vehículo", a.vehiculo),
                                  ("Viáticos", a.viaticos)) if si]
        filas.append([
            Paragraph(f"<b>{_esc(semanal.fecha_larga(a.fecha).capitalize())}</b><br/>{a.hora_inicio:%H:%M}–{a.hora_fin:%H:%M}", chico),
            Paragraph(f"<b>{_esc(a.cliente)}</b><br/>{_esc(a.sede)}<br/>{_esc(a.get_tipo_display())}", chico),
            Paragraph(_esc(a.actividad) + (f"<br/><font color='#b3141c'><b>{' · '.join(marcas)}</b></font>" if marcas else ""), chico),
            Paragraph(_esc(", ".join(semanal.nombre(u) for u in a.tecnicos())), chico),
            Paragraph(_esc(a.insumos), chico),
        ])
    if len(filas) == 1:
        filas.append([Paragraph("No hay actividades programadas.", chico), "", "", "", ""])
    tabla = Table(filas, colWidths=[34 * mm, 48 * mm, None, 42 * mm, 52 * mm], repeatRows=1)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GRIS), ("GRID", (0, 0), (-1, -1), 0.4, BORDE), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), ["white", "#f6f7f9"]),
    ]))
    doc.build([encabezado, Spacer(1, 4 * mm), tabla])
    return f"Cronograma-JANOTEC-{lunes:%Y-%m-%d}.pdf", buffer.getvalue()
