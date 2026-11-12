"""Le da (o le quita) a usuarios existentes el permiso de superusuario: ven y administran toda la plataforma,
incluida la Configuración (/admin/). No toca ni muestra contraseñas.

    python manage.py dar_superusuario yprada jguana mtibamoso          # solo muestra qué cambiaría
    python manage.py dar_superusuario yprada jguana mtibamoso --aplicar
    python manage.py dar_superusuario yprada --quitar --aplicar        # le quita el permiso

Los usuarios se buscan por su nombre de usuario o por su correo.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from accounts.models import Usuario


class Command(BaseCommand):
    help = "Da o quita el permiso de superusuario a usuarios existentes (por usuario o correo)."

    def add_arguments(self, parser):
        parser.add_argument("usuarios", nargs="+", metavar="USUARIO_O_CORREO")
        parser.add_argument("--aplicar", action="store_true", help="Hace el cambio (sin esto solo muestra qué cambiaría).")
        parser.add_argument("--quitar", action="store_true", help="Quita el permiso en lugar de darlo.")

    def handle(self, usuarios, aplicar=False, quitar=False, **opciones):
        encontrados, faltan = [], []
        for x in usuarios:
            u = Usuario.objects.filter(Q(username__iexact=x.strip()) | Q(email__iexact=x.strip())).first()
            (encontrados.append(u) if u else faltan.append(x))
        if faltan:
            raise CommandError(f"No existen estos usuarios: {', '.join(faltan)}. No se cambió nada.")
        nuevo = not quitar
        for u in encontrados:
            antes = "superusuario" if u.is_superuser else "normal"
            ahora = "superusuario" if nuevo else "normal"
            self.stdout.write(f"  {u.username:<20} {u.email:<34} {antes} → {ahora}")
            if aplicar and u.is_superuser != nuevo:
                u.is_superuser = nuevo
                if nuevo:
                    u.is_staff = True  # para entrar a Configuración (/admin/)
                u.save(update_fields=["is_superuser", "is_staff"])
        if aplicar:
            self.stdout.write(self.style.SUCCESS("Listo."))
        else:
            self.stdout.write(self.style.WARNING("No se cambió nada. Repita con --aplicar para hacerlo."))
