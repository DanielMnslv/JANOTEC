"""Vistas del área comercial: del requerimiento a la propuesta aprobada y su traspaso a instalación.

Orden de las secciones (sigue el camino de un requerimiento):
    Bandeja ─ Radicar (asesor o cliente) ─ Detalle y gestión ─ Informe del requerimiento y levantamiento
    ─ Traspaso a instalación ─ Devolver el levantamiento (desde técnica) ─ Respuesta del cliente
    ─ Cotizador (dirección aprueba antes de enviar) ─ Catálogo de precios.

El mismo ticket viaja entre áreas: comercial ⇄ técnica (levantamiento) y, aprobada la propuesta, a bodega e
instalación. Las reglas de esos traspasos están en servicios.py; aquí solo se valida quién y cuándo.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from accounts.models import Empresa
from tickets.models import OportunidadComercial, Seguimiento, Ticket
from tickets.permisos import cliente_requerido, obtener_ticket, personal_requerido, tickets_visibles

from . import pasos, servicios
from .pdf import ficha_pdf
from .forms import CotizacionClienteForm, FichaForm, GestionComercialForm, InformeForm
from .models import FichaComercial, InformeRequerimiento, LineaCotizacion, PropuestaVersion
from .permisos import comercial_requerido, obtener_ticket_comercial, propuestas_requerido, requerimientos_requerido

F = FichaComercial
T = Ticket
# Campos de texto de la ficha que se copian tal cual del formulario
TEXTOS_FICHA = ("contacto_cargo", "municipio", "direccion", "sistema_actual", "energia", "internet", "tamano",
                "otras_necesidades", "nivel_decision", "presupuesto", "observaciones")


def _errores(request, form):
    """Pasa los errores de un formulario a mensajes en pantalla (uno por error)."""
    for errores in form.errors.values():
        for e in errores:
            messages.error(request, e)


# --- Bandeja del área comercial -------------------------------------------------
@login_required
@comercial_requerido
def bandeja(request):
    """Bandeja comercial: los requerimientos por etapa, los que están en técnica, los míos y la búsqueda.

    Por defecto («A cargo del área comercial») lista solo lo que le toca mover a comercial: en gestión, propuestas
    enviadas esperando la respuesta del cliente y aprobadas por traspasar. Lo que está armando el área administrativa
    (elaborando o ajustando la propuesta) y lo que está en técnica se ve aparte, como seguimiento.
    Arriba muestra el conteo por etapa y cuántas propuestas ya pasaron la fecha máxima que pidió el cliente.
    """
    base = tickets_visibles(request.user).filter(ficha__isnull=False)
    vista = request.GET.get("vista", "comercial")
    if vista == "comercial":
        qs = base.filter(area=T.Area.COMERCIAL, ficha__etapa__in=F.ETAPAS_DE_COMERCIAL)
    elif vista == "administrativa":
        qs = base.filter(area=T.Area.COMERCIAL, ficha__etapa__in=F.ETAPAS_DE_ADMINISTRATIVA)
    elif vista == "tecnica":
        qs = base.filter(area=T.Area.TECNICA).exclude(estado__in=T.ESTADOS_FINALES)
    elif vista in F.Etapa.values:
        qs = base.filter(ficha__etapa=vista)
    else:
        qs = base
    if request.GET.get("mios") and request.user.es_comercial:
        qs = qs.filter(ficha__comercial=request.user)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(radicado__icontains=q) | Q(empresa__razon_social__icontains=q) | Q(asunto__icontains=q)
                       | Q(contacto_nombre__icontains=q) | Q(ficha__referencia_externa__icontains=q))
    conteo = dict(base.values_list("ficha__etapa").annotate(n=Count("id")))
    vencidas = base.filter(ficha__etapa__in=[F.Etapa.GESTION, F.Etapa.LEVANTAMIENTO, F.Etapa.ELABORANDO, F.Etapa.AJUSTES],
                           ficha__fecha_max_propuesta__lt=timezone.now()).count()
    pagina = Paginator(qs.order_by("-creado", "-id"), 20).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(request, "comercial/bandeja.html", {
        "pagina": pagina, "vista": vista, "q": q, "conteo": conteo, "vencidas": vencidas, "params": params.urlencode(),
        "etapas": F.Etapa.choices,
        "en_tecnica": base.filter(area=T.Area.TECNICA).exclude(estado__in=T.ESTADOS_FINALES).count(),
        "en_administrativa": base.filter(area=T.Area.COMERCIAL, ficha__etapa__in=F.ETAPAS_DE_ADMINISTRATIVA).count(),
        # Aprobadas por dirección para que comercial las lleve en persona
        "por_entregar": PropuestaVersion.objects.filter(ficha__ticket__in=base, por_comercial=True, entregada_cliente__isnull=True,
                                                        estado=PropuestaVersion.Estado.ENVIADA)
                        .select_related("ficha__ticket__empresa"),
    })


# --- Módulo del área administrativa: las propuestas ---------------------------------------
@login_required
@propuestas_requerido
def propuestas(request):
    """Bandeja del área administrativa: lo que hay que hacer con cada propuesta, en orden de urgencia.

    En revisión de dirección (para dirección, arriba: las que debe aprobar), devueltas por dirección para corregir,
    cambios que pidió el cliente, por armar (con el informe listo) y las enviadas, a las que comercial hace seguimiento.
    Cada requerimiento aparece en un solo grupo.
    """
    from .models import Cotizacion
    base = (FichaComercial.objects.filter(ticket__in=tickets_visibles(request.user))
            .select_related("ticket", "ticket__empresa", "ticket__sede", "comercial").order_by("fecha_max_propuesta", "creado"))
    en_revision = set(Cotizacion.objects.filter(en_revision=True).values_list("ficha_id", flat=True))
    observadas = dict(Cotizacion.objects.filter(en_revision=False, version__isnull=True).exclude(observaciones_direccion="")
                      .values_list("ficha_id", "observaciones_direccion"))
    armando = [F.Etapa.GESTION, F.Etapa.ELABORANDO, F.Etapa.AJUSTES]
    abiertas = list(base.filter(etapa__in=armando, ticket__area=T.Area.COMERCIAL))
    por_aprobar = [f for f in abiertas if f.pk in en_revision]
    devueltas, cambios, por_armar = [], [], []
    for f in abiertas:
        if f.pk in en_revision:
            continue
        if f.pk in observadas:
            f.observaciones = observadas[f.pk]
            devueltas.append(f)
        elif f.etapa == F.Etapa.AJUSTES:
            cambios.append(f)
        elif InformeRequerimiento.objects.filter(ficha=f).exists():
            por_armar.append(f)
    return render(request, "comercial/propuestas.html", {
        "por_aprobar": por_aprobar, "devueltas": devueltas, "cambios": cambios, "por_armar": por_armar,
        "enviadas": list(base.filter(etapa=F.Etapa.ENVIADA)), "es_direccion": request.user.es_direccion,
    })


# --- Radicar -------------------------------------------------------------------------
@login_required
@requerimientos_requerido
def nuevo(request):
    """El asesor radica un requerimiento (empresa existente o prospecto nuevo).

    Si llega desde una oportunidad detectada por un técnico en un acta, el formulario se llena con sus datos y la
    oportunidad pasa a «contactado».
    """
    pk = request.GET.get("oportunidad") or request.POST.get("oportunidad")
    oportunidad = get_object_or_404(OportunidadComercial, pk=pk) if pk else None
    inicial = {}
    if oportunidad and request.method != "POST":
        inicial = {"empresa_nombre": oportunidad.empresa.razon_social,
                   "sede_nombre": oportunidad.sede.nombre if oportunidad.sede else "", "servicios": oportunidad.servicios,
                   "asunto": (oportunidad.nota or "Oportunidad detectada en visita técnica")[:200],
                   "observaciones": f"Detectada por {oportunidad.detectada_por}"
                                    f"{f' en el acta {oportunidad.acta.numero}' if oportunidad.acta else ''}."}
        if oportunidad.acta and oportunidad.acta.ticket:
            t = oportunidad.acta.ticket
            inicial.update(contacto_nombre=t.contacto_nombre, contacto_telefono=t.contacto_telefono,
                           contacto_email=t.contacto_email)
    form = FichaForm(request.POST or None, request.FILES or None, usuario=request.user, initial=inicial)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        with transaction.atomic():
            empresa = d.get("empresa") or Empresa.objects.create(
                razon_social=d["nueva_empresa"].strip()[:200], nit=(d.get("nuevo_nit") or "").strip() or None,
                ciudad=d.get("municipio", "")[:100], direccion=d.get("direccion", "")[:200],
                telefono=d.get("contacto_telefono", "")[:50], email_contacto=d.get("contacto_email") or "", es_prospecto=True,
            )
            comercial = d.get("comercial") or (request.user if request.user.es_comercial else None)
            ficha = servicios.crear_ticket_comercial(
                empresa=empresa, sede=FichaForm.sede_final(empresa, d), solicitante=request.user, autor=request.user, asunto=d["asunto"],
                descripcion=d.get("descripcion"), impacto=d.get("impacto"), contacto_nombre=d["contacto_nombre"],
                contacto_telefono=d["contacto_telefono"], contacto_email=d.get("contacto_email"),
                no_disponible=form.no_disponible(), restricciones=d.get("restricciones"),
                origen=F.Origen.OPORTUNIDAD if oportunidad else F.Origen.COMERCIAL, oportunidad=oportunidad,
                fotos=d["fotos"], anexos=d["anexos"],
                ficha_datos={
                    **{campo: d.get(campo) or "" for campo in TEXTOS_FICHA},
                    "comercial": comercial, "servicios": d.get("servicios") or [],
                    "otras_sedes": d.get("otras_sedes", False), "fecha_max_propuesta": d.get("fecha_max_propuesta"),
                },
            )
            if oportunidad and oportunidad.estado == OportunidadComercial.Estado.NUEVA:
                oportunidad.estado = OportunidadComercial.Estado.CONTACTADO
                oportunidad.save(update_fields=["estado", "actualizado"])
        messages.success(request, f"Requerimiento {ficha.ticket.radicado} radicado. El cliente lo sigue con ese mismo número.")
        return redirect(ficha)
    return render(request, "comercial/nuevo.html", {"form": form, "oportunidad": oportunidad})


@login_required
@cliente_requerido
def nuevo_cliente(request):
    """El cliente pide una cotización desde su portal; queda radicada con el mismo número que seguirá todo el proceso."""
    if not request.user.empresa_id:
        messages.error(request, "Su usuario no está vinculado a una empresa. Contacte a JANOTEC.")
        return redirect("inicio")
    form = CotizacionClienteForm(request.POST or None, request.FILES or None, usuario=request.user)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        sede = d.get("sede")
        ficha = servicios.crear_ticket_comercial(
            empresa=request.user.empresa, sede=sede, solicitante=request.user, autor=request.user, asunto=d["asunto"],
            descripcion=d.get("descripcion"), impacto="", contacto_nombre=d["contacto_nombre"],
            contacto_telefono=d["contacto_telefono"], contacto_email=d["contacto_email"],
            no_disponible=form.no_disponible(), restricciones=d.get("restricciones"), origen=F.Origen.CLIENTE,
            fotos=d["fotos"], anexos=d["anexos"],
            ficha_datos={"contacto_cargo": d.get("contacto_cargo", ""), "sistema_actual": d.get("sistema_actual", ""),
                         "tamano": d.get("tamano", ""), "servicios": d.get("servicios") or [],
                         "fecha_max_propuesta": d.get("fecha_max_propuesta"),
                         "municipio": sede.ciudad if sede else request.user.empresa.ciudad},
        )
        messages.success(request, f"Su solicitud de cotización quedó radicada con el número {ficha.ticket.radicado}.")
        return redirect(ficha.ticket)
    return render(request, "comercial/nuevo_cliente.html", {"form": form})


# --- Detalle y gestión del área comercial ----------------------------------------------
@login_required
@comercial_requerido
def detalle(request, radicado):
    """Ficha del requerimiento: historial, versiones de la propuesta, informe y, si está en comercial, la gestión.

    Con la propuesta aprobada avisa si bodega aún tiene material por revisar antes del traspaso a instalación.
    """
    ticket = obtener_ticket_comercial(request.user, radicado)
    ficha = ticket.ficha
    contexto = {
        "ticket": ticket, "ficha": ficha, "pasos": F.PASOS_CLIENTE,
        "seguimientos": ticket.seguimientos.select_related("autor").prefetch_related("adjuntos"),
        "adjuntos_inicio": ticket.adjuntos.filter(seguimiento__tipo=Seguimiento.Tipo.CREACION),
        "versiones": ficha.versiones.select_related("archivo", "enviada_por", "entregada_por").order_by("-numero"),
        "informe": InformeRequerimiento.objects.filter(ficha=ficha).select_related("elaborado_por").first(),
        "adjuntos_informe": ticket.adjuntos.filter(es_informe=True),
        "en_comercial": ticket.area == T.Area.COMERCIAL,
        "pasos_comercial": pasos.calcular(ficha),
    }
    if ficha.etapa == F.Etapa.APROBADA:
        from inventario.solicitudes import pendiente_de_revision
        contexto["material_pendiente"] = pendiente_de_revision(ficha)
    if contexto["en_comercial"] and ficha.abierta:
        contexto["form_gestion"] = GestionComercialForm(ficha=ficha, usuario=request.user)
        if request.user.gestiona_requerimientos and ficha.etapa in (F.Etapa.GESTION, F.Etapa.ELABORANDO, F.Etapa.AJUSTES):
            contexto["form_informe"] = InformeForm(informe=contexto["informe"])
    from tickets.views import contexto_eliminar
    contexto["eliminar"] = contexto_eliminar(request.user, ticket)
    return render(request, "comercial/detalle.html", contexto)


@login_required
def ficha_descargar(request, radicado):
    """PDF «Ficha de requerimiento». El área comercial recibe la versión interna, salvo que pida la del cliente
    (?version=cliente) para compartirla; los demás solo pueden bajar la del cliente."""
    ticket = obtener_ticket_comercial(request.user, radicado)
    interna = request.user.ve_comercial and request.GET.get("version") != "cliente"
    nombre, contenido = ficha_pdf(ticket.ficha, interna=interna)
    respuesta = HttpResponse(contenido, content_type="application/pdf")
    modo = "attachment" if request.GET.get("descargar") else "inline"
    respuesta["Content-Disposition"] = f'{modo}; filename="{nombre}"'
    return respuesta


def _en_comercial(request, ticket):
    """True si el ticket está en el área comercial; si está en técnica, avisa y devuelve False."""
    if ticket.area != T.Area.COMERCIAL:
        messages.error(request, "La solicitud está en el área técnica; se gestiona allá hasta que vuelva a comercial.")
        return False
    return True


@login_required
@comercial_requerido
@require_POST
def gestionar(request, radicado):
    """Avanza la etapa de la propuesta, asigna asesor y valor, y registra respuestas o cambios del cliente.

    Enviar a mano un PDF de propuesta también exige dirección: solo ella, o administrativa con «Envío urgente» y
    motivo (dirección recibe el aviso). Cada paso a «Propuesta enviada» crea una versión nueva y le llega al cliente.
    """
    ticket = obtener_ticket_comercial(request.user, radicado)
    ficha = ticket.ficha
    if not _en_comercial(request, ticket):
        return redirect(ficha)
    form = GestionComercialForm(request.POST, request.FILES, ficha=ficha, usuario=request.user)
    if not form.is_valid():
        _errores(request, form)
        return redirect(ficha)
    d = form.cleaned_data
    interno = d["visibilidad"] == "interno"
    mensaje = (d.get("mensaje") or "").strip()
    etapa_previa = ficha.etapa
    # Enviar a mano un PDF de propuesta también pasa por dirección: solo ella, o administrativa si es urgente (con motivo)
    motivo_urgencia = ""
    if d["etapa"] == F.Etapa.ENVIADA and etapa_previa != F.Etapa.ENVIADA and not request.user.es_direccion:
        motivo_urgencia = " ".join(request.POST.get("motivo_urgencia", "").split())[:500]
        if request.POST.get("urgente") != "1" or len(motivo_urgencia) < 10:
            messages.error(request, "La propuesta la aprueba dirección antes de enviarla. Ármela en el cotizador y envíela "
                                    "a dirección o, si es urgente, marque «Envío urgente» con el motivo.")
            return redirect(ficha)
    pdf = None
    with transaction.atomic():
        # Sin asesor elegido, lo toma quien lo está gestionando (si es del área comercial)
        ficha.comercial = d.get("comercial") or (request.user if request.user.es_comercial else None)
        if d.get("valor_propuesta") is not None:
            ficha.valor_propuesta = d["valor_propuesta"]
        if d.get("motivo_rechazo"):
            ficha.motivo_rechazo = d["motivo_rechazo"]
        ficha.save()
        if d["etapa"] != etapa_previa and d["etapa"] in (F.Etapa.APROBADA, F.Etapa.RECHAZADA):
            servicios.registrar_respuesta(ficha, request.user, d["etapa"] == F.Etapa.APROBADA, d.get("motivo_rechazo", ""))
        elif d["etapa"] != etapa_previa and d["etapa"] == F.Etapa.AJUSTES:
            servicios.solicitar_cambios(ficha, request.user, d["cambios"])
        elif d["etapa"] != etapa_previa:
            ficha.etapa = d["etapa"]
            if d["etapa"] == F.Etapa.ENVIADA:
                ficha.fecha_envio = timezone.now()
            ficha.save()
            # El estado del ticket sigue a la propuesta: enviada, o de vuelta en gestión si se retoma
            if d["etapa"] == F.Etapa.ENVIADA and ticket.estado not in T.ESTADOS_FINALES:
                ticket.estado = T.Estado.PROPUESTA_ENVIADA
                ticket.save(update_fields=["estado", "actualizado"])
            elif etapa_previa == F.Etapa.ENVIADA and ticket.estado == T.Estado.PROPUESTA_ENVIADA                     and d["etapa"] not in (F.Etapa.CANCELADA,):
                ticket.estado = T.Estado.EN_PROCESO
                ticket.save(update_fields=["estado", "actualizado"])
            if d["etapa"] == F.Etapa.CANCELADA:
                ticket.cambiar_estado(T.Estado.CANCELADO, request.user, mensaje or "Solicitud comercial cancelada.")
        if d.get("propuesta"):
            seg = servicios._seguimiento(ticket, request.user, "Propuesta económica" +
                                         (f" por ${ficha.valor_propuesta:,.0f} antes de IVA".replace(",", ".") if ficha.valor_propuesta else "")
                                         + ". Puede descargarla aquí.")
            servicios.guardar_adjuntos(ticket, [d["propuesta"]], request.user, seg, es_propuesta=True)
            pdf = ticket.adjuntos.filter(es_propuesta=True).last()
        if ficha.etapa == F.Etapa.ENVIADA and etapa_previa != F.Etapa.ENVIADA:
            servicios.enviar_version(ficha, request.user, pdf)
        if mensaje and d["etapa"] not in (F.Etapa.CANCELADA,):
            servicios._seguimiento(ticket, request.user, mensaje, interno=interno)
        ticket.save(update_fields=["actualizado"])
    if ficha.etapa == F.Etapa.ENVIADA and etapa_previa != F.Etapa.ENVIADA:
        servicios.propuesta_enviada(ficha, pdf)
        if motivo_urgencia:
            servicios._seguimiento(ticket, request.user, "Envío urgente de la propuesta sin aprobación de dirección. "
                                                         f"Motivo: {motivo_urgencia}", interno=True)
            servicios.propuesta_urgente(ficha, request.user, motivo_urgencia, "adjunta")
    messages.success(request, f"Solicitud actualizada: {ficha.get_etapa_display()}.")
    return redirect(ficha)


@login_required
@requerimientos_requerido
@require_POST
def informe(request, radicado):
    """El área comercial registra el informe del requerimiento cuando no hizo falta visita."""
    ticket = obtener_ticket_comercial(request.user, radicado)
    ficha = ticket.ficha
    if not _en_comercial(request, ticket):
        return redirect(ficha)
    if ficha.etapa not in (F.Etapa.GESTION, F.Etapa.ELABORANDO, F.Etapa.AJUSTES):
        messages.error(request, "El informe se registra o ajusta mientras se arma la propuesta.")
        return redirect(ficha)
    form = InformeForm(request.POST, request.FILES)
    if not form.is_valid():
        _errores(request, form)
        return redirect(ficha)
    servicios.registrar_informe(ficha, request.user, form.cleaned_data, form.cleaned_data["archivos"])
    messages.success(request, "Informe del requerimiento guardado: ya puede elaborar la propuesta.")
    return redirect(ficha)


@login_required
@requerimientos_requerido
@require_POST
def solicitar_levantamiento(request, radicado):
    """Comercial pide la visita técnica de levantamiento (qué revisar y la jornada): el ticket pasa a técnica."""
    ticket = obtener_ticket_comercial(request.user, radicado)
    if _en_comercial(request, ticket):
        if ticket.ficha.etapa not in (F.Etapa.GESTION, F.Etapa.ELABORANDO):
            messages.error(request, "La visita de levantamiento se pide mientras se arma la propuesta.")
        else:
            nota, jornada = request.POST.get("nota", "").strip(), request.POST.get("jornada", "")
            if len(nota) < 10 or jornada not in ("manana", "tarde"):
                messages.error(request, "Escriba qué debe revisar el técnico y elija la jornada para la visita.")
                return redirect(ticket.ficha)
            servicios.solicitar_levantamiento(ticket.ficha, request.user, nota, jornada=jornada)
            messages.success(request, f"{ticket.radicado} quedó asignado al área técnica para la visita de levantamiento: "
                                      "el coordinador lo recepciona y lo programa.")
    return redirect(ticket.ficha)


@login_required
@requerimientos_requerido
@require_POST
def traspasar(request, radicado):
    """Propuesta aprobada → área técnica para la instalación, cuando bodega ya revisó el material."""
    ticket = obtener_ticket_comercial(request.user, radicado)
    if _en_comercial(request, ticket):
        from inventario.solicitudes import pendiente_de_revision
        pendiente = pendiente_de_revision(ticket.ficha)
        if ticket.ficha.etapa != F.Etapa.APROBADA:
            messages.error(request, "Solo se traspasa a instalación una propuesta aprobada.")
        elif pendiente:
            messages.error(request, f"Antes de pasar al área técnica, bodega debe revisar el material de la propuesta ({pendiente.numero}: "
                                    f"{pendiente.get_estado_display().lower()}).")
        else:
            servicios.traspasar_a_instalacion(ticket.ficha, request.user, request.POST.get("nota", "").strip())
            messages.success(request, f"{ticket.radicado} pasó al área técnica para la instalación. El cliente sigue con el mismo número.")
    return redirect(ticket.ficha)


@login_required
@requerimientos_requerido
@require_POST
def entregada(request, radicado, numero):
    """Comercial marca como entregada al cliente (con la fecha) la propuesta aprobada «por medio de comercial»."""
    from datetime import date

    from django.core.exceptions import ValidationError

    from . import cotizador as coti
    ticket = obtener_ticket_comercial(request.user, radicado)
    version = get_object_or_404(ticket.ficha.versiones, numero=numero)
    try:
        fecha = date.fromisoformat(request.POST.get("fecha", ""))
        coti.marcar_entregada(version, request.user, fecha)
    except ValueError:
        messages.error(request, "Escriba la fecha en que entregó la propuesta.")
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    else:
        messages.success(request, f"Propuesta v{version.numero} marcada como entregada al cliente el {fecha:%d/%m/%Y}.")
    return redirect(ticket.ficha)


# --- Desde el área técnica: devolver el levantamiento -----------------------------------
@login_required
@personal_requerido
@require_POST
def devolver(request, radicado):
    """El técnico (o coordinación) devuelve el levantamiento a comercial con el informe del requerimiento.

    Si hay borrador del informe de requerimientos, debe estar aprobado por dirección; su PDF queda como informe.
    """
    ticket = obtener_ticket(request.user, radicado)
    ficha = getattr(ticket, "ficha", None)
    if not ficha or ficha.etapa != F.Etapa.LEVANTAMIENTO or ticket.area != T.Area.TECNICA:
        raise Http404
    if not (request.user.es_supervisor or ticket.asignado_a_id == request.user.pk):
        raise PermissionDenied
    from tickets import informe as inf

    borrador = inf.vigente(ticket)
    if borrador and not borrador.aprobado:
        messages.error(request, "El borrador del informe de requerimientos todavía no está aprobado por dirección.")
        return redirect(ticket)
    form = InformeForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Complete el informe del requerimiento: qué necesita el cliente y los servicios o materiales a cotizar.")
        return redirect(ticket)
    d = form.cleaned_data
    archivos = list(d["archivos"])
    if borrador and borrador.archivo:  # el informe de requerimientos pasado a limpio queda como informe del ticket
        archivos.append(inf.copia_pdf(borrador))
    servicios.registrar_informe(ficha, request.user, d, archivos, con_visita=True)
    servicios.devolver_a_comercial(ficha, request.user, "\n".join(x for x in (
        d["necesidad"], d.get("solucion") and f"Solución: {d['solucion']}", d.get("servicios") and f"Servicios:\n{d['servicios']}",
        d.get("materiales") and f"Materiales y equipos:\n{d['materiales']}") if x))
    messages.success(request, f"{ticket.radicado} volvió al área comercial con el informe del requerimiento.")
    return redirect("panel")


# --- Cliente: responder la propuesta ------------------------------------------------
@login_required
@cliente_requerido
@require_POST
def responder(request, radicado):
    """El cliente responde la propuesta desde su solicitud: la aprueba, no la aprueba o pide cambios."""
    ticket = obtener_ticket(request.user, radicado)
    ficha = getattr(ticket, "ficha", None)
    if not ficha or ficha.etapa != F.Etapa.ENVIADA:
        raise Http404
    accion = request.POST.get("accion")
    if accion not in ("aprobar", "rechazar", "cambios"):
        raise Http404
    if accion == "cambios":
        cambios = request.POST.get("cambios", "").strip()
        if len(cambios) < 5:
            messages.error(request, "Cuéntenos qué cambios necesita (por ejemplo, la marca de los equipos).")
            return redirect(ticket)
        servicios.solicitar_cambios(ficha, request.user, cambios)
        messages.success(request, "Recibimos los cambios. Su asesor le enviará una nueva versión de la propuesta.")
        return redirect(ticket)
    servicios.registrar_respuesta(ficha, request.user, accion == "aprobar", request.POST.get("motivo", "").strip())
    if accion == "aprobar":
        messages.success(request, "¡Gracias! Aprobó la propuesta. Su asesor coordinará la instalación con el área técnica.")
    else:
        messages.info(request, "Registramos su respuesta. Su asesor podrá contactarlo para ajustar la propuesta.")
    return redirect(ticket)


# --- Cotizador: la propuesta se arma con el catálogo y sale en PDF ---------------------------
@login_required
@propuestas_requerido
def cotizador(request, radicado):
    """Cotizador: la propuesta se arma con el catálogo (descuento, IVA, margen) y sale en PDF con su código PE.

    Comercial la envía a dirección; mientras está en revisión solo dirección la toca: la aprueba (sale al
    cliente) o la devuelve con observaciones. Si es urgente, comercial la envía con un motivo y dirección recibe
    el aviso. Acciones: guardar, informe (traer del informe), ver_pdf, a_direccion, devolver y enviar.
    """
    from django.core.exceptions import ValidationError

    from . import cotizador as coti
    from .forms import CotizacionForm, formset_lineas
    from .models import ItemCatalogo

    ticket = obtener_ticket_comercial(request.user, radicado)
    ficha = ticket.ficha
    if ticket.area != T.Area.COMERCIAL or ficha.etapa not in coti.ETAPAS_COTIZABLES:
        messages.error(request, "La propuesta se arma mientras el requerimiento está en el área comercial elaborándola.")
        return redirect(ficha)
    cotizacion = coti.borrador(ficha)
    Lineas = formset_lineas()
    es_direccion = request.user.es_direccion
    if request.method == "POST":
        accion = request.POST.get("accion", "guardar")
        # En revisión solo dirección la toca: la corrige, la aprueba (sale al cliente) o la devuelve
        if cotizacion.en_revision and not es_direccion:
            messages.error(request, "La propuesta está en revisión de dirección: no se modifica hasta que la apruebe o la devuelva.")
            return redirect("comercial:cotizador", radicado)
        if accion == "devolver":
            observaciones = " ".join(request.POST.get("observaciones", "").split())[:1000]
            if not (es_direccion and cotizacion.en_revision) or len(observaciones) < 5:
                messages.error(request, "Escriba qué hay que corregir para devolver la propuesta a comercial.")
                return redirect("comercial:cotizador", radicado)
            coti.devolver(cotizacion, request.user, observaciones)
            messages.info(request, "Propuesta devuelta a comercial con sus observaciones.")
            return redirect(ficha)
        form = CotizacionForm(request.POST, instance=cotizacion)
        lineas = Lineas(request.POST, instance=cotizacion, prefix="lineas")
        if form.is_valid() and lineas.is_valid():
            form.save()
            for i, f in enumerate(lineas.forms):
                if f.cleaned_data and not f.cleaned_data.get("DELETE") and f.cleaned_data.get("descripcion"):
                    f.instance.orden = i
                    item = ItemCatalogo.objects.filter(descripcion__iexact=f.cleaned_data["descripcion"]).first()
                    f.instance.item = item
            lineas.save()
            if accion == "informe":
                n = coti.traer_del_informe(cotizacion)
                messages.success(request, f"Se agregaron {n} ítem(s) del informe del requerimiento. Revise cantidades y precios.")
            elif accion == "ver_pdf":
                return redirect("comercial:cotizacion_pdf", radicado)
            elif accion == "a_direccion":
                try:
                    coti.enviar_a_direccion(cotizacion, request.user)
                except ValidationError as e:
                    messages.error(request, " ".join(e.messages))
                    return redirect("comercial:cotizador", radicado)
                messages.success(request, "Propuesta enviada a dirección: cuando la apruebe le llega al cliente.")
                return redirect(ficha)
            elif accion == "enviar":
                # Dirección la aprueba y sale; comercial solo la envía si es urgente, con el motivo
                motivo = ""
                if not es_direccion:
                    motivo = " ".join(request.POST.get("motivo_urgencia", "").split())
                    if request.POST.get("urgente") != "1" or len(motivo) < 10:
                        messages.error(request, "La propuesta la aprueba dirección. Si es urgente, marque «Envío urgente» "
                                                "y escriba el motivo (mínimo 10 caracteres).")
                        return redirect("comercial:cotizador", radicado)
                por_comercial = request.POST.get("entrega") == "comercial"
                try:
                    version = coti.enviar(cotizacion, request.user, motivo_urgencia=motivo, por_comercial=por_comercial)
                except ValidationError as e:
                    messages.error(request, " ".join(e.messages))
                    return redirect("comercial:cotizador", radicado)
                messages.success(request, f"Propuesta {version.codigo} " + ("aprobada y " if es_direccion else "") + (
                    "enviada al asesor comercial para que la entregue al cliente." if por_comercial
                    else "enviada al cliente con los botones para responder."))
                return redirect(ficha)
            else:
                messages.success(request, "Cotización guardada.")
            return redirect("comercial:cotizador", radicado)
        messages.error(request, "Revise los ítems marcados.")
    else:
        form = CotizacionForm(instance=cotizacion)
        lineas = Lineas(instance=cotizacion, prefix="lineas")
    catalogo = list(ItemCatalogo.objects.filter(activo=True).values("descripcion", "valor_unitario", "iva", "unidad", "tipo"))
    for c in catalogo:
        c["valor_unitario"], c["iva"] = float(c["valor_unitario"]), float(c["iva"])
    return render(request, "comercial/cotizador.html", {
        "ticket": ticket, "ficha": ficha, "cotizacion": cotizacion, "form": form, "lineas": lineas, "catalogo": catalogo,
        "palabras_categoria": LineaCotizacion.PALABRAS_CATEGORIA,
        "totales": cotizacion.totales(), "numero": (ficha.versiones.count() + 1),
        "codigo": coti.codigo(ficha, ficha.versiones.count() + 1),
        "es_direccion": es_direccion, "bloqueada": cotizacion.en_revision and not es_direccion,
    })


@login_required
@propuestas_requerido
def cotizacion_pdf(request, radicado):
    """Vista previa del PDF de la cotización en borrador (con la marca «BORRADOR»)."""
    from . import cotizador as coti
    from .pdf import propuesta_pdf

    ticket = obtener_ticket_comercial(request.user, radicado)
    cotizacion = coti.borrador(ticket.ficha)
    if not cotizacion.margen:
        messages.error(request, coti.MARGEN_OBLIGATORIO)
        return redirect("comercial:cotizador", radicado)
    numero = ticket.ficha.versiones.count() + 1
    nombre, contenido = propuesta_pdf(cotizacion, coti.codigo(ticket.ficha, numero), numero, borrador=True)
    respuesta = HttpResponse(contenido, content_type="application/pdf")
    respuesta["Content-Disposition"] = f'inline; filename="BORRADOR-{nombre}"'
    return respuesta


@login_required
@propuestas_requerido
def catalogo(request):
    """Catálogo de precios: lo que se cotiza seguido, con su valor de referencia."""
    from .forms import ItemCatalogoForm
    from .models import ItemCatalogo

    editar = ItemCatalogo.objects.filter(pk=request.GET.get("editar")).first() if request.GET.get("editar", "").isdigit() else None
    if request.method == "POST":
        editar = ItemCatalogo.objects.filter(pk=request.POST.get("pk")).first() if request.POST.get("pk", "").isdigit() else None
        form = ItemCatalogoForm(request.POST, request.FILES, instance=editar)
        if form.is_valid():
            item = form.save(commit=False)
            item.fuente = item.fuente or f"Ajustado por {request.user.get_full_name() or request.user.username}"
            item.save()
            messages.success(request, f"«{item.descripcion}» guardado en el catálogo.")
            return redirect("comercial:catalogo")
    else:
        form = ItemCatalogoForm(instance=editar)
    q = request.GET.get("q", "").strip()
    items = ItemCatalogo.objects.all()
    if q:
        items = items.filter(descripcion__icontains=q)
    return render(request, "comercial/catalogo.html", {"items": items, "form": form, "editar": editar, "q": q})
