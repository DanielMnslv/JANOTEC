"""Rutas de la plataforma de tickets: portal del cliente, consulta pública por radicado, panel técnico,
actas, informes, oportunidades, reportes y configuración de clientes.
"""

from django.contrib.auth.decorators import login_required
from django.urls import path

from comercial import trimestre

from . import calificacion, centro, clientes, consulta, ensenar_redaccion, movil, reportes, views

urlpatterns = [
    path("api/tecnicos/v1/visitas/", movil.visitas, name="movil_visitas"),
    path("", views.inicio, name="inicio"),
    path("mi-dia/", views.mi_dia, name="mi_dia"),
    path("consulta/", consulta.consulta, name="consulta"),
    path("consulta/<str:firma>/", consulta.consulta_token, name="consulta_token"),
    path("calificar/<str:firma>/", calificacion.calificar_correo, name="calificar_correo"),
    path("buscar/", views.buscar_radicado, name="buscar_radicado"),
    path("asignados/", views.asignados_a_mi, name="asignados"),
    # Historial de servicio (actas de soporte)
    path("historial/", views.actas_lista, name="actas_lista"),
    path("historial/<int:numero>/", views.acta_detalle, name="acta_detalle"),
    path("historial/<int:numero>/pdf/", views.acta_pdf, name="acta_pdf"),
    path("historial/<int:numero>/evidencias/", views.acta_evidencias, name="acta_evidencias"),
    path("historial/nueva/", views.acta_sin_programar, name="acta_sin_programar"),
    path("historial/archivo/<str:tipo>/<int:pk>/", views.archivo_acta, name="archivo_acta"),
    path("ticket/<str:radicado>/acta/", views.acta_nueva, name="acta_nueva"),
    path("acta/mejorar-redaccion/", views.mejorar_redaccion, name="mejorar_redaccion"),
    path("ticket/<str:radicado>/devolver/", views.devolver_ticket, name="devolver_ticket"),
    path("devolucion/<int:pk>/corregida/", views.resolver_devolucion, name="resolver_devolucion"),
    path("acta/revisar-ortografia/", views.revisar_ortografia, name="revisar_ortografia"),
    # Ejemplos de redacción: así se le enseña a «Mejorar redacción» cómo deben quedar los textos
    path("soporte/redaccion/ejemplos/", ensenar_redaccion.lista, name="redaccion_ejemplos"),
    path("soporte/redaccion/ejemplos/<int:pk>/", ensenar_redaccion.lista, name="redaccion_ejemplo"),
    path("soporte/redaccion/ejemplos/<int:pk>/eliminar/", ensenar_redaccion.eliminar, name="redaccion_ejemplo_eliminar"),
    path("soporte/redaccion/ejemplos/<int:pk>/aprobar/", ensenar_redaccion.aprobar, name="redaccion_ejemplo_aprobar"),
    path("acta/redaccion/correccion/", ensenar_redaccion.correccion, name="redaccion_correccion"),
    path("ticket/<str:radicado>/acta/vincular/", views.vincular_acta, name="vincular_acta"),
    path("ticket/<str:radicado>/acta/<int:numero>/desvincular/", views.desvincular_acta, name="desvincular_acta"),
    # Oportunidades comerciales detectadas en campo
    path("oportunidades/", views.oportunidades, name="oportunidades"),
    path("oportunidades/<int:pk>/", views.oportunidad_actualizar, name="oportunidad_actualizar"),
    # Portal del cliente
    path("mis-solicitudes/", views.mis_tickets, name="mis_tickets"),
    path("mis-solicitudes/nueva/", views.nuevo_ticket, name="nuevo_ticket"),
    # Detalle compartido (el contenido depende del rol)
    path("ticket/<str:radicado>/", views.ticket_detalle, name="ticket_detalle"),
    path("ticket/<str:radicado>/responder/", views.responder_cliente, name="responder_cliente"),
    path("ticket/<str:radicado>/confirmar/", views.confirmar_solucion, name="confirmar_solucion"),
    path("ticket/<str:radicado>/calificar/", views.calificar, name="calificar"),
    path("ticket/<str:radicado>/cancelar/", views.cancelar, name="cancelar"),
    path("ticket/<str:radicado>/eliminar/", views.eliminar_ticket, name="eliminar_ticket"),
    path("ticket/<str:radicado>/gestionar/", views.gestionar, name="gestionar"),
    path("ticket/<str:radicado>/recepcionar/", views.recepcionar, name="recepcionar"),
    path("ticket/<str:radicado>/editar/", views.editar_ticket, name="editar_ticket"),
    path("ticket/<str:radicado>/etapas/", views.guardar_etapas, name="guardar_etapas"),
    path("ticket/<str:radicado>/aplazar/", views.aplazar, name="aplazar"),
    path("ticket/<str:radicado>/omitir-paso/", views.omitir_paso, name="omitir_paso"),
    path("ticket/<str:radicado>/finalizar/", views.finalizar_sin_propuesta, name="finalizar_sin_propuesta"),
    path("ticket/<str:radicado>/informe/", views.entregar_informe, name="entregar_informe"),
    path("ticket/<str:radicado>/informe/leer-materiales/", views.leer_materiales_informe, name="leer_materiales_informe"),
    path("ticket/<str:radicado>/informe/borrador/", views.informe_borrador, name="informe_borrador"),
    path("ticket/<str:radicado>/informe/borrador.pdf", views.informe_borrador_pdf, name="informe_borrador_pdf"),
    path("informe/foto/<int:pk>/", views.foto_zona, name="foto_zona"),
    path("adjunto/<int:pk>/", views.descargar_adjunto, name="descargar_adjunto"),
    path("ticket/<str:radicado>/inventario-listo/", views.inventario_listo, name="inventario_listo"),
    # Área de soporte
    path("soporte/", views.panel, name="panel"),
    path("soporte/indicadores/", login_required(centro.vista), name="indicadores_centro"),
    path("soporte/indicadores/metas/", login_required(centro.vista_metas), name="indicadores_metas"),
    path("soporte/reportes/descargables/", login_required(reportes.vista), name="reportes_descargables"),
    path("soporte/reportes/", views.reportes, name="reportes"),  # antiguo: lleva al Centro de indicadores
    path("soporte/reportes/trimestre/", login_required(trimestre.vista), name="reportes_trimestre"),
    # Clientes y sedes: el módulo principal (sedes, mantenimiento por sistema, equipos instalados y su hoja de vida)
    path("soporte/clientes/", clientes.clientes, name="clientes"),
    path("soporte/clientes/nuevo/", clientes.cliente_form, name="cliente_nuevo_admin"),
    path("soporte/clientes/importar/", clientes.importar_clientes, name="importar_clientes"),
    path("soporte/clientes/<int:pk>/", clientes.cliente_detalle, name="cliente_detalle"),
    path("soporte/clientes/<int:pk>/editar/", clientes.cliente_form, name="cliente_editar_admin"),
    path("soporte/clientes/<int:pk>/eliminar/", clientes.cliente_eliminar, name="cliente_eliminar"),
    path("soporte/sedes/<int:pk>/eliminar/", clientes.sede_eliminar, name="sede_eliminar"),
    path("soporte/clientes/<int:empresa_pk>/encargado/nuevo/", clientes.encargado_form, name="encargado_nuevo"),
    path("soporte/clientes/<int:empresa_pk>/encargado/<int:pk>/", clientes.encargado_form, name="encargado_editar"),
    path("soporte/sedes/<int:pk>/", clientes.sede_detalle, name="sede_detalle"),
    path("soporte/sedes/<int:sede_pk>/mantenimiento/nuevo/", clientes.plan_form, name="sede_plan_nuevo"),
    path("soporte/sedes/<int:sede_pk>/mantenimiento/<int:pk>/", clientes.plan_form, name="sede_plan_editar"),
    path("soporte/sedes/<int:sede_pk>/equipos/nuevo/", clientes.equipo_form, name="equipo_nuevo"),
    path("soporte/equipos/<int:pk>/", clientes.equipo_detalle, name="equipo_detalle"),
    path("soporte/equipos/<int:pk>/editar/", clientes.equipo_form, name="equipo_editar"),
]
