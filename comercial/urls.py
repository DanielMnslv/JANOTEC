from django.urls import path

from . import respuesta, views

app_name = "comercial"

urlpatterns = [
    path("", views.bandeja, name="bandeja"),
    path("nuevo/", views.nuevo, name="nuevo"),
    path("solicitar-cotizacion/", views.nuevo_cliente, name="nuevo_cliente"),
    path("propuesta/<str:firma>/", respuesta.respuesta, name="respuesta"),
    path("catalogo/", views.catalogo, name="catalogo"),
    path("propuestas/", views.propuestas, name="propuestas"),
    path("<str:radicado>/", views.detalle, name="detalle"),
    path("<str:radicado>/ficha.pdf", views.ficha_descargar, name="ficha_pdf"),
    path("<str:radicado>/gestionar/", views.gestionar, name="gestionar"),
    path("<str:radicado>/informe/", views.informe, name="informe"),
    path("<str:radicado>/cotizador/", views.cotizador, name="cotizador"),
    path("<str:radicado>/cotizador/vista-previa.pdf", views.cotizacion_pdf, name="cotizacion_pdf"),
    path("<str:radicado>/levantamiento/", views.solicitar_levantamiento, name="levantamiento"),
    path("<str:radicado>/traspasar/", views.traspasar, name="traspasar"),
    path("<str:radicado>/version/<int:numero>/entregada/", views.entregada, name="entregada"),
    path("<str:radicado>/devolver/", views.devolver, name="devolver"),
    path("<str:radicado>/responder/", views.responder, name="responder"),
]
