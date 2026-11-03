import os
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.core.files.base import ContentFile
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts.models import Empresa

from . import cierre, diagnostico, etapas, notificaciones, pasos, reincidencia
from .forms import (
    ActaForm,
    AplazarForm,
    CalificacionForm,
    EquipoActaFormSet,
    FiltroActasForm,
    FiltroTicketsForm,
    GestionForm,
    InformeBorradorForm,
    RadicacionForm,
    RespuestaClienteForm,
)
from .models import (
    ActaSoporte, Adjunto, EquipoActa, EvidenciaActa, FirmaActa, FotoEquipo, InformeBorrador, OportunidadComercial, Seguimiento, Ticket,
)
from .permisos import (
    actas_visibles, cliente_requerido, comercial_o_supervisor, gestiona_ticket, obtener_ticket, personal_requerido,
    radicador_requerido, supervisor_requerido, tickets_visibles,
)

E = Ticket.Estado


def _inicio_del_dia(dia):
    """Medianoche local como datetime con zona horaria.

    Se filtra por rangos (y no con __date / __year) porque MySQL sin las tablas
    de zonas horarias cargadas devuelve NULL en esas conversiones.
    """
    return timezone.make_aware(datetime.combine(dia, time.min))


def _guardar_adjuntos(ticket, archivos, usuario, seguimiento=None):
    for archivo in archivos:
        Adjunto.objects.create(
            ticket=ticket,
            seguimiento=seguimiento,
            archivo=archivo,
            nombre_original=archivo.name[:255],
            tamano=archivo.size,
            subido_por=usuario,
        )


ESTADOS_POR_ATENDER = [E.EN_CRONOGRAMA, E.AUTORIZADO]


def _filtrar(qs, form, usuario):
    if not form.is_valid():
        return qs
    d = form.cleaned_data
    if d.get("q"):
        q = d["q"].strip()
        qs = qs.filter(
            Q(radicado__icontains=q) | Q(asunto__icontains=q) | Q(descripcion__icontains=q)
            | Q(empresa__razon_social__icontains=q)
        )
    if d.get("estado") == "abiertos":
        qs = qs.filter(estado__in=Ticket.ESTADOS_ABIERTOS + [E.VISITA_EJECUTADA])
    elif d.get("estado") == "por_atender":  # en el cronograma y aún sin acta: lo que el técnico tiene por hacer
        qs = qs.filter(estado__in=ESTADOS_POR_ATENDER)
    elif d.get("estado"):
        qs = qs.filter(estado=d["estado"])
    if d.get("tipo"):
        qs = qs.filter(tipo=d["tipo"])
    if d.get("prioridad"):
        qs = qs.filter(prioridad=d["prioridad"])
    if d.get("empresa"):
        qs = qs.filter(empresa=d["empresa"])
    asignado = d.get("asignado")
    if asignado == "mios":
        qs = qs.filter(asignado_a=usuario)
    elif asignado == "sin":
        qs = qs.filter(asignado_a__isnull=True)
    elif asignado:
        qs = qs.filter(asignado_a_id=asignado)
    return qs


@login_required
def inicio(request):
    if request.user.es_personal:
        return redirect("panel")
    if request.user.es_bodega:  # también la de bodega que atiende lo comercial (tiene el enlace en la barra)
        return redirect("inventario:panel")
    if request.user.es_comercial:
        return redirect("comercial:bandeja")
    return redirect("mis_tickets")  # cliente y central de atención


@login_required
def asignados_a_mi(request):
    """Tickets que el área técnica dejó en manos de comercial, bodega, central o dirección."""
    if not request.user.es_interno:
        raise PermissionDenied
    tickets = (tickets_visibles(request.user).filter(asignado_a=request.user, estado__in=Ticket.ESTADOS_ABIERTOS)
               .order_by("-actualizado"))
    return render(request, "tickets/asignados.html", {"tickets": tickets})


@login_required
def buscar_radicado(request):
    radicado = request.GET.get("radicado", "").strip().upper()
    if not radicado:
        return redirect("inicio")
    ticket = tickets_visibles(request.user).filter(radicado=radicado).first()
    if ticket:
        return redirect(ticket)
    messages.warning(request, f"No se encontró el radicado {radicado} entre sus solicitudes.")
    return redirect("inicio")


# ============================================================================
# Portal del cliente
# ============================================================================
@login_required
@radicador_requerido
def mis_tickets(request):
    """Cliente: las solicitudes de su empresa. Central de atención y comercial: las que radicaron."""
    cierre.revisar_si_toca()
    base = tickets_visibles(request.user)
    if not request.user.es_cliente:
        base = base.filter(radicado_por=request.user)
    form = FiltroTicketsForm(request.GET or None, para_personal=False)
    qs = _filtrar(base, form, request.user)
    resumen = {
        "abiertos": base.filter(estado__in=Ticket.ESTADOS_ABIERTOS).count(),
        "programadas": base.filter(estado__in=[E.EN_CRONOGRAMA, E.AUTORIZADO]).count(),
        "por_confirmar": base.filter(estado=E.VISITA_EJECUTADA).count(),
        "cerrados": base.filter(estado__in=Ticket.ESTADOS_FINALES).count(),
    }
    # Como un correo: lo último que llegó, primero
    pagina = Paginator(qs.order_by("-creado", "-id"), 15).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "tickets/cliente_lista.html",
        {"pagina": pagina, "form": form, "resumen": resumen, "params": params.urlencode()},
    )


@login_required
@radicador_requerido
def nuevo_ticket(request):
    """Paso 1: registrar una solicitud de soporte técnico, mantenimiento o toma de requerimientos."""
    if request.user.es_cliente and not request.user.empresa_id:
        messages.error(request, "Su usuario no está vinculado a una empresa. Contacte a JANOTEC.")
        return redirect("mis_tickets")
    form = RadicacionForm(request.POST or None, request.FILES or None, usuario=request.user)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            ticket = form.save(commit=False)
            if form.contacto_es_interno:
                messages.warning(request, "El contacto registrado es del personal JANOTEC: los avisos y la autorización de "
                                          "ingreso no le llegarán al cliente. Corríjalo en «Editar datos».")
            ticket.solicitante = request.user
            ticket.radicado_por = request.user
            ticket.asignar_prioridad_inicial()
            ticket.save()
            for desde, hasta in form.rangos_validos():
                ticket.rangos_no_disponible.create(desde=desde, hasta=hasta)
            if request.user.es_personal and reincidencia.visitas_previas(ticket):
                messages.warning(request, "Esta sede tuvo soporte del mismo sistema en los últimos 30 días: revise el aviso "
                                          "de servicio repetido antes de programar.")
            quien = "" if request.user.es_cliente else f" por {request.user} ({request.user.rol_corto})"
            seg = Seguimiento.objects.create(
                ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.CREACION,
                mensaje=f"Solicitud registrada{quien}.", estado_nuevo=E.GENERADO,
            )
            _guardar_adjuntos(ticket, form.cleaned_data["archivos"], request.user, seg)
        notificaciones.ticket_creado(ticket)
        messages.success(
            request,
            f"La solicitud ({ticket.get_tipo_display().lower()}) quedó registrada con el número {ticket.radicado}. "
            "El área técnica la recepcionará y la programará en el cronograma; las respuestas llegarán a los correos indicados.",
        )
        return redirect(ticket)
    return render(request, "tickets/cliente_nuevo.html", {"form": form})


@login_required
@cliente_requerido
@require_POST
def responder_cliente(request, radicado):
    ticket = obtener_ticket(request.user, radicado)
    if not ticket.abierto:
        messages.error(request, "Esta solicitud ya está cerrada. Radique una nueva si lo necesita.")
        return redirect(ticket)
    form = RespuestaClienteForm(request.POST, request.FILES)
    if form.is_valid():
        with transaction.atomic():
            seg = Seguimiento.objects.create(
                ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.COMENTARIO,
                mensaje=form.cleaned_data["mensaje"],
            )
            _guardar_adjuntos(ticket, form.cleaned_data["archivos"], request.user, seg)
            ticket.save(update_fields=["actualizado"])
        notificaciones.respuesta_del_cliente(ticket, seg)
        messages.success(request, "Su mensaje fue enviado al área de soporte.")
    else:
        for errores in form.errors.values():
            for e in errores:
                messages.error(request, e)
    return redirect(ticket)


@login_required
@cliente_requerido
@require_POST
def confirmar_solucion(request, radicado):
    """Tras la visita: el cliente confirma que quedó solucionado (cierra) o pide una nueva visita."""
    ticket = obtener_ticket(request.user, radicado)
    if ticket.estado != E.VISITA_EJECUTADA:
        raise Http404
    accion = request.POST.get("accion")
    if accion == "aceptar":
        seg = ticket.cambiar_estado(E.CERRADO, request.user, "El cliente confirmó que la solicitud quedó resuelta.")
        notificaciones.actualizacion_para_cliente(ticket, seg)  # «solucionada y cerrada», con las estrellas
        notificaciones.respuesta_del_cliente(ticket, seg)
        messages.success(request, "Gracias por confirmar. La solicitud quedó cerrada.")
    elif accion == "reabrir":
        motivo = request.POST.get("motivo", "").strip() or "El cliente indica que el problema persiste."
        ticket.cambiar_estado(E.PENDIENTE_VISITA, request.user, motivo)
        notificaciones.respuesta_del_cliente(ticket, ticket.seguimientos.last())
        messages.info(request, "Programaremos una nueva visita; el área técnica continuará con el caso.")
    return redirect(ticket)


