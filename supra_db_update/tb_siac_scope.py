"""Critérios de inclusão/exclusão de tabelas TB_SIAC_* nas análises SUPRA↔SIMDNIT."""

from __future__ import annotations

import re

# Cópias de arquivo nomeadas explicitamente (maiúsculas).
_EXCLUDED_EXACT: frozenset[str] = frozenset(
    {
        "TB_SIAC_CONTRATO_01102025_1511",
        "TB_SIAC_CONTRATO_23",
        "TB_SIAC_CONTRATO_OLD_13122022_1209",
        "TB_SIAC_CONTRATO_OLD_14122022_1122",
        "TB_SIAC_CONTRATO_OLD_26122023_0842",
        "TB_SIAC_EMPENHO_CONTA_CORRENTE_OLD_24022023_1111",
        "TB_SIAC_EMPENHO_OLD_13122022_1219",
        "TB_SIAC_FINANCEIRO_23",
        "TB_SIAC_FINANCEIRO_OLD_13122022_1211",
        "TB_SIAC_FINANCEIRO_OLD_26122025_0907",
        "TB_SIAC_FISCAL_CONTRATO_23",
    }
)

# Sufixo tipo _ddMMyyyy_hhmm em cópias (ex.: _01102025_1511).
_DATE_SNAPSHOT_AT_END = re.compile(r"_\d{8}_\d{4}$", re.IGNORECASE)


def is_tb_siac_dbo(schema: str, table: str) -> bool:
    return schema.lower() == "dbo" and table.upper().startswith("TB_SIAC_")


def is_tb_siac_backup_or_archive(schema: str, table: str) -> bool:
    """
    True para tabelas de cópia/arquivo (old, snapshots datados, sufixo _23, lista fixa).
    Não inclui TB_CIPI — quem chama trata CIPI à parte.
    """
    if not is_tb_siac_dbo(schema, table):
        return False
    t = table.upper()
    if t in _EXCLUDED_EXACT:
        return True
    if "_OLD" in t:
        return True
    if _DATE_SNAPSHOT_AT_END.search(t):
        return True
    if t.endswith("_23"):
        return True
    return False


def include_tb_siac_for_analysis(schema: str, table: str) -> bool:
    """TB_SIAC_* em dbo, não CIPI, não cópia/arquivo."""
    if not is_tb_siac_dbo(schema, table):
        return False
    if table.upper().startswith("TB_CIPI_"):
        return False
    if is_tb_siac_backup_or_archive(schema, table):
        return False
    return True
