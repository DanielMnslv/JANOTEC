import json
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from accounts.models import Usuario
from tickets.models import Ticket
from tickets.permisos import personal_requerido, supervisor_requerido, tickets_visibles

from . import herramientas, semanal, servicios
from .forms import (
    ActividadForm, ConfiguracionEnvioForm, ContactoForm, DocumentoForm, ImportarForm, tecnicos_queryset,
)
from .models import Actividad, ConfiguracionEnvio, Contacto, DocumentoPersonal, EnlaceCronograma, ProgramacionSemana

DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def _lunes(request):
    try:
        dia = date.fromisoformat(request.GET.get("d", ""))
    except ValueError:
        dia = timezone.localdate()
    return Actividad.lunes_de(dia)


def _url_semana(dia):
    return f"{reverse('cronograma:semana')}?d={Actividad.lunes_de(dia):%Y-%m-%d}"


# --- Paso 3: cronograma semanal -------------------------------------------------------
@login_required
@personal_requerido
def semana(request):
    """El coordinador arma la semana arrastrando los tickets recepcionados; el técnico ve sus visitas."""
    lunes = _lunes(request)
    domingo = lunes + timedelta(days=6)
    actividades = (Actividad.objects.filter(fecha__range=(lunes, domingo))
                   .select_related("ticket", "ticket__empresa").prefetch_related("encargados", "acompanantes"))
    editable = request.user.es_supervisor
    if not editable:
        actividades = actividades.filter(Q(encargados=request.user) | Q(acompanantes=request.user)).distinct()
    por_dia = {lunes + timedelta(days=i): [] for i in range(7)}
    for a in actividades:
        por_dia[a.fecha].append(a)
    dias = [{"fecha": f, "nombre": DIAS[i], "actividades": lista, "hoy": f == timezone.localdate()}
            for i, (f, lista) in enumerate(por_dia.items()) if i < 6 or lista]
    contexto = {"lunes": lunes, "domingo": domingo, "dias": dias, "editable": editable, "pestana": "cronograma",
                "vista": "tablero" if request.GET.get("vista") == "tablero" else "tabla",  # la tabla por defecto: se ve toda la información
                "contactos_dir": Contacto.objects.filter(activo=True).exclude(email=""),
                "todas": [a for d in dias for a in d["actividades"]],
                "anterior": lunes - timedelta(days=7), "siguiente": lunes + timedelta(days=7),
                "total": sum(len(d["actividades"]) for d in dias),
                "en_rojo": sum(1 for d in dias for a in d["actividades"] if a.en_rojo)}
    if editable:
        from inventario.servicios import catalogo_planeacion
        contexto["catalogo_inventario"] = catalogo_planeacion()
        programables = (tickets_visibles(request.user).filter(area=Ticket.Area.TECNICA,
                                                              estado__in=Ticket.ESTADOS_PROGRAMABLES)
                        .select_related("ficha").prefetch_related("rangos_no_disponible", "aplazamientos")
                        .order_by("-prioridad", "creado"))
        # Lo que se llena solo al soltar el ticket en un día (cliente, sede, actividad, tipo, notificar a…)
        valores = {t.pk: dict(Actividad.valores_desde_ticket(t), radicado=t.radicado,
                              jornada=t.get_jornada_display(),
                              restricciones=t.resumen_no_disponible + (f" · {t.restricciones}" if t.restricciones else ""))
                   for t in programables}
        campos = ["fecha", "hora_inicio", "hora_fin", "cliente", "sede", "actividad", "tipo", "linea", "insumos",
                  "notificar_a", "trabajo_alturas", "sst", "sst_persona", "sst_trabajo", "sst_duracion", "vehiculo", "viaticos"]
        actividades_json = {}
        for d in dias:
            for a in d["actividades"]:
                datos = {c: getattr(a, c) for c in campos}
                datos.update(fecha=a.fecha.isoformat(), hora_inicio=a.hora_inicio.strftime("%H:%M"),
                             hora_fin=a.hora_fin.strftime("%H:%M"), ticket=a.ticket_id or "",
                             radicado=a.ticket.radicado if a.ticket else "",
                             encargados=[u.pk for u in a.encargados.all()],
                             acompanantes=[u.pk for u in a.acompanantes.all()])
                actividades_json[a.pk] = datos
        cambio = semanal.cambios(lunes)
        contexto["cambios_pendientes"] = bool(cambio and cambio["hay"])
        contexto["publicada"] = cambio["publicacion"] if cambio else None
        contexto.update(programables=programables, valores_tickets=valores, valores_actividades=actividades_json,
                        form=ActividadForm(), forzar=request.GET.get("forzar", ""))
    return render(request, "cronograma/semana.html", contexto)