@login_required
@cliente_requerido
@require_POST
def calificar(request, radicado):
    ticket = obtener_ticket(request.user, radicado)
    if ticket.estado != E.CERRADO or ticket.calificacion:
        raise Http404
    form = CalificacionForm(request.POST)
    if form.is_valid():
        from .calificacion import registrar
        registrar(ticket, form.cleaned_data["calificacion"], form.cleaned_data["comentario"], request.user)
        messages.success(request, "¡Gracias por su calificación!")
    else:
        messages.error(request, "Elija de 1 a 10 estrellas.")
    return redirect(ticket)


@login_required
@cliente_requerido
@require_POST
def cancelar(request, radicado):
    ticket = obtener_ticket(request.user, radicado)
    if ticket.estado not in (E.GENERADO, E.RECEPCIONADO):
        messages.error(request, "Solo se puede cancelar una solicitud que aún no está en el cronograma.")
        return redirect(ticket)
    ticket.cambiar_estado(E.CANCELADO, request.user, request.POST.get("motivo", "").strip() or "Cancelado por el cliente.")
    messages.info(request, "La solicitud fue cancelada.")
    return redirect(ticket)


# ============================================================================
# Detalle (compartido) y descargas
# ============================================================================
@login_required
def ticket_detalle(request, radicado):
    cierre.revisar_si_toca()
    ticket = obtener_ticket(request.user, radicado)
    gestiona = gestiona_ticket(request.user, ticket)
    if request.user.es_comercial and hasattr(ticket, "ficha") and not gestiona:
        return redirect("comercial:detalle", radicado)  # el área comercial trabaja desde su propia vista
    seguimientos = ticket.seguimientos.select_related("autor").prefetch_related("adjuntos")
    adjuntos_inicio = ticket.adjuntos.filter(seguimiento__tipo=Seguimiento.Tipo.CREACION)
    historial = actas_visibles(request.user).filter(empresa=ticket.empresa)
    if ticket.sede_id:
        historial = historial.filter(sede=ticket.sede)
    contexto = {"ticket": ticket, "adjuntos_inicio": adjuntos_inicio, "historial_sede": historial.exclude(ticket=ticket).order_by("-fecha")[:5],
                "actas_ticket": actas_visibles(request.user).filter(ticket=ticket).order_by("-fecha"),
                "puede_acta": _puede_diligenciar_acta(request.user, ticket),
                "es_levantamiento": _es_levantamiento(ticket), "ficha": getattr(ticket, "ficha", None),
                "actividades": ticket.actividades.prefetch_related("encargados").order_by("-fecha"),
                "rangos": ticket.rangos_no_disponible.all(), "etapas": etapas.etapas_cliente(ticket),
                "visitas_previas": reincidencia.visitas_previas(ticket) if request.user.es_personal else [],
                "puede_informe": ticket.estado == E.INFORME and _puede_entregar_informe(request.user, ticket),
                "pasos_tecnica": pasos.calcular(ticket) if request.user.es_personal else None,
                "acta_bloqueada": not pasos.puede_hacer_visita(ticket)
                and _puede_diligenciar_acta(request.user, ticket, ignorar_pasos=True)}
    if contexto["puede_informe"] or contexto["es_levantamiento"] and _puede_entregar_informe(request.user, ticket):
        from . import informe as inf

        contexto["borrador"] = inf.vigente(ticket)
        contexto["tipo_sugerido"] = inf.tipo_sugerido(ticket)
    if contexto["es_levantamiento"]:
        from comercial.forms import InformeForm
        b = contexto.get("borrador")
        # El estudio aprobado llena solo el informe del requerimiento (se puede ajustar antes de enviarlo)
        contexto["form_informe"] = InformeForm(initial={
            "necesidad": ticket.asunto, "solucion": b.conclusiones, "servicios": b.servicios,
            "materiales": b.materiales_para_comercial()} if b and b.aprobado and b.es_estudio else None)
    contexto["puede_vincular"] = _puede_vincular_acta(request.user, ticket)
    pendiente = request.GET.get("vincular", "")
    if contexto["puede_vincular"] and pendiente.isdigit():
        acta = ActaSoporte.objects.select_related("empresa", "sede").filter(numero=pendiente, ticket__isnull=True).first()
        if acta:
            contexto["vincular"] = {"acta": acta, "avisos": diferencias_acta(acta, ticket)}

    if gestiona:
        contexto["seguimientos"] = seguimientos
        contexto["form_gestion"] = GestionForm(ticket=ticket, usuario=request.user)
        contexto["aplazamientos"] = ticket.aplazamientos.select_related("autor")
        if request.user.es_coordinador and ticket.estado in (E.EN_CRONOGRAMA, E.AUTORIZADO):
            contexto["form_aplazar"] = AplazarForm()
        contexto["otros_tickets"] = (
            tickets_visibles(request.user).filter(empresa=ticket.empresa).exclude(pk=ticket.pk)
            .order_by("-creado")[:5]
        )
        return render(request, "tickets/personal_detalle.html", contexto)

    contexto["seguimientos"] = seguimientos.filter(interno=False).exclude(
        tipo__in=[Seguimiento.Tipo.ASIGNACION, Seguimiento.Tipo.PRIORIDAD]
    )
    contexto["form_respuesta"] = RespuestaClienteForm()
    contexto["form_calificacion"] = CalificacionForm()
    contexto["escala_calificacion"] = range(Ticket.ESCALA_CALIFICACION, 0, -1)
    contexto["pasos"] = Ticket.PASOS_CLIENTE
    return render(request, "tickets/cliente_detalle.html", contexto)


@login_required
def descargar_adjunto(request, pk):
    """Los adjuntos nunca se sirven públicamente: se valida el acceso aquí."""
    adjunto = get_object_or_404(Adjunto.objects.select_related("ticket", "seguimiento"), pk=pk)
    obtener_ticket(request.user, adjunto.ticket.radicado)  # 404 si no puede verlo
    if adjunto.interno and not (request.user.es_personal or request.user.ve_comercial):
        raise Http404
    try:
        archivo = adjunto.archivo.open("rb")
    except FileNotFoundError:
        raise Http404("El archivo ya no está disponible.")
    en_linea = request.GET.get("ver") == "1"
    return FileResponse(archivo, as_attachment=not en_linea, filename=adjunto.nombre_original)


# ============================================================================
# Panel del área de soporte
# ============================================================================
@login_required
def salud_procesos(request):
    """Lo que la plataforma detectó sola en el uso real (solo el superusuario)."""
    if not request.user.is_superuser:
        raise PermissionDenied
    from .models import Hallazgo
    if request.method == "POST":
        nuevos = diagnostico.revisar()
        messages.success(request, f"Revisión hecha: {len(nuevos)} situación(es) nueva(s).")
        return redirect("salud_procesos")
    info = diagnostico.reglas()
    abiertos = list(diagnostico.pendientes().select_related("ticket", "usuario").order_by("creado"))
    grupos = []
    for clave, regla in info.items():
        casos = [h for h in abiertos if h.regla == clave]
        if casos:
            personas = {}
            for h in casos:
                if h.usuario:
                    nombre = h.usuario.get_full_name() or h.usuario.username
                    personas[nombre] = personas.get(nombre, 0) + 1
            grupos.append({"regla": regla, "casos": casos, "personas": sorted(personas.items(), key=lambda x: -x[1])})
    orden = {"alta": 0, "media": 1, "baja": 2}
    grupos.sort(key=lambda g: (orden[g["regla"].gravedad], -len(g["casos"])))
    hace_30 = timezone.now() - timedelta(days=30)
    resueltos = Hallazgo.objects.filter(resuelto__gte=hace_30).values("regla").annotate(n=Count("id")).order_by("-n")
    return render(request, "tickets/salud_procesos.html", {
        "grupos": grupos, "total": len(abiertos), "altas": sum(1 for h in abiertos if h.gravedad == "alta"),
        "resueltos": [(info[r["regla"]].nombre if r["regla"] in info else r["regla"], r["n"]) for r in resueltos],
        "sin_problemas": [r for c, r in info.items() if not any(h.regla == c for h in abiertos)],
        "revisados": Hallazgo.objects.filter(resuelto__isnull=True, revisado_en__isnull=False)
                                     .select_related("ticket", "revisado_por").order_by("-revisado_en"),
    })


@login_required
@require_POST
def hallazgo_revisar(request, pk):
    """Marcar un caso de «Salud de los procesos» como revisado (con el motivo), o reabrirlo (solo el superusuario)."""
    from .models import Hallazgo

    h = get_object_or_404(Hallazgo, pk=pk)
    if request.POST.get("accion") == "reabrir":
        if not request.user.is_superuser:
            raise PermissionDenied
        h.revisado_en, h.revisado_por, h.motivo_revision = None, None, ""
        h.save(update_fields=["revisado_en", "revisado_por", "motivo_revision"])
        messages.info(request, "El caso volvió a la lista.")
    else:
        try:
            diagnostico.marcar_revisado(h, request.user, request.POST.get("motivo", ""))
        except ValidationError as e:
            messages.error(request, " ".join(e.messages))
        else:
            messages.success(request, "Caso revisado: sale de la lista con su motivo.")
    volver = request.POST.get("volver", "")
    return redirect(volver if volver.startswith("/") and not volver.startswith("//") else "salud_procesos")


