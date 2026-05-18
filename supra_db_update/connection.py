"""Conexão ODBC alinhada ao projeto 1_backup_sqlserver (pyodbc + Encrypt=no)."""

from __future__ import annotations

import os

import pyodbc

from supra_db_update.config import SqlServerEndpoint, get_setting, load_env


def pick_driver() -> str:
    load_env()
    custom = get_setting("DRIVER", "ODBC_DRIVER")
    if custom:
        return custom
    for name in (
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "ODBC Driver 13 for SQL Server",
    ):
        try:
            for row in pyodbc.drivers():
                if row == name:
                    return name
        except Exception:
            pass
    raise SystemExit(
        "Nenhum driver ODBC para SQL Server. Instale msodbcsql + unixodbc "
        "ou defina DRIVER no .env."
    )


def _server_part(ep: SqlServerEndpoint) -> str:
    instance = get_setting(f"{ep.label}_INSTANCE") if ep.label.startswith("SUPRA") else None
    if not instance:
        instance = get_setting("SIMDNIT_INSTANCE") if ep.label == "SIMDNIT" else None
    if instance:
        return f"{ep.host}\\{instance},{ep.port}"
    return f"{ep.host},{ep.port}"


def connect_endpoint(ep: SqlServerEndpoint) -> pyodbc.Connection:
    load_env()
    if not ep.user or not ep.password:
        raise SystemExit(
            f"Credenciais incompletas para {ep.label}: defina utilizador e palavra-passe no .env."
        )
    driver = pick_driver()
    conn_str = (
        f"DRIVER={{{driver}}};"
        f"SERVER={_server_part(ep)};"
        f"DATABASE={ep.database};"
        f"UID={ep.user};"
        f"PWD={ep.password};"
        "Encrypt=no;"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(conn_str, timeout=30)


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
