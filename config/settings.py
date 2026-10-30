"""
Configuración de la Plataforma de Tickets JANOTEC.

Todos los valores sensibles se leen de variables de entorno (archivo .env en
desarrollo). Ver .env.example.
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=True)  # el .env manda, también al recargar el servidor tras un cambio


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "si", "sí")


def env_list(name, default=""):
    return [v.strip() for v in os.getenv(name, default).split(",") if v.strip()]


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-insegura-cambiar-en-produccion")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "accounts",
    "tickets",
    "comercial",
    "cronograma",
    "inventario",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "tickets.context_processors.empresa",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Base de datos -----------------------------------------------------------
# DB_ENGINE=mysql (por defecto) o sqlite para pruebas rápidas.
if os.getenv("DB_ENGINE", "mysql").lower() == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": os.getenv("DB_NAME", "janotec_tickets"),
            "USER": os.getenv("DB_USER", "janotec"),
            "PASSWORD": os.getenv("DB_PASSWORD", ""),
            "HOST": os.getenv("DB_HOST", "127.0.0.1"),
            "PORT": os.getenv("DB_PORT", "3306"),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            "CONN_MAX_AGE": 60,
        }
    }

AUTH_USER_MODEL = "accounts.Usuario"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "inicio"
LOGOUT_REDIRECT_URL = "login"

LANGUAGE_CODE = "es-co"
TIME_ZONE = "America/Bogota"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = Path(os.getenv("MEDIA_ROOT", BASE_DIR / "media"))

# Adjuntos: tamaño máximo por archivo (MB) y extensiones permitidas
ADJUNTO_MAX_MB = int(os.getenv("ADJUNTO_MAX_MB", "10"))
ADJUNTO_EXTENSIONES = env_list(
    "ADJUNTO_EXTENSIONES", "pdf,jpg,jpeg,png,webp,mp4,docx,xlsx,txt,zip"
)
DATA_UPLOAD_MAX_MEMORY_SIZE = ADJUNTO_MAX_MB * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Correo -----------------------------------------------------------------
# En desarrollo los correos se imprimen en la consola.
EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)  # Hostinger: puerto 465 con SSL (y EMAIL_USE_TLS=False)
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "20"))
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "Soporte JANOTEC <soporte@janotec.com.co>")
NOTIFICAR_SOPORTE_EMAIL = os.getenv("NOTIFICAR_SOPORTE_EMAIL", "")  # buzón del área
NOTIFICAR_COMERCIAL_EMAIL = os.getenv("NOTIFICAR_COMERCIAL_EMAIL", "")  # oportunidades detectadas en campo
NOTIFICAR_SST_EMAIL = os.getenv("NOTIFICAR_SST_EMAIL", "")  # recibe las solicitudes de acompañamiento SST (HSE)
NOTIFICAR_ADMINISTRATIVA_EMAIL = os.getenv("NOTIFICAR_ADMINISTRATIVA_EMAIL", "")  # recibe el cronograma consolidado de la semana
# Fase de pruebas: lo que iba para un cliente llega a este correo (el personal JANOTEC recibe normal). Vacío = real.
CORREO_CLIENTES_PRUEBA = os.getenv("CORREO_CLIENTES_PRUEBA", "").strip()
if CORREO_CLIENTES_PRUEBA:
    EMAIL_BACKEND_REAL, EMAIL_BACKEND = EMAIL_BACKEND, "config.correo.RedirigirClientesBackend"
SITIO_URL = os.getenv("SITIO_URL", "http://127.0.0.1:8000")

# Prefijo del número de radicado: JNT-2026-000001
RADICADO_PREFIJO = os.getenv("RADICADO_PREFIJO", "JNT")

# Días que espera un ticket «Resuelto» la confirmación del cliente antes de cerrarse solo
TICKET_DIAS_CIERRE_AUTOMATICO = int(os.getenv("TICKET_DIAS_CIERRE_AUTOMATICO", "3"))

# --- Seguridad en producción ------------------------------------------------
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", "0"))
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

SESSION_COOKIE_AGE = 60 * 60 * 8  # 8 horas

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO" if not DEBUG else "WARNING"},
}
