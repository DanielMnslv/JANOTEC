from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.contrib.staticfiles.views import serve as servir_estatico
from django.urls import include, path, re_path

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

if not settings.DEBUG:
    # Estilos, íconos e imágenes en producción aunque Nginx no tenga «location /static/» (si la tiene, Nginx responde primero)
    urlpatterns += [re_path(r"^static/(?P<path>.*)$", servir_estatico, {"insecure": True})]