@login_required
@personal_requerido
def panel(request):
    cierre.revisar_si_toca()
    diagnostico.revisar_si_toca()
    from inventario import alertas
    alertas.revisar_si_toca()
    todos = tickets_visibles(request.user).filter(area=Ticket.Area.TECNICA)  # la bandeja técnica
    datos = request.GET.copy()
    if not request.GET:
        # El técnico arranca en lo que tiene programado y aún no ha atendido, la próxima visita primero
        datos["estado"] = "abiertos" if request.user.es_supervisor else "por_atender"
    form = FiltroTicketsForm(datos, para_personal=True, supervisor=request.user.es_supervisor)
    qs = _filtrar(todos, form, request.user).select_related("ficha")
    if request.GET.get("aplazados") == "1":
        qs = todos.filter(veces_aplazado__gt=0, estado__in=[E.GENERADO, *Ticket.ESTADOS_PROGRAMABLES]).select_related("ficha")

    orden = request.GET.get("orden") or ("visita" if datos.get("estado") == "por_atender" else "-creado")
    if orden not in ("-creado", "creado", "-actualizado", "prioridad", "visita"):
        orden = "-creado"
    if orden == "prioridad":
        from django.db.models import Case, IntegerField, When
        peso = Case(
            When(prioridad="critica", then=0), When(prioridad="alta", then=1),
            When(prioridad="media", then=2), default=3, output_field=IntegerField(),
        )
        qs = qs.annotate(peso=peso).order_by("peso", "-creado", "-id")  # dentro de cada nivel, el más reciente arriba
    elif orden == "visita":
        from django.db.models import F
        qs = qs.order_by(F("visita_programada").asc(nulls_last=True), "-id")
    else:
        qs = qs.order_by(orden, "-id")

    # Se cuenta sobre los ids: la vista del técnico une sus visitas del cronograma y repetiría tickets al agrupar
    conteo = dict(Ticket.objects.filter(pk__in=todos.values("pk")).values_list("estado").annotate(n=Count("id")))
    hoy = timezone.localdate()
    indicadores = {
        "generados": conteo.get(E.GENERADO, 0),
        "por_programar": conteo.get(E.RECEPCIONADO, 0) + conteo.get(E.PENDIENTE_VISITA, 0),
        "criticos": todos.filter(prioridad__in=["critica", "alta"], estado__in=Ticket.ESTADOS_ABIERTOS).count(),
        "programados": conteo.get(E.EN_CRONOGRAMA, 0) + conteo.get(E.AUTORIZADO, 0),
        "por_atender": sum(conteo.get(e, 0) for e in ESTADOS_POR_ATENDER),
        "hoy": todos.filter(creado__gte=_inicio_del_dia(hoy), creado__lt=_inicio_del_dia(hoy + timedelta(days=1))).count(),
        "abiertos": sum(conteo.get(e, 0) for e in Ticket.ESTADOS_ABIERTOS),
        "aplazados": todos.filter(veces_aplazado__gt=0, estado__in=[E.GENERADO, *Ticket.ESTADOS_PROGRAMABLES]).count(),
    }
    pagina = Paginator(qs, 20).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "tickets/personal_panel.html",
        {"pagina": pagina, "form": form, "ind": indicadores, "orden": orden, "params": params.urlencode()},
    )


@login_required
@require_POST
def gestionar(request, radicado):
    ticket = obtener_ticket(request.user, radicado)
    if not gestiona_ticket(request.user, ticket):
        raise PermissionDenied
    form = GestionForm(request.POST, request.FILES, ticket=ticket, usuario=request.user)
    if not form.is_valid():
        for campo, errores in form.errors.items():
            for e in errores:
                messages.error(request, e)
        return redirect(ticket)

    d = form.cleaned_data
    d.setdefault("asignado_a", ticket.asignado_a)  # campos que solo trae el formulario del supervisor
    d.setdefault("prioridad", ticket.prioridad)
    interno = d["visibilidad"] == "interno"
    mensaje = (d["mensaje"] or "").strip()
    seg_visible = None
    cambio_asignacion = False

    with transaction.atomic():
        # 1) Responsable
        if d["asignado_a"] != ticket.asignado_a:
            ticket.asignado_a = d["asignado_a"]
            cambio_asignacion = d["asignado_a"] is not None
            ticket.save(update_fields=["asignado_a", "actualizado"])
            Seguimiento.objects.create(
                ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.ASIGNACION, interno=True,
                mensaje=f"Asignado a {d['asignado_a']}." if d["asignado_a"] else "Se quitó el responsable.",
            )

        # 2) Prioridad
        if d["prioridad"] != ticket.prioridad:
            anterior = ticket.get_prioridad_display()
            ticket.prioridad = d["prioridad"]
            ticket.save(update_fields=["prioridad", "actualizado"])
            Seguimiento.objects.create(
                ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.PRIORIDAD, interno=True,
                mensaje=f"Prioridad: {anterior} → {ticket.get_prioridad_display()}.",
            )

        # 3) Estado (el mensaje acompaña al cambio de estado)
        seg_estado = None
        traspaso = d["estado"] == E.EN_PROCESO and ticket.estado != E.EN_PROCESO and ticket.area == Ticket.Area.TECNICA
        if traspaso:
            # Pasa al área comercial de verdad: ficha comercial e informe del requerimiento con lo que escribió
            from comercial.servicios import pasar_a_propuesta
            pasar_a_propuesta(ticket, request.user, mensaje or ticket.descripcion)
            messages.info(request, f"{ticket.radicado} pasó al área comercial para la propuesta.")
        elif d["estado"] != ticket.estado:
            seg_estado = ticket.cambiar_estado(
                d["estado"], request.user, mensaje if not interno else "", interno=False
            )
            if not interno:
                seg_visible = seg_estado

        # 4) Mensaje suelto (sin cambio de estado) o nota interna
        seg_mensaje = None
        if mensaje and (interno or seg_estado is None) and not traspaso:  # en el traspaso el texto va al informe
            seg_mensaje = Seguimiento.objects.create(
                ticket=ticket, autor=request.user, mensaje=mensaje, interno=interno,
                tipo=Seguimiento.Tipo.NOTA_INTERNA if interno else Seguimiento.Tipo.COMENTARIO,
            )
            if not interno:
                seg_visible = seg_mensaje

        destino_adjuntos = seg_mensaje or seg_estado
        if d["archivos"]:
            if destino_adjuntos is None:
                destino_adjuntos = Seguimiento.objects.create(
                    ticket=ticket, autor=request.user, interno=interno,
                    tipo=Seguimiento.Tipo.NOTA_INTERNA if interno else Seguimiento.Tipo.COMENTARIO,
                    mensaje="Adjuntó archivos.",
                )
                if not interno:
                    seg_visible = destino_adjuntos
            _guardar_adjuntos(ticket, d["archivos"], request.user, destino_adjuntos)


    if cambio_asignacion:
        notificaciones.asignacion(ticket, request.user, mensaje)
    else:
        notificaciones.mensaje_en_ticket(ticket, seg_mensaje or (seg_estado if mensaje else None))
    notificaciones.actualizacion_para_cliente(ticket, seg_visible)
    messages.success(request, "Ticket actualizado.")
    if ticket.estado == E.CERRADO:
        from inventario.solicitudes import resumen_pendiente
        pendiente = resumen_pendiente(ticket)
        if pendiente:  # no se bloquea el cierre, pero el material no se puede perder de vista
            messages.warning(request, f"Atención: quedó material sin cuadrar en {ticket.radicado}: {pendiente}. El técnico debe "
                                      "descontarlo en el acta de servicio o devolverlo a bodega indicando este ticket.")
    if traspaso and not tickets_visibles(request.user).filter(pk=ticket.pk).exists():
        return redirect("panel")  # pasó al área comercial: el coordinador técnico ya no lo ve en su área
    return redirect(ticket)


@login_required
@supervisor_requerido
def reportes(request):
    """Indicadores de gestión del periodo (para el informe de cierre) y el estado actual de las solicitudes."""
    from . import indicadores

    tickets = tickets_visibles(request.user)  # coordinador técnico: su área; dirección: todo
    clave, desde, hasta, etiqueta = indicadores.periodo(request.GET)
    ve_comercial = request.user.ve_comercial
    resultado = indicadores.calcular(tickets, desde, hasta, ve_comercial)
    distribucion = resultado["valores"]["distribucion"]
    mayor = max(distribucion.values()) or 1
    por_estado = [(label, tickets.filter(estado=valor).count(), valor) for valor, label in Ticket.Estado.choices]
    return render(request, "tickets/personal_reportes.html", {
        "periodo": clave, "desde": desde, "hasta": hasta, "etiqueta": etiqueta, "periodos": indicadores.PERIODOS,
        "indicadores": resultado["indicadores"], "v": resultado["valores"], "ve_comercial": ve_comercial,
        "distribucion": [(n, c, round(100 * c / mayor)) for n, c in distribucion.items()],
        "por_responsable": indicadores.por_responsable(tickets, desde, hasta),
        "por_cliente": indicadores.por_cliente(tickets, desde, hasta),
        "por_tipo": indicadores.por_tipo(tickets, desde, hasta),
        "por_falla": (tickets.filter(creado__gte=_inicio_del_dia(desde), creado__lt=_inicio_del_dia(hasta + timedelta(days=1)))
                      .exclude(falla=None).values("falla__nombre", "categoria__nombre").annotate(n=Count("id")).order_by("-n")[:8]),
        "por_estado": [e for e in por_estado if e[1]], "generado": timezone.localtime(),
    })


