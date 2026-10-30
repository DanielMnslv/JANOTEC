from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

admin.site.site_header = "JANOTEC · Administración de la plataforma de tickets"
admin.site.site_title = "JANOTEC Tickets"
admin.site.index_title = "Configuración"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("cuenta/ingresar/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    path("cuenta/", include("django.contrib.auth.urls")),
    path("comercial/", include("comercial.urls")),
    path("cronograma/", include("cronograma.urls")),
    path("inventario/", include("inventario.urls")),
    path("", include("tickets.urls")),
]