def _volver(actividad):
    return redirect(_url_semana(actividad.fecha))


@login_required
@supervisor_requerido
@require_POST
def guardar(request, pk=None):
    """Crea (al soltar un ticket o con «Agregar actividad») o edita una actividad."""
    actividad = get_object_or_404(Actividad, pk=pk) if pk else None
    anterior = (actividad.fecha, actividad.hora_inicio, actividad.hora_fin) if actividad else None
    tecnicos_antes = list(actividad.tecnicos()) if actividad else []
    form = ActividadForm(request.POST, instance=actividad)
    if not form.is_valid():
        for campo, errores in form.errors.items():
            for e in errores:
                etiqueta = form.fields[campo].label if campo in form.fields else ""
                messages.error(request, f"{etiqueta}: {e}" if etiqueta else e)
        try:
            return redirect(_url_semana(date.fromisoformat(request.POST.get("fecha", ""))))
        except ValueError:
            return redirect("cronograma:semana")
    with transaction.atomic():
        actividad = form.save(commit=False)
        if not actividad.pk:
            actividad.creado_por = request.user
        actividad.save()
        form.save_m2m()
        servicios.guardar(actividad, request.user, horario_anterior=anterior, tecnicos_antes=tecnicos_antes)
    if actividad.choque:
        messages.error(request, f"¡Atención! {actividad.cliente}: {actividad.choque} La actividad quedó resaltada en rojo.")
    elif not actividad.correos and not actividad.es_interna:
        messages.warning(request, f"{actividad.cliente}: sin correo para notificar; la actividad quedó en rojo.")
    else:
        messages.success(request, f"Actividad guardada: {actividad.cliente} · {servicios.cuando(actividad)}.")
    if actividad.sst and not servicios.correo_sst():
        messages.warning(request, "Esta visita requiere acompañamiento SST, pero el área SST no tiene correo: agréguelo en "
                                  "Cronograma → Directorio o la solicitud no le llegará a nadie.")
    return _volver(actividad)


@login_required
@supervisor_requerido
@require_POST
def mover(request, pk):
    """Arrastrar una actividad a otro día (se responde en JSON para avisar el cruce de inmediato)."""
    actividad = get_object_or_404(Actividad, pk=pk)
    try:
        nueva = date.fromisoformat(json.loads(request.body or "{}").get("fecha", ""))
    except (ValueError, AttributeError):
        return JsonResponse({"ok": False, "error": "Fecha inválida"}, status=400)
    anterior = (actividad.fecha, actividad.hora_inicio, actividad.hora_fin)
    actividad.fecha = nueva
    servicios.guardar(actividad, request.user, horario_anterior=anterior)
    return JsonResponse({"ok": True, "choque": actividad.choque, "rojo": actividad.en_rojo,
                         "reenviar": anterior[0] != nueva and actividad.autorizacion == Actividad.Autorizacion.SIN_ENVIAR})


@login_required
@supervisor_requerido
@require_POST
def eliminar(request, pk):
    actividad = get_object_or_404(Actividad, pk=pk)
    fecha = actividad.fecha
    servicios.quitar(actividad, request.user)
    messages.info(request, "Actividad retirada del cronograma.")
    return redirect(_url_semana(fecha))


@login_required
@supervisor_requerido
@require_POST
def enviar_autorizacion(request, pk):
    actividad = get_object_or_404(Actividad.objects.prefetch_related("encargados", "acompanantes"), pk=pk)
    if actividad.es_interna:
        messages.error(request, "Es una actividad interna: no se le envía autorización a ningún cliente.")
        return _volver(actividad)
    if not actividad.correos:
        messages.error(request, "Agregue al menos un correo en «Notificar a» para enviar la autorización.")
        return _volver(actividad)
    _, problemas = servicios.documentos_para(actividad)
    if problemas and not request.POST.get("forzar"):
        messages.error(request, "Antes de enviar la autorización revise los documentos de los técnicos: "
                                + " ".join(problemas) + " Actualícelos en «Documentos del personal» o envíe de todas formas.")
        return redirect(f"{_url_semana(actividad.fecha)}&forzar={actividad.pk}")
    try:
        servicios.enviar_autorizacion(actividad, request.user)
    except Exception:  # noqa: BLE001
        servicios.log.exception("No se pudo enviar la autorización de la actividad %s", actividad.pk)
        messages.error(request, "No se pudo enviar el correo. Verifique la configuración de correo e intente de nuevo.")
        return _volver(actividad)
    messages.success(request, f"Autorización de ingreso enviada a {', '.join(actividad.correos)}.")
    return _volver(actividad)