# ============================================================================
# Historial de servicio (actas de soporte en campo)
# ============================================================================
@login_required
def actas_lista(request):
    base = actas_visibles(request.user)
    form = FiltroActasForm(request.GET or None, actas=base, mostrar_empresa=request.user.es_personal)
    qs = base
    if form.is_valid():
        d = form.cleaned_data
        if d.get("q"):
            q = d["q"].strip()
            filtro = (Q(diagnostico__icontains=q) | Q(observaciones__icontains=q) | Q(sede_texto__icontains=q)
                      | Q(tecnico_texto__icontains=q) | Q(equipos__equipo__icontains=q) | Q(equipos__serial__icontains=q))
            if q.isdigit():
                filtro |= Q(numero=int(q))
            qs = qs.filter(filtro).distinct()
        if d.get("empresa"):
            qs = qs.filter(empresa=d["empresa"])
        if d.get("sede"):
            qs = qs.filter(sede=d["sede"])
        if d.get("sistema"):
            qs = qs.filter(sistema=d["sistema"])
        if d.get("resultado"):
            qs = qs.filter(resultado=d["resultado"])
        if d.get("anio"):
            anio = int(d["anio"])
            qs = qs.filter(fecha__gte=_inicio_del_dia(date(anio, 1, 1)), fecha__lt=_inicio_del_dia(date(anio + 1, 1, 1)))
    resumen = {
        "total": qs.count(),
        "sedes": qs.exclude(sede=None).values("sede").distinct().count(),
        "pendientes": qs.exclude(resultado=ActaSoporte.Resultado.SOLUCIONADO).count(),
    }
    pagina = Paginator(qs.order_by("-fecha"), 25).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "tickets/actas_lista.html",
                  {"pagina": pagina, "form": form, "resumen": resumen, "params": params.urlencode()})


@login_required
def acta_detalle(request, numero):
    acta = get_object_or_404(actas_visibles(request.user).prefetch_related("equipos"), numero=numero)
    anteriores = actas_visibles(request.user).filter(empresa=acta.empresa).exclude(pk=acta.pk)
    anteriores = anteriores.filter(sede=acta.sede) if acta.sede_id else anteriores.filter(sede_texto=acta.sede_texto)
    from inventario.models import MovimientoActivo, MovimientoInsumo
    from .forms import EvidenciasActaForm
    return render(request, "tickets/acta_detalle.html", {
        "acta": acta, "anteriores": anteriores.order_by("-fecha")[:6],
        "evidencias": acta.evidencias.select_related("subido_por"),
        "form_evidencias": EvidenciasActaForm() if _puede_subir_evidencias(request.user, acta) else None,
        "material_activos": MovimientoActivo.objects.filter(acta=acta).select_related("activo"),
        "material_insumos": MovimientoInsumo.objects.filter(acta=acta).select_related("insumo"),
    })


def _puede_subir_evidencias(usuario, acta):
    return usuario.es_supervisor or (acta.tecnico_id is not None and acta.tecnico_id == usuario.pk)


@login_required
@require_POST
def acta_evidencias(request, numero):
    """El técnico del acta (o coordinación) agrega evidencias que no alcanzó a subir en el sitio."""
    from .forms import EvidenciasActaForm

    acta = get_object_or_404(actas_visibles(request.user), numero=numero)
    if not _puede_subir_evidencias(request.user, acta):
        raise PermissionDenied
    form = EvidenciasActaForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, " ".join(e for errores in form.errors.values() for e in errores))
        return redirect(reverse("acta_detalle", args=[acta.numero]) + "#evidencias")
    for archivo in form.cleaned_data["archivos"]:
        EvidenciaActa.objects.create(acta=acta, etapa=form.cleaned_data["etapa"], archivo=archivo,
                                     nombre_original=archivo.name[:255], subido_por=request.user)
    if acta.ticket_id:
        Seguimiento.objects.create(ticket=acta.ticket, autor=request.user, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                                   mensaje=f"Agregó {len(form.cleaned_data['archivos'])} evidencia(s) al acta N° {acta.numero}.")
    messages.success(request, "Evidencias agregadas al acta.")
    return redirect(reverse("acta_detalle", args=[acta.numero]) + "#evidencias")


# ============================================================================
# Acta de soporte diligenciada en la plataforma (reemplaza el acta en papel/SafetyTrack)
# ============================================================================
def _es_levantamiento(ticket):
    """Ticket del área comercial que está en el área técnica solo para una visita de levantamiento."""
    ficha = getattr(ticket, "ficha", None)
    return bool(ficha and ficha.etapa == "levantamiento" and ticket.area == Ticket.Area.TECNICA)


def _puede_diligenciar_acta(usuario, ticket, ignorar_pasos=False):
    """Los técnicos del ticket (o el área técnica) desde que se recepciona hasta que se cierra.

    Paso a paso: se habilita cuando el ticket tiene técnico y la visita está programada (o coordinación omitió esos pasos).
    """
    if not ignorar_pasos and not pasos.puede_hacer_visita(ticket):
        return False
    en_atencion = ticket.estado in Ticket.ESTADOS_EN_ATENCION or (
        ticket.area == Ticket.Area.TECNICA and ticket.abierto and ticket.estado != E.GENERADO)
    if not (usuario.es_personal and en_atencion):
        return False
    if usuario.es_supervisor or ticket.asignado_a_id == usuario.pk:
        return True
    return ticket.actividades.filter(encargados=usuario).exists()


def _puede_vincular_acta(usuario, ticket):
    return usuario.es_supervisor or (usuario.es_personal and ticket.asignado_a_id == usuario.pk)


def diferencias_acta(acta, ticket):
    """Lo que no coincide entre el acta y el ticket (se advierte antes de vincular)."""
    avisos = []
    if acta.empresa_id != ticket.empresa_id:
        avisos.append(f"El acta es de «{acta.empresa}» y el ticket de «{ticket.empresa}».")
    elif acta.sede_id and ticket.sede_id and acta.sede_id != ticket.sede_id:
        avisos.append(f"El acta es de la sede «{acta.sede}» y el ticket de «{ticket.sede}».")
    if timezone.localtime(acta.fecha).date() < timezone.localtime(ticket.creado).date():
        avisos.append(f"El acta es del {timezone.localtime(acta.fecha):%d/%m/%Y}, antes de que se radicara el ticket "
                      f"({timezone.localtime(ticket.creado):%d/%m/%Y}).")
    return avisos


@login_required
@personal_requerido
@require_POST
def vincular_acta(request, radicado):
    """Asocia al ticket un acta que ya existe (p. ej. una histórica de SafetyTrack). No cambia el estado del ticket."""
    ticket = obtener_ticket(request.user, radicado)
    if not _puede_vincular_acta(request.user, ticket):
        raise PermissionDenied
    volver = reverse("ticket_detalle", args=[ticket.radicado])
    numero = request.POST.get("numero", "").strip()
    acta = ActaSoporte.objects.select_related("empresa", "sede", "ticket").filter(numero=numero).first() if numero.isdigit() else None
    if acta is None:
        messages.error(request, f"No existe el acta N.º {numero or '—'}.")
        return redirect(volver + "#actas")
    if acta.ticket_id == ticket.pk:
        messages.info(request, f"El acta {acta.numero} ya está vinculada a este ticket.")
        return redirect(volver + "#actas")
    if acta.ticket_id:
        messages.error(request, f"El acta {acta.numero} ya está vinculada a {acta.ticket.radicado}.")
        return redirect(volver + "#actas")
    avisos = diferencias_acta(acta, ticket)
    if avisos and request.POST.get("confirmar") != "1":
        return redirect(f"{volver}?vincular={acta.numero}#actas")  # se muestra lo que no coincide y se pide confirmar
    acta.ticket = ticket
    acta.save(update_fields=["ticket"])
    Seguimiento.objects.create(
        ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
        mensaje=f"Se vinculó el acta N.º {acta.numero} ({acta.get_origen_display()}, {timezone.localtime(acta.fecha):%d/%m/%Y}, "
                f"{acta.tecnico_texto}) a este ticket." + (" Diferencias confirmadas: " + " ".join(avisos) if avisos else ""))
    ticket.save(update_fields=["actualizado"])
    messages.success(request, f"Acta {acta.numero} vinculada a {ticket.radicado}.")
    return redirect(volver + "#actas")


@login_required
@supervisor_requerido
@require_POST
def desvincular_acta(request, radicado, numero):
    """Solo las actas que se vincularon (históricas): las diligenciadas en el ticket no se desvinculan."""
    ticket = obtener_ticket(request.user, radicado)
    acta = get_object_or_404(ActaSoporte, numero=numero, ticket=ticket, origen=ActaSoporte.Origen.SAFETYTRACK)
    acta.ticket = None
    acta.save(update_fields=["ticket"])
    Seguimiento.objects.create(ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                               mensaje=f"Se desvinculó el acta N.º {acta.numero} de este ticket.")
    messages.info(request, f"Acta {acta.numero} desvinculada de {ticket.radicado}.")
    return redirect(reverse("ticket_detalle", args=[ticket.radicado]) + "#actas")


@login_required
@personal_requerido
def acta_nueva(request, radicado):
    ticket = obtener_ticket(request.user, radicado)
    if not _puede_diligenciar_acta(request.user, ticket):
        if _puede_diligenciar_acta(request.user, ticket, ignorar_pasos=True):
            messages.error(request, "Paso a paso: el acta se habilita cuando el ticket tiene técnico y la visita está "
                                    "programada en el cronograma (o coordinación omite esos pasos con su motivo).")
        else:
            messages.error(request, "El acta la diligencia el encargado del ticket mientras está en atención.")
        return redirect(ticket)
    return _diligenciar_acta(request, ticket.empresa, ticket.sede, ticket)


@login_required
@personal_requerido
def acta_sin_programar(request):
    """Acta de un trabajo que no estaba en el cronograma (p. ej. en otra visita el cliente pide un trabajo pequeño
    y se resuelve en el momento). Primero se elige el cliente y la sede; el acta queda sin ticket."""
    empresas = Empresa.objects.filter(activa=True).order_by("razon_social")
    empresa = sede = None
    if request.GET.get("empresa", "").isdigit():
        empresa = empresas.filter(pk=request.GET["empresa"]).first()
    if empresa and request.GET.get("sede", "").isdigit():
        sede = empresa.sedes.filter(pk=request.GET["sede"], activa=True).first()
    if empresa and (sede or request.GET.get("sede") == "0"):
        return _diligenciar_acta(request, empresa, sede)
    return render(request, "tickets/acta_elegir_cliente.html", {
        "empresas": empresas, "empresa": empresa,
        "sedes": empresa.sedes.filter(activa=True).order_by("nombre") if empresa else [],
    })


