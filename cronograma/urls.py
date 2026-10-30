from django.urls import path

from . import views

app_name = "cronograma"

urlpatterns = [
    path("", views.semana, name="semana"),
    path("insumos/", views.insumos, name="insumos"),
    path("indicadores/", views.indicadores, name="indicadores"),
    path("correos/", views.correos, name="correos"),
    path("historial/", views.historial, name="historial"),
    path("directorio/", views.directorio, name="directorio"),
    path("configuracion/", views.configuracion, name="configuracion"),
    path("pdf/", views.pdf, name="pdf"),
    path("respaldo/", views.respaldo, name="respaldo"),
    path("importar/", views.importar, name="importar"),
    path("vaciar/", views.vaciar, name="vaciar"),
    path("visor/<str:token>/", views.visor, name="visor"),
    path("compartir/", views.compartir, name="compartir"),
    path("compartido/<str:token>/", views.compartido, name="compartido"),
    path("actividad/", views.guardar, name="crear"),
    path("actividad/<int:pk>/", views.guardar, name="editar"),
    path("actividad/<int:pk>/mover/", views.mover, name="mover"),
    path("actividad/<int:pk>/eliminar/", views.eliminar, name="eliminar"),
    path("actividad/<int:pk>/correo/", views.enviar_actividad, name="enviar_actividad"),
    path("actividad/<int:pk>/sst/", views.correo_sst, name="correo_sst"),
    path("actividad/<int:pk>/autorizacion/", views.enviar_autorizacion, name="enviar_autorizacion"),
    path("autorizacion/<str:token>/", views.autorizacion, name="autorizacion"),
    path("documentos/", views.documentos, name="documentos"),
    path("documentos/mios/", views.documentos_tecnico, name="mis_documentos"),
    path("documentos/<int:pk>/", views.documentos_tecnico, name="documentos_tecnico"),
    path("documentos/archivo/<int:pk>/", views.documento_descargar, name="documento"),
]