# --- Respuesta del cliente desde el correo (sin iniciar sesión) ---------------------------
def autorizacion(request, token):
    """Los botones del correo abren esta página; la respuesta se registra al confirmar (POST).

    Así los antivirus de correo que abren los enlaces no aceptan ni rechazan por el cliente.
    """
    actividad = servicios.actividad_de_token(token)
    if actividad is None:
        return render(request, "cronograma/autorizacion.html", {"invalido": True}, status=404)
    if request.method == "POST":
        accion = request.POST.get("accion")
        if accion in ("aceptar", "rechazar"):
            servicios.responder_autorizacion(actividad, accion == "aceptar", request.POST.get("motivo", "").strip())
            return render(request, "cronograma/autorizacion.html", {"a": actividad, "respondida": True,
                                                                    "cuando": servicios.cuando(actividad)})
    return render(request, "cronograma/autorizacion.html", {
        "a": actividad, "cuando": servicios.cuando(actividad), "tecnicos": actividad.tecnicos(),
        "elegida": request.GET.get("r", ""),
    })


# --- Documentos del personal ---------------------------------------------------------
def _puede_ver_documentos(usuario, tecnico):
    return usuario.es_supervisor or usuario.pk == tecnico.pk


@login_required
@supervisor_requerido
def documentos(request):
    """Estado de los documentos de cada técnico (lo que se adjunta a las autorizaciones)."""
    tecnicos = tecnicos_queryset().prefetch_related(
        Prefetch("documentos", queryset=DocumentoPersonal.objects.filter(actual=True), to_attr="docs_actuales"))
    filas = []
    for t in tecnicos:
        actuales = {d.tipo: d for d in t.docs_actuales}
        filas.append({"tecnico": t, "docs": [(tipo, etiqueta, actuales.get(tipo)) for tipo, etiqueta in DocumentoPersonal.Tipo.choices]})
    return render(request, "cronograma/documentos.html", {"filas": filas, "tipos": DocumentoPersonal.Tipo.choices})


@login_required
@personal_requerido
def documentos_tecnico(request, pk=None):
    """Subir o actualizar los documentos de un técnico (el coordinador o el mismo técnico)."""
    tecnico = get_object_or_404(Usuario, pk=pk) if pk else request.user
    if not _puede_ver_documentos(request.user, tecnico):
        raise PermissionDenied
    if request.method == "POST":
        form = DocumentoForm(request.POST, request.FILES)
        if form.is_valid():
            doc = form.save(commit=False)
            doc.usuario = tecnico
            doc.subido_por = request.user
            doc.nombre_original = request.FILES["archivo"].name[:255]
            doc.save()
            messages.success(request, f"{doc.get_tipo_display()} actualizado para {tecnico}.")
            return redirect(request.path)
        for errores in form.errors.values():
            for e in errores:
                messages.error(request, e)
    actuales = DocumentoPersonal.actuales(tecnico)
    secciones = [(tipo, etiqueta, actuales.get(tipo), DocumentoForm(initial={"tipo": tipo}))
                 for tipo, etiqueta in DocumentoPersonal.Tipo.choices]
    historial = DocumentoPersonal.objects.filter(usuario=tecnico, actual=False)[:30]
    return render(request, "cronograma/documentos_tecnico.html",
                  {"tecnico": tecnico, "secciones": secciones, "historial": historial})


@login_required
@personal_requerido
def documento_descargar(request, pk):
    doc = get_object_or_404(DocumentoPersonal.objects.select_related("usuario"), pk=pk)
    if not _puede_ver_documentos(request.user, doc.usuario):
        raise Http404
    try:
        return FileResponse(doc.archivo.open("rb"), as_attachment=False, filename=doc.nombre_original)
    except FileNotFoundError:
        raise Http404("El archivo ya no está disponible.")