def _diligenciar_acta(request, empresa, sede, ticket=None):
    if request.method == "POST":
        form = ActaForm(request.POST, request.FILES)
        equipos = EquipoActaFormSet(request.POST, request.FILES, prefix="equipos")
        # Sin la lista de utilizados en el envío (formulario anterior) no hay filas que guardar
        utilizados = (EquipoActaFormSet(request.POST, request.FILES, prefix="utilizados", form_kwargs={"clase": "utilizado"})
                      if "utilizados-TOTAL_FORMS" in request.POST else EquipoActaFormSet(prefix="utilizados", form_kwargs={"clase": "utilizado"}))
        retirados = (EquipoActaFormSet(request.POST, request.FILES, prefix="retirados", form_kwargs={"clase": "retirado"})
                     if "retirados-TOTAL_FORMS" in request.POST else EquipoActaFormSet(prefix="retirados", form_kwargs={"clase": "retirado"}))
    elif ticket is None:
        correos = [c for c in (empresa.email_contacto, settings.NOTIFICAR_SOPORTE_EMAIL) if c]
        form = ActaForm(initial={
            "ciudad": (sede.ciudad if sede and sede.ciudad else empresa.ciudad), "actividad": ["Soporte"],
            "cargo_tecnico": request.user.cargo, "copia_correo": " ; ".join(dict.fromkeys(correos)),
        })
        equipos = EquipoActaFormSet(prefix="equipos")
        utilizados = EquipoActaFormSet(prefix="utilizados", form_kwargs={"clase": "utilizado"})
        retirados = EquipoActaFormSet(prefix="retirados", form_kwargs={"clase": "retirado"})
    else:
        correos = ticket.correos_respuesta + ([settings.NOTIFICAR_SOPORTE_EMAIL] if settings.NOTIFICAR_SOPORTE_EMAIL else [])
        form = ActaForm(initial={
            "ciudad": (ticket.sede.ciudad if ticket.sede and ticket.sede.ciudad else ticket.empresa.ciudad),
            "actividad": ["Instalación"] if ticket.categoria.nombre.startswith("Instalación") else ["Soporte"],
            "sistema": reincidencia.SISTEMA_POR_SERVICIO.get(ticket.categoria.nombre, "CCTV"),
            "diagnostico": ticket.falla_display,
            "recibio": ticket.contacto_nombre,
            "cargo_tecnico": request.user.cargo,
            "copia_correo": " ; ".join(dict.fromkeys(correos)),  # sin repetir (el del ticket puede ser el de soporte)
            "marcar_resuelto": True,
        })
        equipos = EquipoActaFormSet(prefix="equipos")
        utilizados = EquipoActaFormSet(prefix="utilizados", form_kwargs={"clase": "utilizado"})
        retirados = EquipoActaFormSet(prefix="retirados", form_kwargs={"clase": "retirado"})
    if ticket is None:
        del form.fields["marcar_resuelto"]  # sin ticket no hay visita que avanzar

    # Material del inventario en poder del técnico: lo que marque se descuenta con el acta
    from inventario import servicios as inventario
    mat_activos, mat_insumos = inventario.en_poder_de(request.user)
    material_errores, usar_activos, usar_insumos, usados = [], [], [], {}
    if request.method == "POST":
        elegidos = set(request.POST.getlist("usar_activo"))
        usar_activos = [a for a in mat_activos if str(a.pk) in elegidos]
        for insumo, tiene in mat_insumos:
            texto = request.POST.get(f"usar_insumo_{insumo.pk}", "").replace(",", ".").strip()
            if not texto:
                continue
            usados[insumo.pk] = texto
            try:
                cantidad = Decimal(texto)
            except InvalidOperation:
                cantidad = Decimal(-1)
            if cantidad < 0 or cantidad > tiene:
                material_errores.append(f"{insumo.codigo}: tiene {tiene.normalize():f} y escribió {texto}.")
            elif cantidad > 0:
                usar_insumos.append((insumo, cantidad))
    else:
        elegidos = {str(a.pk) for a in mat_activos if ticket and a.ticket_destino_id == ticket.pk}

    if (request.method == "POST" and form.is_valid() and equipos.is_valid()
            and (not utilizados.is_bound or utilizados.is_valid()) and (not retirados.is_bound or retirados.is_valid())
            and not material_errores):
        d = form.cleaned_data
        with transaction.atomic():
            acta = ActaSoporte.objects.create(
                numero=ActaSoporte.siguiente_numero(), fecha=timezone.now(), origen=ActaSoporte.Origen.PLATAFORMA,
                ticket=ticket, empresa=empresa, sede=sede, sede_texto=str(sede) if sede else "", ciudad=d["ciudad"],
                tecnico=request.user, tecnico_texto=request.user.get_full_name() or request.user.username,
                actividad=" , ".join(d["actividad"]), sistema=d["sistema"], modalidad=d["modalidad"],
                diagnostico=d["diagnostico"], resultado=d["resultado"], observaciones=d["observaciones"],
                recibio=d["recibio"], cargo_recibio=d["cargo_recibio"], copia_correo=d["copia_correo"],
                latitud=d.get("latitud"), longitud=d.get("longitud"),
            )
            # «Se retiró» dentro de los intervenidos: queda como equipo retirado (y si es de JANOTEC vuelve a bodega)
            filas = [(f, EquipoActa.Clase.RETIRADO if f.has_changed() and f.es_retiro else EquipoActa.Clase.INTERVENIDO)
                     for f in equipos]
            if utilizados.is_bound:
                filas += [(f, EquipoActa.Clase.UTILIZADO) for f in utilizados]
            if retirados.is_bound:
                filas += [(f, EquipoActa.Clase.RETIRADO) for f in retirados]
            for f, clase in filas:
                if not f.has_changed() or not f.cleaned_data.get("equipo"):
                    continue
                e = f.cleaned_data
                equipo = EquipoActa.objects.create(acta=acta, clase=clase, equipo=e["equipo"], marca=e["marca"],
                                                   referencia=e.get("referencia") or "", serial=e["serial"], cantidad=e["cantidad"], observaciones=e["observaciones"],
                                                   propiedad=e.get("propiedad") or "", placa=(e.get("placa") or "").strip().upper(),
                                                   trabajo=e.get("accion") if e.get("accion") in EquipoActa.Trabajo.values else "")
                for foto in e["fotos"]:
                    FotoEquipo.objects.create(equipo=equipo, archivo=foto, nombre_original=foto.name[:255])
            for etapa in EvidenciaActa.Etapa.values:
                for archivo in d.get(f"evidencias_{etapa}") or []:
                    EvidenciaActa.objects.create(acta=acta, etapa=etapa, archivo=archivo, nombre_original=archivo.name[:255],
                                                 subido_por=request.user)
            inventario.usar_en_acta(acta, usar_activos, usar_insumos, request.user)
            inventario.retirar_en_acta(acta, request.user)
            nombre_tecnico = request.user.get_full_name() or request.user.username
            FirmaActa.objects.create(acta=acta, rol=FirmaActa.Rol.TECNICO, nombre=nombre_tecnico,
                                     cargo=d["cargo_tecnico"], imagen=d["firma_tecnico"])
            if d.get("firma_acompanante"):
                FirmaActa.objects.create(acta=acta, rol=FirmaActa.Rol.ACOMPANANTE, nombre=d["acompanante_nombre"],
                                         cargo=d["acompanante_cargo"], imagen=d["firma_acompanante"])
            if d.get("firma_cliente"):
                FirmaActa.objects.create(acta=acta, rol=FirmaActa.Rol.CLIENTE, nombre=d["recibio"],
                                         cargo=d["cargo_recibio"], imagen=d["firma_cliente"])
            oportunidad = None
            if d.get("oportunidad_servicios") or (d.get("oportunidad_nota") or "").strip():
                oportunidad = OportunidadComercial.objects.create(
                    acta=acta, empresa=empresa, sede=sede, detectada_por=request.user,
                    servicios=d.get("oportunidad_servicios") or [], nota=(d.get("oportunidad_nota") or "").strip(),
                )

            mensaje = (f"Se diligenció el acta de soporte N° {acta.numero} · {acta.actividad} de {acta.get_sistema_display_corto()}"
                       f" · Atención {acta.get_modalidad_display().lower()} · Resultado: {acta.get_resultado_display()}.\n{acta.observaciones}")
            seg = None
            # En un levantamiento comercial «terminar» no es resolver: el ticket vuelve al área comercial
            levantamiento = ticket is not None and _es_levantamiento(ticket)
            if ticket is None:
                pass  # trabajo no programado: el acta queda en el historial de la sede, sin ticket
            elif d.get("marcar_resuelto") and not levantamiento and ticket.estado != E.VISITA_EJECUTADA:
                # Solucionado: el cliente confirma (o se cierra solo). Si no, queda pendiente de nueva visita.
                nuevo = E.VISITA_EJECUTADA if acta.resultado == ActaSoporte.Resultado.SOLUCIONADO else E.PENDIENTE_VISITA
                if etapas.lleva_informe(ticket) and acta.resultado != ActaSoporte.Resultado.PARCIAL:
                    nuevo = E.INFORME  # toma de requerimientos o diagnóstico: falta el informe técnico
                seg = ticket.cambiar_estado(nuevo, request.user, mensaje)
            else:
                seg = Seguimiento.objects.create(ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.COMENTARIO,
                                                 mensaje=mensaje)
                ticket.save(update_fields=["actualizado"])
        notificaciones.actualizacion_para_cliente(ticket, seg)
        notificaciones.acta_registrada(acta)
        if oportunidad:
            notificaciones.oportunidad_detectada(oportunidad)
        messages.success(request, f"Acta de soporte N° {acta.numero} registrada."
                         + (f" Se descontaron del inventario {len(usar_activos)} activo(s) y {len(usar_insumos)} insumo(s)."
                            if usar_activos or usar_insumos else "")
                         + (" Gracias por reportar la oportunidad comercial." if oportunidad else ""))
        if levantamiento:
            # El acta es la constancia de la visita; lo que cotiza comercial es el informe del requerimiento
            messages.info(request, "Ahora diligencie el informe del requerimiento para devolverlo al área comercial.")
            return redirect(reverse("ticket_detalle", args=[ticket.radicado]) + "#informe-requerimiento")
        return redirect(acta)

    return render(request, "tickets/acta_form.html", {
        "ticket": ticket, "empresa": empresa, "sede": sede,
        "form": form, "equipos": equipos, "utilizados": utilizados, "retirados": retirados, "mat_activos": mat_activos,
        "mat_insumos": [(i, c, usados.get(i.pk, "")) for i, c in mat_insumos],
        "elegidos": {int(pk) for pk in elegidos if str(pk).isdigit()}, "material_errores": material_errores,
    })


