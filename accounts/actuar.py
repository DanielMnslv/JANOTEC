"""Pantallas de «Actuar como»: el superusuario entra como el usuario de prueba de un área y vuelve al suyo.

Ver ``accounts/prueba.py`` para las reglas. Aquí están las tres vistas (lista de áreas, entrar, volver) y el
middleware que, mientras se actúa como otro, no deja guardar nada que no sea de PRUEBA.
"""
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from accounts import prueba
from accounts.models import Usuario

BACKEND = "django.contrib.auth.backends.ModelBackend"


def _seguro(request, url, otro):
    """La dirección a la que se vuelve, solo si es de este mismo sitio."""
    if url and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        return url
    return otro


def _real(request):
    """Quien de verdad está frente a la pantalla: el superusuario original si está actuando, si no el usuario."""
    return prueba.original(request) or request.user


@login_required
def probar_como(request):
    """Lista de áreas con su usuario de prueba y el botón «Actuar como»; el botón de arriba prepara el entorno PRUEBA
    (usuarios, catálogo, stock y proveedores: lo mismo que «manage.py preparar_prueba_completa»)."""
    real = _real(request)
    if not real.is_superuser:
        raise PermissionDenied
    if request.method == "POST":
        from io import StringIO

        from django.core.management import call_command
        salida = StringIO()
        call_command("preparar_prueba_completa", autor=real.username, stdout=salida)
        messages.success(request, "Entorno de prueba listo. " + " ".join(salida.getvalue().splitlines()[:2]))
        return redirect("probar_como")
    return render(request, "accounts/probar_como.html", {
        "usuarios": prueba.usuarios(), "real": real, "actuando": prueba.original(request) is not None})


@login_required
@require_POST
def actuar(request, pk):
    """Entra como el usuario de prueba indicado (solo un superusuario, solo hacia usuarios de prueba)."""
    real = _real(request)
    if not real.is_superuser:
        raise PermissionDenied
    destino = get_object_or_404(Usuario, pk=pk, is_active=True)
    if not prueba.es_de_prueba(destino):
        raise PermissionDenied
    login(request, destino, backend=BACKEND)  # cambia la sesión: lo que había se borra
    request.session[prueba.SESION] = real.pk
    prueba.registrar(real, destino, f"{real.username} actuó como {destino.username} (validación del proceso)")
    messages.info(request, f"Está actuando como «{destino.get_full_name()}». Todo lo que haga queda a nombre de ese "
                           "usuario de prueba y solo se puede guardar sobre cosas de PRUEBA.")
    return redirect(_seguro(request, request.POST.get("siguiente"), reverse("inicio")))


@require_POST
def volver(request):
    """Vuelve al usuario del superusuario que estaba actuando como otro."""
    real = prueba.original(request)
    if real is None:
        return redirect("inicio")
    actuaba = Usuario.objects.filter(pk=request.user.pk).first() if request.user.is_authenticated else None
    login(request, real, backend=BACKEND)
    request.session.pop(prueba.SESION, None)
    if actuaba is not None:
        prueba.registrar(real, actuaba, f"{real.username} dejó de actuar como {actuaba.username}")
    messages.success(request, "Volvió a su usuario.")
    return redirect(_seguro(request, request.POST.get("siguiente"), reverse("probar_como")))


class SoloPruebaMiddleware:
    """Mientras el superusuario actúa como otro, solo deja guardar sobre tickets, órdenes y clientes de PRUEBA."""

    LIBRES = ("probar_como", "actuar_como", "volver_a_mi_usuario", "logout")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_view(self, request, vista, args, kwargs):
        if request.method != "POST" or not request.session.get(prueba.SESION):
            return None
        nombre = request.resolver_match.view_name if request.resolver_match else ""
        if nombre in self.LIBRES or prueba.objeto_es_de_prueba(nombre, kwargs, request.POST):
            return None
        messages.error(request, "Mientras actúa como un usuario de prueba solo puede trabajar sobre tickets, órdenes y "
                                "clientes de PRUEBA. Vuelva a su usuario para trabajar con datos reales.")
        return redirect(_seguro(request, request.META.get("HTTP_REFERER"), reverse("inicio")))
