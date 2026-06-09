"""Conexão SQL Server sem ODBC (pymssql / FreeTDS embutido)."""

from __future__ import annotations

import pymssql

from supra_db_update.config import SqlServerEndpoint, get_setting, load_env


def _resolve_server(ep: SqlServerEndpoint) -> tuple[str, str]:
    """Retorna (server, port) para pymssql.connect().

    Named instance → server='host\\instance', port ignorado (SQL Browser resolve).
    Sem instance    → server='host', port=ep.port.
    """
    instance = None
    if ep.label.startswith("SUPRA"):
        instance = get_setting(f"{ep.label}_INSTANCE")
    elif ep.label.startswith("SIMDNIT"):
        instance = get_setting("SIMDNIT_INSTANCE")
    if instance:
        return f"{ep.host}\\{instance}", ep.port
    return ep.host, ep.port


def connect_endpoint(ep: SqlServerEndpoint) -> pymssql.Connection:
    load_env()
    if not ep.user or not ep.password:
        raise SystemExit(
            f"Credenciais incompletas para {ep.label}: defina utilizador e palavra-passe no .env."
        )
    server, port = _resolve_server(ep)
    return pymssql.connect(
        server=server,
        port=port,
        user=ep.user,
        password=ep.password,
        database=ep.database,
        login_timeout=30,
    )


def test_connection(ep: SqlServerEndpoint) -> tuple[bool, str]:
    try:
        conn = connect_endpoint(ep)
        try:
            cur = conn.cursor()
            cur.execute("SELECT DB_NAME() AS db, @@VERSION AS v")
            cur.fetchone()
        finally:
            conn.close()
        return True, "OK"
    except Exception as e:
        return False, str(e)