@login_required
def archivo_acta(request, tipo, pk):
    """Fotos, firmas y PDF original de las actas: nunca públicos, se valida el acceso al acta."""
    if tipo == "original":
        acta = get_object_or_404(actas_visibles(request.user), pk=pk)
        if not acta.pdf_original:
            raise Http404
        try:
            return FileResponse(acta.pdf_original.open("rb"), as_attachment=False, content_type="application/pdf",
                                filename=f"Acta_{acta.numero}.pdf")
        except FileNotFoundError:
            raise Http404("El archivo ya no está disponible.")
    modelo = {"foto": FotoEquipo, "firma": FirmaActa, "evidencia": EvidenciaActa}.get(tipo)
    if modelo is None:
        raise Http404
    objeto = get_object_or_404(modelo, pk=pk)
    acta = objeto.equipo.acta if tipo == "foto" else objeto.acta
    get_object_or_404(actas_visibles(request.user), pk=acta.pk)
    campo = objeto.imagen if tipo == "firma" else objeto.archivo
    try:
        return FileResponse(campo.open("rb"), as_attachment=False)
    except FileNotFoundError:
        raise Http404("El archivo ya no está disponible.")


# ============================================================================
# Oportunidades comerciales detectadas en campo (solo área administrativa)
# ============================================================================
@login_required
@comercial_o_supervisor
def oportunidades(request):
    qs = OportunidadComercial.objects.select_related("empresa", "sede", "detectada_por", "acta", "acta__ticket")
    estado = request.GET.get("estado", "abiertas")
    if estado == "abiertas":
        qs = qs.exclude(estado__in=[OportunidadComercial.Estado.GANADA, OportunidadComercial.Estado.DESCARTADA])
    elif estado in OportunidadComercial.Estado.values:
        qs = qs.filter(estado=estado)
    servicio = request.GET.get("servicio", "")
    if servicio in dict(OportunidadComercial.SERVICIOS):
        # Filtro en Python: los servicios se guardan como lista JSON
        qs = [o for o in qs if servicio in o.servicios]
    conteo = dict(OportunidadComercial.objects.values_list("estado").annotate(n=Count("id")))
    pagina = Paginator(qs, 20).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "tickets/oportunidades.html", {
        "pagina": pagina, "estado": estado, "servicio": servicio, "conteo": conteo, "params": params.urlencode(),
        "estados": OportunidadComercial.Estado.choices, "servicios": OportunidadComercial.SERVICIOS,
    })


