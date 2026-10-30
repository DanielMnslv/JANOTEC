"""El cliente responde la propuesta desde el correo, sin usuario ni contraseña: aprueba, no aprueba o pide cambios.

«Apruebo» queda registrado con el solo clic: la página lo envía sola con JavaScript (como las estrellas), así los
antivirus del correo, que abren los enlaces sin ejecutar la página, no aprueban por el cliente. «Con cambios» y
«No la apruebo» abren la página con esa opción marcada para que el cliente escriba y confirme. El enlace es de una versión:
si ya se respondió o llegó una versión nueva, la página lo dice y no deja responder la anterior.
"""
from django.conf import settings
from django.contrib import messages
from django.core import signing
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from tickets.models import Seguimiento

from . import servicios
from .models import FichaComercial, PropuestaVersion

SAL = "comercial.propuesta"
VIGENCIA = 60 * 24 * 3600  # el enlace sirve dos meses
ACCIONES = ("aprobar", "cambios", "rechazar")


def enlace(version, accion=None):
    firma = signing.dumps({"v": version.pk}, salt=SAL)
    url = f"{settings.SITIO_URL.rstrip('/')}{reverse('comercial:respuesta', args=[firma])}"
    return f"{url}?r={accion}" if accion else url


@require_http_methods(["GET", "POST"])
def respuesta(request, firma):
    try:
        datos = signing.loads(firma, salt=SAL, max_age=VIGENCIA)
    except signing.BadSignature:
        messages.error(request, "El enlace de la propuesta venció o no es válido. Comuníquese con su asesor comercial.")
        return redirect("consulta")
    version = PropuestaVersion.objects.select_related("ficha__ticket__empresa", "ficha__comercial", "archivo").filter(pk=datos.get("v")).first()
    if version is None:  # la propuesta se retiró (o era una muestra): se le explica al cliente, sin página de error
        return render(request, "comercial/respuesta.html", {"no_disponible": True}, status=404)
    ficha = version.ficha
    actual = ficha.version_actual
    vigente = (ficha.etapa == FichaComercial.Etapa.ENVIADA and actual and actual.pk == version.pk
               and version.estado == PropuestaVersion.Estado.ENVIADA)
    error = ""
    accion = request.POST.get("accion") if request.method == "POST" else request.GET.get("r")
    accion = accion if accion in ACCIONES else ""
    if request.method == "POST" and vigente:
        texto = request.POST.get("texto", "").strip()
        if accion == "cambios" and len(texto) < 5:
            error = "Cuéntenos qué cambios necesita (por ejemplo, la marca de los equipos)."
        elif accion:
            t = ficha.ticket
            if accion == "aprobar":
                servicios.registrar_respuesta(ficha, None, True)
            elif accion == "rechazar":
                servicios.registrar_respuesta(ficha, None, False, texto)
            else:
                servicios.solicitar_cambios(ficha, None, texto)
            Seguimiento.objects.create(ticket=t, tipo=Seguimiento.Tipo.COMENTARIO, interno=True,
                                       mensaje=f"El cliente respondió la propuesta versión {version.numero} desde el correo: "
                                               f"{dict(aprobar='la aprobó', rechazar='no la aprobó', cambios='pidió cambios')[accion]}.")
            return redirect(request.path)
    version.refresh_from_db()
    return render(request, "comercial/respuesta.html", {
        "version": version, "ficha": ficha, "ticket": ficha.ticket, "vigente": vigente, "accion": accion, "error": error,
        "actual": actual, "aprobar_ya": request.method == "GET" and vigente and accion == "aprobar",
    })