# --- Lo semanal: insumos, indicadores y correos -----------------------------------------
def _contexto_semana(request):
    lunes = _lunes(request)
    return {"lunes": lunes, "domingo": lunes + timedelta(days=6), "anterior": lunes - timedelta(days=7),
            "siguiente": lunes + timedelta(days=7)}


@login_required
@supervisor_requerido
def insumos(request):
    """Lo que hay que alistar en almacén: insumos sumados de toda la semana y por actividad."""
    ctx = _contexto_semana(request)
    actividades = list(semanal.actividades_de(ctx["lunes"]))
    consolidado = semanal.consolidar_insumos(actividades)
    from inventario.servicios import existencias_por_codigo
    existencias = existencias_por_codigo()
    for fila in consolidado:  # lo planeado con código del inventario se compara con lo que hay en bodega
        if fila["codigo"] and fila["codigo"] in existencias:
            fila["en_bodega"] = existencias[fila["codigo"]]
            fila["falta"] = max(fila["cantidad"] - fila["en_bodega"], 0)
    por_sede = {}
    for a in actividades:
        items = semanal.parse_insumos(a.insumos)
        if items:
            por_sede.setdefault(" · ".join(x for x in (a.cliente, a.sede) if x), []).extend(items)
    ctx.update(consolidado=consolidado, por_sede=sorted(por_sede.items()), pestana="insumos", resumen={
        "items": len(consolidado), "metros": round(sum(i["cantidad"] for i in consolidado if i["unidad"] == "m")),
        "unidades": round(sum(i["cantidad"] for i in consolidado if i["unidad"] == "und")), "sedes": len(por_sede)})
    return render(request, "cronograma/insumos.html", ctx)


@login_required
@supervisor_requerido
def indicadores(request):
    ctx = _contexto_semana(request)
    ctx.update(ind=semanal.indicadores(semanal.actividades_de(ctx["lunes"])), pestana="indicadores")
    return render(request, "cronograma/indicadores.html", ctx)


@login_required
@supervisor_requerido
def correos(request):
    """«Correos y envío»: destinatarios, vista previa, enviar todo, programar para el jueves y aviso de cambios."""
    ctx = _contexto_semana(request)
    lunes = ctx["lunes"]
    volver = f"{reverse('cronograma:correos')}?d={lunes:%Y-%m-%d}"
    if request.method == "POST":
        accion, faltan = request.POST.get("accion"), []
        if accion == "enviar":
            enviados, faltan = semanal.enviar_semana(lunes, request.user)
            messages.success(request, f"{semanal.semana_larga(lunes)}: enviados {enviados} correo(s).")
        elif accion == "cambios":
            enviados, faltan = semanal.notificar_cambios(lunes, request.user)
            messages.success(request, f"Aviso de cambios enviado: {enviados} correo(s).")
        elif accion == "uno":
            indice = request.POST.get("indice", "")
            correo = semanal.enviar_uno(lunes, int(indice), request.user) if indice.isdigit() else None
            if correo:
                messages.success(request, f"Enviado a {correo['nombre']} ({correo['email']}).")
            else:
                messages.error(request, "Ese destinatario no tiene correo: agréguelo en el Directorio.")
            return redirect(f"{volver}&ver={indice}")
        elif accion == "programar":
            ProgramacionSemana.objects.update_or_create(lunes=lunes, defaults={"programada_por": request.user})
            config = ConfiguracionEnvio.actual()
            aviso = "" if config.envio_automatico else " Ojo: el envío automático está apagado en Configuración del envío."
            messages.success(request, f"{semanal.semana_larga(lunes)} programada: se enviará sola el {config.get_hora_display()}.{aviso}")
        elif accion == "desprogramar":
            ProgramacionSemana.objects.filter(lunes=lunes).delete()
            messages.info(request, "Se quitó la programación del jueves para esta semana.")
        if faltan:
            messages.warning(request, "Sin correo: " + ", ".join(faltan) + ". Complételo en el Directorio o en Configuración → Usuarios.")
        return redirect(volver)
    cambio = semanal.cambios(lunes)
    lista, faltan = semanal.correos_semana(lunes, cambio=cambio if cambio and cambio["hay"] else None)
    for i, c in enumerate(lista):
        c["indice"] = i
    ver = request.GET.get("ver", "")
    if not ver.isdigit() and lista:
        ver = "0"
    actividades = list(semanal.actividades_de(lunes))
    grupos = [(titulo, [c for c in lista if c["tipo"] == tipo]) for tipo, titulo in (
        ("Técnico", "Técnicos"), ("Administrativa", "Administrativa (consolidado)"), ("SST", "Solicitudes SST"),
        ("Contacto", "Contactos / clientes"))]
    ctx.update(pestana="correos", cambio=cambio, correos=lista, faltan=faltan, ver=ver,
               grupos=[(t, cs) for t, cs in grupos if cs or t != "Contactos / clientes"],
               previa=lista[int(ver)] if ver.isdigit() and int(ver) < len(lista) else None,
               programada=ProgramacionSemana.objects.filter(lunes=lunes).first(), config=ConfiguracionEnvio.actual(),
               resumen={"actividades": len(actividades), "tecnicos": sum(1 for c in lista if c["tipo"] == "Técnico"),
                        "sst": sum(1 for a in actividades if a.sst), "faltan": len(faltan)})
    return render(request, "cronograma/correos.html", ctx)


