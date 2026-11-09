"""Conector de MySQL: se usa mysqlclient si está instalado; si no, PyMySQL hace sus veces.

El servidor de producción (Plesk) no tiene mysqlclient, que necesita compilarse; usa PyMySQL.
Este ajuste vive en el repositorio para que ninguna publicación automática lo borre.
"""
try:
    import MySQLdb  # noqa: F401  (mysqlclient, el conector preferido)
except ImportError:
    try:
        import pymysql

        pymysql.install_as_MySQLdb()
    except ImportError:
        pass  # Sin ninguno de los dos: Django avisará al conectarse (o se usa SQLite).
