#!/usr/bin/env bash
# Actualiza la plataforma en el servidor Linux con lo último de GitHub.
# Uso (en la carpeta de la plataforma):   ./actualizar.sh
# Si el servicio de systemd tiene otro nombre:   SERVICIO=otro_nombre ./actualizar.sh
set -euo pipefail

cd "$(dirname "$0")"
SERVICIO="${SERVICIO:-janotec}"

echo "== Bajando cambios de GitHub"
git pull --ff-only

echo "== Eligiendo el Python de la plataforma"
# Con entorno propio (.venv) se usa ese; en el servidor Plesk es el Python de Plesk, el mismo de PassengerPython
if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
    echo "== Instalando dependencias"
    "$PY" -m pip install -q -r requirements.txt
elif [ -x /opt/plesk/python/3/bin/python3.10 ]; then
    PY=/opt/plesk/python/3/bin/python3.10
else
    PY=python3
fi
echo "Python: $PY"

echo "== Aplicando cambios a la base de datos"
"$PY" manage.py migrate --noinput

echo "== Preparando estilos e imágenes"
"$PY" manage.py collectstatic --noinput -v 0

echo "== Revisando la configuración"
"$PY" manage.py check

echo "== Reiniciando la plataforma"
if systemctl list-unit-files "${SERVICIO}.service" >/dev/null 2>&1 && systemctl cat "${SERVICIO}.service" >/dev/null 2>&1; then
    sudo systemctl restart "$SERVICIO"
    echo "Reiniciado el servicio $SERVICIO."
elif [ -d tmp ] || [ -f passenger_wsgi.py ]; then
    mkdir -p tmp && touch tmp/restart.txt  # Plesk / Passenger
    echo "Reinicio solicitado a Passenger (Plesk)."
else
    echo "No encontré el servicio «$SERVICIO». Reinicie la aplicación desde Plesk o con: SERVICIO=nombre ./actualizar.sh"
fi

echo "== Listo: $(git log -1 --format='%h %s')"