# --- Lo que traía la plataforma anterior: vaciar, PDF, respaldo, historial, directorio… ---
@login_required
@supervisor_requerido
@require_POST
def vaciar(request):
    lunes = _lunes(request)
    actividades = list(semanal.actividades_de(lunes))
    for a in actividades:
        servicios.quitar(a, request.user)
    messages.info(request, f"Se vació el cronograma de la semana {semanal.etiqueta_semana(lunes)} ({len(actividades)} actividades).")
    return redirect(_url_semana(lunes))


@login_required
@personal_requerido
def pdf(request):
    nombre, contenido = herramientas.pdf_semana(_lunes(request))
    respuesta = HttpResponse(contenido, content_type="application/pdf")
    modo = "attachment" if request.GET.get("descargar") else "inline"
    respuesta["Content-Disposition"] = f'{modo}; filename="{nombre}"'
    return respuesta


@login_required
@supervisor_requerido
def respaldo(request):
    lunes = _lunes(request)
    respuesta = JsonResponse(herramientas.respaldo(lunes), json_dumps_params={"ensure_ascii": False, "indent": 2})
    respuesta["Content-Disposition"] = f'attachment; filename="cronograma_janotec_{lunes:%Y-%m-%d}.json"'
    return respuesta


@login_required
@supervisor_requerido
@require_POST
def importar(request):
    form = ImportarForm(request.POST, request.FILES)
    if not form.is_valid():
        for errores in form.errors.values():
            for e in errores:
                messages.error(request, e)
        return redirect("cronograma:semana")
    r = herramientas.importar(form.cleaned_data["archivo"], request.user)
    partes = [f"{r['creadas']} actividad(es) importada(s)"]
    if r["repetidas"]:
        partes.append(f"{r['repetidas']} ya existían")
    if r["contactos"]:
        partes.append(f"{r['contactos']} contacto(s) nuevos en el directorio")
    messages.success(request, ". ".join(partes) + ".")
    if r["tecnicos_no_encontrados"]:
        messages.warning(request, "No se encontraron como usuarios: " + ", ".join(sorted(r["tecnicos_no_encontrados"]))
                         + ". Créelos en Configuración y asígnelos en cada actividad.")
    if r["sin_fecha"]:
        messages.warning(request, f"{r['sin_fecha']} fila(s) sin fecha no se importaron.")
    if r["vacias"]:
        messages.info(request, f"Se descartaron {r['vacias']} fila(s) vacías.")
    destino = min(r["semanas"]) if r["semanas"] else _lunes(request)
    return redirect(_url_semana(destino))


@login_required
@supervisor_requerido
def historial(request):
    """El «Historial (BD)» de la plataforma anterior: aquí todo queda guardado, sin Supabase."""
    rapido = request.GET.get("rapido", "")
    try:
        desde, hasta = date.fromisoformat(request.GET.get("desde", "")), date.fromisoformat(request.GET.get("hasta", ""))
    except ValueError:
        desde, hasta = herramientas.rango_rapido(rapido or "mes")
    if desde > hasta:
        desde, hasta = hasta, desde
    ctx = _contexto_semana(request)
    ctx.update(pestana="historial", desde=desde, hasta=hasta, ind=herramientas.historial(desde, hasta))
    return render(request, "cronograma/historial.html", ctx)


