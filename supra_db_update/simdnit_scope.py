"""
Âmbito SIMDNIT: só contratos cuja linha-mãe em dbo.Dados_Contrato satisfaz SG_UND_GESTORA.

Por defeito SG_UND_GESTORA = 'CGCONT' (configurável: SIMDNIT_SCOPE_SG_UND_GESTORA no .env).
"""

from __future__ import annotations

import re
from typing import Any

from supra_db_update.config import get_setting


def scope_sg_und_gestora() -> str:
    v = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    return v.strip()


def br_ident(ident: str) -> str:
    if not re.match(r"^[A-Za-z0-9_.]+$", ident):
        raise ValueError(f"identificador inválido: {ident!r}")
    return "[" + ident.replace("]", "]]") + "]"


def get_sk_contrato_scoped(cm, nu: str) -> int | None:
    """SK_CONTRATO em Dados_Contrato para o NU e SG_UND_GESTORA do âmbito."""
    sg = scope_sg_und_gestora()
    cur = cm.cursor()
    cur.execute(
        """
        SELECT TOP 1 SK_CONTRATO FROM dbo.Dados_Contrato
        WHERE NU_CON_FORMATADO = %s AND SG_UND_GESTORA = %s
        """,
        (nu, sg),
    )
    r = cur.fetchone()
    return int(r[0]) if r and r[0] is not None else None


def validate_nu_in_scoped_dados_contrato(cm, nu: str) -> tuple[bool, str]:
    """True se existir cabeçalho de contrato no âmbito para este NU."""
    sg = scope_sg_und_gestora()
    cur = cm.cursor()
    cur.execute(
        """
        SELECT 1 FROM dbo.Dados_Contrato
        WHERE NU_CON_FORMATADO = %s AND SG_UND_GESTORA = %s
        """,
        (nu, sg),
    )
    if cur.fetchone():
        return True, ""
    return (
        False,
        f"NU_CON_FORMATADO {nu!r} não existe em dbo.Dados_Contrato com "
        f"SG_UND_GESTORA = {sg!r} (simdnit_scope)",
    )


def _link_m_to_dc_conditions(
    table_qualifier: str, sim_cols_lower: dict[str, str]
) -> str:
    """Condições (dc ↔ tabela m) com OR entre NU e SK quando ambos existem."""
    parts: list[str] = []
    if "nu_con_formatado" in sim_cols_lower:
        cn = br_ident(sim_cols_lower["nu_con_formatado"])
        parts.append(
            f"dc.{br_ident('NU_CON_FORMATADO')} = {table_qualifier}.{cn}"
        )
    if "sk_contrato" in sim_cols_lower:
        cs = br_ident(sim_cols_lower["sk_contrato"])
        parts.append(f"dc.{br_ident('SK_CONTRATO')} = {table_qualifier}.{cs}")
    if not parts:
        return ""
    return "(" + " OR ".join(parts) + ")"


def sql_and_exists_scoped_dados_contrato(
    table_schema: str,
    table_name: str,
    table_alias: str,
    sim_cols_lower: dict[str, str],
) -> tuple[str, tuple[Any, ...]]:
    """
    Fragmento SQL começando com AND ... EXISTS (ligação a Dados_Contrato no âmbito).
    Parâmetros extra: (SG_UND_GESTORA,).
    """
    sg = scope_sg_und_gestora()
    tqual = f"{br_ident(table_schema)}.{br_ident(table_name)}"
    if table_alias:
        lhs = br_ident(table_alias)
    else:
        lhs = tqual
    link = _link_m_to_dc_conditions(lhs, sim_cols_lower)
    if not link:
        return " AND 1 = 0 ", ()
    frag = f"""
    AND EXISTS (
      SELECT 1 FROM dbo.Dados_Contrato AS dc
      WHERE dc.{br_ident('SG_UND_GESTORA')} = %s
      AND {link}
    )"""
    return frag, (sg,)


def sql_where_scoped_first_predicates(
    first_predicate_sql: str,
    first_params: tuple[Any, ...],
    table_schema: str,
    table_name: str,
    table_alias: str,
    sim_cols_lower: dict[str, str],
) -> tuple[str, tuple[Any, ...]]:
    """WHERE primeiro predicado (ex.: NU = ?) + EXISTS âmbito; parâmetros na ordem correta."""
    frag, extra = sql_and_exists_scoped_dados_contrato(
        table_schema, table_name, table_alias, sim_cols_lower
    )
    return f"WHERE ({first_predicate_sql}){frag}", first_params + extra