@login_required
@comercial_o_supervisor
@require_POST
def oportunidad_actualizar(request, pk):
    o = get_object_or_404(OportunidadComercial, pk=pk)
    nuevo = request.POST.get("estado")
    nota = request.POST.get("nota", "").strip()
    if nuevo in OportunidadComercial.Estado.values:
        o.estado = nuevo
    if nota:
        fecha = timezone.localtime().strftime("%d/%m/%Y %H:%M")
        quien = request.user.get_full_name() or request.user.username
        o.seguimiento = (o.seguimiento + "\n" if o.seguimiento else "") + f"[{fecha} · {quien}] {nota}"
    o.save()
    messages.success(request, f"Oportunidad de {o.empresa} actualizada: {o.get_estado_display()}.")
    volver = request.POST.get("volver", "")
    if not url_has_allowed_host_and_scheme(volver, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        volver = "oportunidades"  # solo se vuelve a páginas de la propia plataforma
    return redirect(volver or "oportunidades")


# ============================================================================
# Paso 2: recepción y verificación (área técnica)
# ============================================================================
def _recepcionar(ticket, usuario):
    seg = ticket.cambiar_estado(
        E.RECEPCIONADO, usuario,
        "Recibimos su solicitud: estamos validando el requerimiento y pronto la asignaremos al cronograma de visitas.",
    )
    notificaciones.actualizacion_para_cliente(ticket, seg)


@login_required
@require_POST
def inventario_listo(request, radicado):
    """Coordinación vio el avance del alistamiento y decide: el ticket pasa de «En inventario» a «Recepcionado»."""
    from django.core.exceptions import PermissionDenied

    from inventario import solicitudes as sol

    if not request.user.es_coordinador:
        raise PermissionDenied
    ticket = obtener_ticket(request.user, radicado)
    if ticket.estado != E.EN_INVENTARIO:
        messages.info(request, f"{ticket.radicado} ya no está «En inventario».")
        return redirect(ticket)
    listas = [sol.lista_alistamiento(s) for s in ticket.solicitudes_material.exclude(estado="cancelada")]
    avance = "; ".join(f"{l['solicitud'].numero}: {l['listos']} de {l['total']} listos ({l['solicitud'].get_estado_display().lower()})"
                       for l in listas) or "sin lista de material"
    nota = request.POST.get("nota", "").strip()
    ticket.reingresar(request.user, f"Coordinación revisó el alistamiento en inventario ({avance}) y pasa la instalación "
                                    "a «Recepcionado» para programarla." + (f"\n{nota}" if nota else ""))
    messages.success(request, f"{ticket.radicado} pasó a «Recepcionado»: ya puede programar la instalación en el cronograma.")
    return redirect(ticket)


@login_required
@supervisor_requerido
@require_POST
def recepcionar(request, radicado):
    """El ticket queda en validación del requerimiento, listo para pasar al cronograma."""
    ticket = obtener_ticket(request.user, radicado)
    if ticket.estado == E.GENERADO and not ticket.impacto:
        messages.warning(request, f"Antes de recepcionar {ticket.radicado}, defina el nivel de urgencia (baja, media o alta).")
        return redirect(reverse("editar_ticket", args=[ticket.radicado]) + "?recepcionar=1")
    if ticket.estado == E.GENERADO and ticket.por_confirmar:
        messages.warning(request, f"Antes de recepcionar {ticket.radicado}, confirme lo que el cliente no supo: "
                                  + ", ".join(ticket.por_confirmar_display).lower() + ".")
        return redirect(reverse("editar_ticket", args=[ticket.radicado]) + "?recepcionar=1")
    if ticket.estado != E.GENERADO:
        messages.info(request, f"{ticket.radicado} ya estaba {ticket.get_estado_display().lower()}.")
    else:
        _recepcionar(ticket, request.user)
        messages.success(request, f"{ticket.radicado} recepcionado: ya puede arrastrarlo al cronograma.")
    volver = request.POST.get("volver", "")
    if url_has_allowed_host_and_scheme(volver, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return redirect(volver)
    return redirect(ticket)


def _valor_mostrable(ticket, campo):
    """Cómo se lee un dato del ticket en el historial («Alta», «Sí», el nombre de la sede…)."""
    campo = {"empresa_nombre": "empresa", "sede_nombre": "sede", "sistema": "sistema_display",
             "falla_nombre": "falla_display", "alturas": "trabajo_alturas", "sst": "acompanamiento_sst"}.get(campo, campo)
    if campo == "disponibilidad":
        return " · ".join(x for x in (ticket.resumen_no_disponible, ticket.restricciones) if x) or "Puede atender siempre"
    valor = getattr(ticket, campo)
    if hasattr(ticket, f"get_{campo}_display"):
        return getattr(ticket, f"get_{campo}_display")() or "—"
    if isinstance(valor, bool):
        return "Sí" if valor else "No"
    return str(valor) if valor not in (None, "") else "—"


def _sincronizar_cronograma(ticket, antes):
    """Lleva la corrección a las actividades del cronograma que aún no tienen la autorización enviada."""
    from cronograma.models import Actividad

    avisar = []
    for a in ticket.actividades.all():
        if a.autorizacion == Actividad.Autorizacion.SIN_ENVIAR:
            a.cliente = ticket.empresa.razon_social
            a.sede = ticket.sede.nombre if ticket.sede else ""
            if a.actividad.strip() == antes["descripcion_texto"].strip():
                a.actividad = ticket.descripcion
            if a.notificar_a.replace(" ", "") == antes["correos_texto"].replace(" ", ""):
                a.notificar_a = " ; ".join(ticket.correos_respuesta)
            a.trabajo_alturas, a.sst = ticket.trabajo_alturas, ticket.acompanamiento_sst or a.sst
        else:
            avisar.append(a)
        a.calcular_choque()
        a.save()
    return avisar


@login_required
@supervisor_requerido
def editar_ticket(request, radicado):
    """El área técnica (casi siempre Guana, tras hablar con el cliente) corrige lo que el cliente puso por error.

    Queda en el historial qué decía antes y qué dice ahora, con el motivo; si se marca, el cliente
    recibe el resumen del ajuste. Las actividades del cronograma sin autorización enviada se actualizan.
    """
    ticket = obtener_ticket(request.user, radicado)
    original = Ticket.objects.get(pk=ticket.pk)  # el form modifica `ticket` al validar
    form = RadicacionForm(request.POST or None, request.FILES or None, instance=ticket, usuario=request.user)
    if request.method == "POST" and form.is_valid():
        motivo = request.POST.get("motivo_ajuste", "").strip()[:500]
        informar = request.POST.get("informar_cliente") == "on"
        campos = [c for c in form.changed_data if c in form.fields and c not in ("archivos", "latitud", "longitud")
                  and not c.startswith("nd_")]
        cambio_disponibilidad = any(c.startswith("nd_") for c in form.changed_data)
        antes = {c: _valor_mostrable(original, c) for c in campos}
        antes["disponibilidad"] = _valor_mostrable(original, "disponibilidad")
        antes["descripcion_texto"], antes["correos_texto"] = original.descripcion, " ; ".join(original.correos_respuesta)
        with transaction.atomic():
            form.save()
            ticket.refresh_from_db()
            lineas = [f"• {form.fields[c].label}: {antes[c]} → {_valor_mostrable(ticket, c)}" for c in campos
                      if antes[c] != _valor_mostrable(ticket, c)]
            if cambio_disponibilidad:
                lineas.append(f"• Cuándo no puede atender: {antes['disponibilidad']} → {_valor_mostrable(ticket, 'disponibilidad')}")
            seg = None
            if lineas or form.cleaned_data["archivos"]:
                detalle = "\n".join(lineas) or "• Se agregaron adjuntos."
                seg = Seguimiento.objects.create(
                    ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                    mensaje="Datos de la solicitud corregidos:\n" + detalle + (f"\nMotivo: {motivo}" if motivo else ""),
                )
            _guardar_adjuntos(ticket, form.cleaned_data["archivos"], request.user, seg)
            visible = None
            if informar and lineas:
                visible = Seguimiento.objects.create(
                    ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.COMENTARIO,
                    mensaje="Ajustamos su solicitud según lo conversado con usted:\n" + "\n".join(lineas)
                            + (f"\n{motivo}" if motivo else ""),
                )
            con_autorizacion = _sincronizar_cronograma(ticket, antes) if lineas else []
            confirmados = original.por_confirmar_display
            ticket.por_confirmar, ticket.confirmado_por, ticket.confirmado_en = [], request.user, timezone.now()
            ticket.save(update_fields=["por_confirmar", "confirmado_por", "confirmado_en", "actualizado"])
            if confirmados:
                Seguimiento.objects.create(
                    ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                    mensaje="Confirmó lo que el cliente no supo al radicar: " + ", ".join(confirmados).lower() + ".")
            recepcionado = request.POST.get("recepcionar") == "1" and ticket.estado == E.GENERADO
            if recepcionado:
                _recepcionar(ticket, request.user)
        if visible:
            notificaciones.actualizacion_para_cliente(ticket, visible)
        if recepcionado:
            messages.success(request, f"{ticket.radicado} confirmado y recepcionado: ya puede arrastrarlo al cronograma.")
        elif not lineas:
            messages.info(request, "Datos confirmados." if confirmados else "No hubo cambios en los datos.")
        else:
            messages.success(request, f"Solicitud corregida ({len(lineas)} cambio{'s' if len(lineas) != 1 else ''})."
                             + (" Se le informó al cliente." if visible else ""))
        rojas = [a for a in ticket.actividades.all() if a.choque]
        if rojas:
            messages.error(request, f"¡Atención! Con la nueva disponibilidad, la visita programada cruza: {rojas[0].choque}")
        if con_autorizacion:
            messages.warning(request, "Esta solicitud ya tiene la autorización de ingreso enviada al cliente: si el cambio "
                                      "afecta la visita, edítela en el cronograma y reenvíe la autorización.")
        return redirect(ticket)
    return render(request, "tickets/editar_ticket.html", {"form": form, "ticket": ticket,
                                                          "recepcionar": request.GET.get("recepcionar") == "1",
                                                          "motivo": request.POST.get("motivo_ajuste", ""),
                                                          "informar": request.POST.get("informar_cliente") == "on"})


# ============================================================================
# Etapas del servicio e informe técnico (unificación de janotecsas.netlify.app)
# ============================================================================
@login_required
@supervisor_requerido
@require_POST
def guardar_etapas(request, radicado):
    """Fecha estimada y nota visible al cliente de cada etapa."""
    from . import etapas

    ticket = obtener_ticket(request.user, radicado)
    n = etapas.guardar_etapas(ticket, request.POST)
    if n:
        Seguimiento.objects.create(ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                                   mensaje=f"Actualizó las fechas estimadas y notas de {n} etapa(s) para el cliente.")
        ticket.save(update_fields=["actualizado"])
    messages.success(request, "Etapas actualizadas: el cliente ya las ve." if n else "No hubo cambios en las etapas.")
    return redirect(ticket)


def _puede_entregar_informe(usuario, ticket):
    return usuario.es_supervisor or _puede_diligenciar_acta(usuario, ticket)


@login_required
@personal_requerido
@require_POST
def entregar_informe(request, radicado):
    """Informe en elaboración → al cliente (confirma o se cierra solo) o a comercial para la propuesta."""
    ticket = obtener_ticket(request.user, radicado)
    if ticket.estado != E.INFORME or not _puede_entregar_informe(request.user, ticket):
        messages.error(request, "El informe se entrega cuando el ticket está «Informe en elaboración».")
        return redirect(ticket)
    nota = request.POST.get("nota", "").strip()
    destino = request.POST.get("destino")
    archivos = request.FILES.getlist("informe")
    from . import informe as inf

    borrador = inf.vigente(ticket)
    if borrador and not borrador.aprobado and not archivos:
        messages.error(request, "El borrador del informe todavía no está aprobado por coordinación. Apruébelo (pasar a limpio) "
                                "o adjunte el informe en PDF.")
        return redirect(ticket)
    aprobado = borrador if borrador and borrador.aprobado and borrador.archivo and not archivos else None
    if not archivos and not nota and not aprobado:
        messages.error(request, "Adjunte el informe (PDF) o escriba sus conclusiones.")
        return redirect(ticket)
    if any(os.path.splitext(a.name)[1].lower() not in (".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png") or
           a.size > settings.ADJUNTO_MAX_MB * 1024 * 1024 for a in archivos):
        messages.error(request, f"El informe debe ser PDF, Word o imagen de máximo {settings.ADJUNTO_MAX_MB} MB.")
        return redirect(ticket)
    with transaction.atomic():
        seg = Seguimiento.objects.create(ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.COMENTARIO,
                                         mensaje="Informe técnico" + (f": {nota}" if nota else " entregado."))
        for a in archivos:
            Adjunto.objects.create(ticket=ticket, seguimiento=seg, archivo=a, nombre_original=a.name[:255], tamano=a.size,
                                   subido_por=request.user, es_informe=True)
        if aprobado:  # el informe que coordinación pasó a limpio
            with aprobado.archivo.open("rb") as f:
                Adjunto.objects.create(ticket=ticket, seguimiento=seg, archivo=ContentFile(f.read(), name=os.path.basename(aprobado.archivo.name)),
                                       nombre_original=f"Informe_tecnico_{ticket.radicado}.pdf", tamano=aprobado.archivo.size,
                                       subido_por=request.user, es_informe=True)
        if destino == "comercial":
            from comercial import servicios as com
            servicios_txt = request.POST.get("servicios", "").strip()
            materiales_txt = request.POST.get("materiales", "").strip()
            if aprobado and aprobado.es_estudio and not servicios_txt and not materiales_txt:
                servicios_txt, materiales_txt = aprobado.servicios.strip(), aprobado.materiales_para_comercial()
            if not servicios_txt and not materiales_txt:
                transaction.set_rollback(True)
                messages.error(request, "Para pasar a comercial escriba los servicios a cotizar, los materiales y equipos a "
                                        "suministrar, o ambos (con ellos se arma la propuesta y bodega alista lo aprobado).")
                return redirect(ticket)
            com.pasar_a_propuesta(ticket, request.user, nota or "Ver el informe técnico adjunto.",
                                  servicios=servicios_txt, materiales=materiales_txt)
            messages.success(request, f"Informe entregado: {ticket.radicado} pasó al área comercial para la propuesta.")
        else:
            seg_estado = ticket.cambiar_estado(
                E.VISITA_EJECUTADA, request.user,
                "Le entregamos el informe técnico. Por favor confírmenos si quedó conforme o si necesita algo más.")
            notificaciones.actualizacion_para_cliente(ticket, seg_estado)
            messages.success(request, "Informe entregado: el cliente lo ve al consultar su solicitud.")
    return redirect(ticket)


def _borrador_o_404(ticket):
    from . import informe as inf

    borrador = inf.vigente(ticket)
    if borrador is None:
        raise Http404("Este ticket no tiene borrador de informe.")
    return borrador


def _informe_abierto(ticket):
    """El borrador se trabaja con el ticket en «Informe en elaboración» o en el levantamiento pedido por comercial."""
    return ticket.estado == E.INFORME or _es_levantamiento(ticket)


def _guardar_zonas(request, borrador):
    """Zonas del estudio: nombre, qué se hará y fotos de la visual. Devuelve los errores (fotos que no son imagen)."""
    from .models import FotoZona, ZonaInforme

    errores = []

    def fotos(campo, zona):
        for f in request.FILES.getlist(campo):
            if os.path.splitext(f.name)[1].lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                errores.append(f"«{f.name}» no es una foto (jpg, png, webp).")
                continue
            FotoZona.objects.create(zona=zona, archivo=f, nombre_original=f.name[:255])

    FotoZona.objects.filter(zona__borrador=borrador, pk__in=[
        int(k.split("-")[1]) for k in request.POST if re.fullmatch(r"foto-\d+-borrar", k)]).delete()
    for zona in borrador.zonas.all():
        p = f"zona-{zona.pk}-"
        if request.POST.get(p + "borrar"):
            zona.delete()
            continue
        nombre = request.POST.get(p + "nombre", "").strip()[:150]
        if nombre:
            zona.nombre, zona.descripcion = nombre, request.POST.get(p + "descripcion", "").strip()
            if (request.POST.get(p + "orden") or "").isdigit():
                zona.orden = int(request.POST[p + "orden"])
            zona.save()
        fotos(p + "fotos", zona)
    nombre = request.POST.get("zona-nueva-nombre", "").strip()[:150]
    if nombre:
        ultima = borrador.zonas.order_by("-orden").first()
        zona = ZonaInforme.objects.create(borrador=borrador, nombre=nombre, orden=(ultima.orden + 1 if ultima else 1),
                                          descripcion=request.POST.get("zona-nueva-descripcion", "").strip())
        fotos("zona-nueva-fotos", zona)
    elif request.FILES.getlist("zona-nueva-fotos"):
        errores.append("Escriba el nombre de la zona nueva para guardar sus fotos.")
    return errores


@login_required
@personal_requerido
def informe_borrador(request, radicado):
    """Borrador del informe armado desde el acta: se corrige aquí y coordinación lo aprueba (pasa a limpio)."""
    from . import informe as inf

    ticket = obtener_ticket(request.user, radicado)
    if not _puede_entregar_informe(request.user, ticket):
        raise PermissionDenied
    borrador = inf.vigente(ticket)
    en_informe = _informe_abierto(ticket)
    if request.method == "POST":
        accion = request.POST.get("accion")
        if not en_informe:
            messages.error(request, "El borrador se trabaja mientras el ticket está «Informe en elaboración» o en el levantamiento.")
            return redirect(ticket)
        tipo = request.POST.get("tipo") if request.POST.get("tipo") in InformeBorrador.Tipo.values else None
        tipo_final = tipo or (borrador.tipo if borrador else inf.tipo_sugerido(ticket))
        if accion in ("generar", "redactar") and tipo_final == InformeBorrador.Tipo.VISITA and not ticket.actas.exists():
            messages.error(request, "Primero diligencie el acta de la visita: de ella sale el borrador del informe.")
            return redirect(ticket)
        if accion == "generar" and borrador is None:
            borrador = inf.generar(ticket, request.user, tipo=tipo)
            Seguimiento.objects.create(ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                                       mensaje=f"Generó el borrador del {borrador.get_tipo_display().split(' (')[0].lower()}.")
            messages.success(request, "Borrador generado. Revíselo y corrija lo que haga falta." + (
                " Agregue las zonas con sus fotos y la tabla de equipos y materiales." if borrador.es_estudio else ""))
            return redirect("informe_borrador", radicado)
        if borrador is None:
            return redirect(ticket)
        if accion == "reabrir" and borrador.aprobado and request.user.es_supervisor:
            borrador.estado = InformeBorrador.Estado.BORRADOR
            borrador.save(update_fields=["estado", "actualizado"])
            Seguimiento.objects.create(ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                                       mensaje="Devolvió el informe técnico a borrador para corregirlo.")
            messages.info(request, "El informe volvió a borrador: ya se puede corregir.")
            return redirect("informe_borrador", radicado)
        if borrador.aprobado:
            messages.error(request, "El informe ya está aprobado. Para corregirlo, coordinación debe devolverlo a borrador.")
            return redirect("informe_borrador", radicado)
        if accion == "redactar":
            inf.generar(ticket, request.user, borrador, tipo=tipo)
            messages.info(request, "Se volvió a redactar el texto con los datos actuales del acta." + (
                " Las zonas, fotos y equipos que ya había escrito se conservan." if borrador.es_estudio else ""))
            return redirect("informe_borrador", radicado)
        form = InformeBorradorForm(request.POST, instance=borrador)
        if form.is_valid():
            with transaction.atomic():
                borrador = form.save()
                errores = _guardar_zonas(request, borrador) if borrador.es_estudio else []
            for e in errores:
                messages.warning(request, e)
            if accion == "aprobar" and not errores:
                if not request.user.es_supervisor:
                    raise PermissionDenied
                if borrador.es_estudio and not (borrador.suministros.strip() or borrador.servicios.strip()):
                    messages.error(request, "Antes de aprobar el estudio escriba los equipos y materiales a suministrar, los "
                                            "servicios a cotizar, o ambos: con ellos comercial arma la propuesta.")
                    return redirect("informe_borrador", radicado)
                inf.aprobar(borrador, request.user)
                Seguimiento.objects.create(ticket=ticket, autor=request.user, tipo=Seguimiento.Tipo.NOTA_INTERNA, interno=True,
                                           mensaje="Aprobó el informe técnico y lo pasó a limpio: ya se puede entregar.")
                messages.success(request, "Informe aprobado y pasado a limpio. Ahora elija a quién se entrega."
                                 if not _es_levantamiento(ticket) else
                                 "Informe aprobado y pasado a limpio. Ya puede devolver el levantamiento al área comercial.")
                return redirect(ticket)
            if not errores:
                messages.success(request, "Cambios del borrador guardados.")
            return redirect("informe_borrador", radicado)
    else:
        form = InformeBorradorForm(instance=borrador) if borrador else None
    if borrador is None:
        return redirect(ticket)
    return render(request, "tickets/informe_borrador.html", {
        "ticket": ticket, "borrador": borrador, "form": form, "en_informe": en_informe,
        "zonas": borrador.zonas.prefetch_related("fotos") if borrador.es_estudio else [],
        "puede_aprobar": request.user.es_supervisor})


@login_required
@personal_requerido
def foto_zona(request, pk):
    """Fotos de las zonas del estudio: solo para quien trabaja el informe del ticket."""
    from .models import FotoZona

    foto = get_object_or_404(FotoZona.objects.select_related("zona__borrador__ticket"), pk=pk)
    ticket = obtener_ticket(request.user, foto.zona.borrador.ticket.radicado)
    if not _puede_entregar_informe(request.user, ticket):
        raise PermissionDenied
    try:
        return FileResponse(foto.archivo.open("rb"), as_attachment=False)
    except FileNotFoundError:
        raise Http404("La foto ya no está disponible.")


@login_required
@personal_requerido
def informe_borrador_pdf(request, radicado):
    """PDF del borrador (con marca de agua) o el definitivo si ya se aprobó."""
    from . import informe as inf

    ticket = obtener_ticket(request.user, radicado)
    if not _puede_entregar_informe(request.user, ticket):
        raise PermissionDenied
    borrador = _borrador_o_404(ticket)
    if borrador.aprobado and borrador.archivo:
        try:
            return FileResponse(borrador.archivo.open("rb"), as_attachment=False,
                                filename=f"Informe_tecnico_{ticket.radicado}.pdf", content_type="application/pdf")
        except FileNotFoundError:
            pass
    nombre, contenido = inf.pdf(borrador, definitivo=False)
    respuesta = HttpResponse(contenido, content_type="application/pdf")
    respuesta["Content-Disposition"] = f'inline; filename="{nombre}"'
    return respuesta


@login_required
@require_POST
def omitir_paso(request, radicado):
    """Coordinación omite el paso actual del área técnica (con motivo): se habilita el siguiente."""
    if not request.user.es_supervisor:
        raise PermissionDenied
    ticket = obtener_ticket(request.user, radicado)
    info = pasos.calcular(ticket)
    clave = request.POST.get("paso", "")
    motivo = request.POST.get("motivo", "").strip()
    if not info or not info["paso"] or info["paso"]["clave"] != clave or clave not in pasos.OMITIBLES:
        messages.error(request, "Solo se puede omitir el paso en que va el ticket.")
    elif len(motivo) < 10:
        messages.error(request, "Escriba por qué se omite el paso (mínimo 10 caracteres): queda en el historial.")
    else:
        pasos.omitir(ticket, clave, motivo, request.user)
        messages.success(request, f"Se omitió «{info['paso']['nombre']}». Ya está habilitado el paso siguiente.")
    return redirect(ticket)


# ============================================================================
# Aplazamiento de una visita programada (solo el coordinador del área técnica)
# ============================================================================
@login_required
@require_POST
def aplazar(request, radicado):
    """La visita programada se aplaza: sale del cronograma, vuelve a «Por programar» y queda el motivo resaltado."""
    from django.core.exceptions import PermissionDenied

    from cronograma import servicios as crono

    if not request.user.es_coordinador:
        raise PermissionDenied
    ticket = obtener_ticket(request.user, radicado)
    if ticket.estado not in (E.EN_CRONOGRAMA, E.AUTORIZADO):
        messages.error(request, "Solo se aplaza una visita que está programada en el cronograma.")
        return redirect(ticket)
    form = AplazarForm(request.POST)
    if not form.is_valid():
        errores = [e for lista in form.errors.values() for e in lista]
        messages.error(request, "No se aplazó: " + " ".join(errores))
        return redirect(f"{ticket.get_absolute_url()}#aplazar")
    d = form.cleaned_data
    aplazamiento = crono.aplazar_visita(ticket, request.user, d["motivo"], d["detalle"].strip(), d["informar_cliente"])
    messages.success(request, f"Visita aplazada (aplazamiento n.º {aplazamiento.numero}). El ticket volvió a «Recepcionado»: "
                              "ya puede reprogramarlo en el cronograma; "
                              "se avisó a los técnicos" + (" y al cliente." if d["informar_cliente"] else "."))
    return redirect(ticket)