@login_required
@supervisor_requerido
def directorio(request):
    """Administrativa, SST y contactos; los técnicos son los usuarios de la plataforma (con su correo)."""
    if request.method == "POST":
        if request.POST.get("eliminar"):
            Contacto.objects.filter(pk=request.POST["eliminar"]).delete()
            messages.info(request, "Contacto eliminado.")
            return redirect("cronograma:directorio")
        contacto = get_object_or_404(Contacto, pk=request.POST["pk"]) if request.POST.get("pk") else None
        form = ContactoForm(request.POST, instance=contacto)
        if form.is_valid():
            form.save()
            messages.success(request, f"Directorio actualizado: {form.instance.nombre}.")
            return redirect("cronograma:directorio")
        for errores in form.errors.values():
            for e in errores:
                messages.error(request, e)
    ctx = _contexto_semana(request)
    ctx.update(pestana="directorio", contactos=Contacto.objects.all(), tecnicos=tecnicos_queryset(), form=ContactoForm(),
               tipos=[(v, e.split(" (")[0]) for v, e in Contacto.Tipo.choices])
    return render(request, "cronograma/directorio.html", ctx)


@login_required
@supervisor_requerido
def configuracion(request):
    config = ConfiguracionEnvio.actual()
    form = ConfiguracionEnvioForm(request.POST or None, instance=config)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Configuración del envío guardada.")
        return redirect("cronograma:configuracion")
    ctx = _contexto_semana(request)
    ctx.update(pestana="configuracion", form=form, config=config)
    return render(request, "cronograma/configuracion.html", ctx)


def visor(request, token):
    """Solo lectura, sin iniciar sesión (el «visor.html» de la plataforma anterior), para la administrativa."""
    lunes = herramientas.lunes_de_token(token)
    if lunes is None:
        return render(request, "cronograma/visor.html", {"invalido": True}, status=404)
    actividades = list(semanal.actividades_de(lunes))
    return render(request, "cronograma/visor.html", {
        "lunes": lunes, "domingo": lunes + timedelta(days=6), "actividades": actividades,
        "insumos": semanal.consolidar_insumos(actividades), "semana": semanal.etiqueta_semana(lunes)})


@login_required
@supervisor_requerido
def compartir(request):
    """El enlace de solo lectura para quienes no usan la plataforma: se crea con un clic y se revoca si se filtra."""
    enlace = EnlaceCronograma.objects.filter(activo=True).first()
    if request.method == "POST":
        if request.POST.get("accion") == "revocar":
            EnlaceCronograma.objects.filter(activo=True).update(activo=False)
            messages.success(request, "Enlace revocado: ya no abre.")
        elif enlace is None:
            EnlaceCronograma.objects.create(creado_por=request.user)
            messages.success(request, "Enlace creado. Ya lo puede copiar y compartir.")
        return redirect("cronograma:compartir")
    if enlace:
        enlace.url = settings.SITIO_URL.rstrip("/") + reverse("cronograma:compartido", args=[enlace.token])
    ctx = _contexto_semana(request)
    ctx.update(pestana="compartir", enlace=enlace)
    return render(request, "cronograma/compartir.html", ctx)


@never_cache
def compartido(request, token):
    """El cronograma en vivo, sin iniciar sesión: abre en la semana actual y se puede pasar a otras."""
    enlace = EnlaceCronograma.objects.filter(token=token, activo=True).first()
    if enlace is None:
        return render(request, "cronograma/visor.html", {"invalido": True}, status=404)
    enlace.registrar_acceso()
    lunes = _lunes(request)
    actividades = list(semanal.actividades_de(lunes).select_related("ticket__empresa"))
    contexto = {"lunes": lunes, "domingo": lunes + timedelta(days=6), "actividades": actividades,
                "insumos": semanal.consolidar_insumos(actividades), "semana": semanal.etiqueta_semana(lunes),
                "compartido": True, "anterior": lunes - timedelta(days=7), "siguiente": lunes + timedelta(days=7),
                "actualizado": timezone.localtime()}
    plantilla = "cronograma/_visor_contenido.html" if request.GET.get("parcial") else "cronograma/visor.html"
    return render(request, plantilla, contexto)


@login_required
@supervisor_requerido
@require_POST
def enviar_actividad(request, pk):
    """El correo de una sola actividad: a sus técnicos y a la administrativa."""
    actividad = get_object_or_404(Actividad.objects.prefetch_related("encargados", "acompanantes"), pk=pk)
    enviados, faltan = semanal.enviar_actividad(actividad)
    messages.success(request, f"Correo de la actividad enviado ({enviados}).")
    if faltan:
        messages.warning(request, "Sin correo: " + ", ".join(faltan) + ".")
    return _volver(actividad)


@login_required
@supervisor_requerido
def correo_sst(request, pk):
    """Ver (y enviar ya, sin esperar la aceptación del cliente) la solicitud de acompañamiento HSE."""
    actividad = get_object_or_404(Actividad.objects.prefetch_related("encargados", "acompanantes"), pk=pk)
    destino = servicios.correo_sst()
    if request.method == "POST":
        if not destino:
            messages.error(request, "Agregue el correo del área SST en el Directorio.")
        else:
            servicios.avisar_sst(actividad)
            messages.success(request, f"Solicitud HSE enviada a {destino}.")
        return _volver(actividad)
    return render(request, "cronograma/correo_sst.html", {"a": actividad, "texto": servicios.texto_sst(actividad),
                                                          "destino": destino, "lunes": Actividad.lunes_de(actividad.fecha)})


@login_required
@personal_requerido
def horas_extras(request):
    """Auto reporte de salida a campo. El técnico ve lo suyo; el área administrativa ve todo y descarga el Excel."""
    from .forms import ReporteHorasExtraForm
    from .models import ReporteHorasExtra

    form = ReporteHorasExtraForm(request.POST or None, usuario=request.user)
    if request.method == "POST" and form.is_valid():
        reporte = form.save(commit=False)
        if not request.user.es_supervisor:
            reporte.colaborador = request.user
        reporte.registrado_por = request.user
        reporte.save()
        messages.success(request, f"Horas registradas: {reporte.duracion} h en {reporte.cliente}.")
        return redirect("cronograma:horas_extras")

    hoy = timezone.localdate()
    try:
        anio, mes = (int(x) for x in request.GET.get("mes", f"{hoy:%Y-%m}").split("-"))
        date(anio, mes, 1)
    except ValueError:
        anio, mes = hoy.year, hoy.month
    reportes = ReporteHorasExtra.objects.filter(fecha__year=anio, fecha__month=mes).select_related("colaborador")
    if not request.user.es_supervisor:
        reportes = reportes.filter(colaborador=request.user)
    reportes = list(reportes)

    totales = {}
    for r in reportes:
        totales[r.colaborador] = totales.get(r.colaborador, 0) + r.minutos
    totales = [(c, f"{m // 60}:{m % 60:02d}") for c, m in sorted(totales.items(), key=lambda x: x[0].get_full_name())]

    if request.user.es_supervisor and request.GET.get("formato") == "excel":
        from openpyxl import Workbook

        libro = Workbook()
        hoja = libro.active
        hoja.title = "Horas extras"
        hoja.append(["Colaborador", "Fecha", "Hora de salida", "Hora de llegada", "Horas", "Cliente", "Registró"])
        for r in sorted(reportes, key=lambda x: (x.fecha, x.hora_salida)):
            hoja.append([r.colaborador.get_full_name() or r.colaborador.username, r.fecha, r.hora_salida.strftime("%H:%M"),
                         r.hora_llegada.strftime("%H:%M"), round(r.minutos / 60, 2), r.cliente,
                         (r.registrado_por.get_full_name() or r.registrado_por.username) if r.registrado_por else ""])
            hoja.cell(hoja.max_row, 2).number_format = "DD/MM/YYYY"
        for col, ancho in zip("ABCDEFG", (34, 12, 14, 14, 8, 40, 30)):
            hoja.column_dimensions[col].width = ancho
        respuesta = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        respuesta["Content-Disposition"] = f'attachment; filename="horas_extras_{anio}-{mes:02d}.xlsx"'
        libro.save(respuesta)
        return respuesta

    return render(request, "cronograma/horas_extras.html", {
        "form": form, "reportes": reportes, "totales": totales, "mes": f"{anio}-{mes:02d}",
    })
