"""Alertas de segurança pré-migração por tabela.

Regras suportadas
-----------------
source_row_count_gte_target
    SIMDNIT deve ter >= linhas que o SUPRA. Evita sincronizar um conjunto
    menor que o já existente, o que indicaria deleção indevida no SIMDNIT.

no_1900_date_regression
    Para cada coluna de data configurada em `date_columns`: se o SUPRA já
    tem registros com data válida (ano != 1900) para um contrato, o SIMDNIT
    não pode ter voltado a registrar 1900 para esse mesmo contrato.
    (Equipe terceira costuma sobrescrever datas válidas por 1900 em ajustes.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymssql

_CHUNK = 900


# ---------------------------------------------------------------------------
# Estrutura de configuração
# ---------------------------------------------------------------------------

@dataclass
class DateColPair:
    supra: str
    simdnit: str


@dataclass
class TableContract:
    supra_table: str
    simdnit_table: str
    join_supra: str = "contrato"
    join_simdnit: str = "NU_CON_FORMATADO"
    rules: list[str] = field(default_factory=list)
    date_columns: list[DateColPair] = field(default_factory=list)


@dataclass
class ValidationResult:
    ok: bool
    message: str
    details: dict[str, Any]


# ---------------------------------------------------------------------------
# Carregamento do YAML
# ---------------------------------------------------------------------------

def _parse_table_contracts_dict(tables_cfg: dict) -> "dict[str, TableContract]":
    result: dict[str, TableContract] = {}
    for supra_table, cfg in (tables_cfg or {}).items():
        cfg = cfg or {}
        date_columns = [
            DateColPair(supra=d["supra"], simdnit=d["simdnit"])
            for d in (cfg.get("date_columns") or [])
        ]
        result[supra_table] = TableContract(
            supra_table=supra_table,
            simdnit_table=cfg.get("simdnit_table", supra_table),
            join_supra=cfg.get("join_supra", "contrato"),
            join_simdnit=cfg.get("join_simdnit", "NU_CON_FORMATADO"),
            rules=list(cfg.get("rules") or []),
            date_columns=date_columns,
        )
    return result


def load_table_contracts(path: Path | None = None) -> "dict[str, TableContract]":
    if path is None:
        from supra_db_update._paths import runtime_root
        path = runtime_root() / "import_rules.json"

    if not path.is_file():
        return {}

    with path.open(encoding="utf-8") as f:
        raw = json.load(f)

    # New format: {"rules": [...], "table_contracts": {...}}
    if isinstance(raw, dict) and "table_contracts" in raw:
        return _parse_table_contracts_dict(raw["table_contracts"])

    # Legacy YAML-style format: {"tables": {...}}
    if isinstance(raw, dict) and "tables" in raw:
        return _parse_table_contracts_dict(raw["tables"])

    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_schema_table(qualified: str) -> tuple[str, str]:
    parts = qualified.strip().split(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Tabela inválida (esperado schema.nome): {qualified!r}")
    return parts[0], parts[1]


def count_rows(cursor: pymssql.Cursor, schema: str, table: str, where: str = "") -> int | None:
    try:
        clause = f" WHERE {where}" if where else ""
        cursor.execute(f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]{clause}")
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return None


def count_rows_in(
    cursor: pymssql.Cursor,
    schema: str,
    table: str,
    col: str,
    values: "list[str]",
) -> "int | None":
    """COUNT(*) filtrando [{col}] IN (values), executado em chunks para listas grandes."""
    if not values:
        return 0
    total = 0
    try:
        for chunk in _chunks(values, _CHUNK):
            ph = ",".join(["%s"] * len(chunk))
            cursor.execute(
                f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}] WHERE [{col}] IN ({ph})",
                chunk,
            )
            row = cursor.fetchone()
            if row is None:
                return None
            total += int(row[0])
    except Exception:
        return None
    return total


def _fetch_scope_contracts(source: pymssql.Cursor, sg: str) -> "list[str]":
    """Retorna a lista ÚNICA de NU_CON_FORMATADO dos contratos no escopo SG_UND_GESTORA no SIMDNIT.

    Usada para aplicar o mesmo escopo CGCONT ao lado SUPRA (via IN-list),
    tornando a comparação de contagens apples-to-apples.
    DISTINCT garante que contratos duplicados em Dados_Contrato não causem
    dupla-contagem em count_rows_in (que soma COUNT por chunk de IN-list).
    """
    if not sg:
        return []
    try:
        source.execute(
            "SELECT DISTINCT [NU_CON_FORMATADO] FROM [dbo].[Dados_Contrato] "
            "WHERE [SG_UND_GESTORA] = %s",
            (sg,),
        )
        return [str(r[0]) for r in source.fetchall() if r[0] is not None]
    except Exception:
        return []


def _scope_where_simdnit(contract: "TableContract", sg: str) -> str:
    """Cláusula WHERE para escopar linhas SIMDNIT ao SG_UND_GESTORA configurado.

    Dados_Contrato: filtra diretamente por [SG_UND_GESTORA].
    Demais tabelas: filtra por [{join_simdnit}] IN (subquery em Dados_Contrato).
    Reajuste usa [Contrato] como join, não NU_CON_FORMATADO — tratado via join_simdnit.
    """
    if not sg:
        return ""
    _, sim_table = _split_schema_table(contract.simdnit_table)
    if sim_table == "Dados_Contrato":
        return f"[SG_UND_GESTORA] = '{sg}'"
    jc = contract.join_simdnit
    return (
        f"[{jc}] IN ("
        f"SELECT [NU_CON_FORMATADO] FROM [dbo].[Dados_Contrato] "
        f"WHERE [SG_UND_GESTORA] = '{sg}')"
    )


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ---------------------------------------------------------------------------
# Regras
# ---------------------------------------------------------------------------

def _check_source_row_count_gte_target(
    source: pymssql.Cursor,
    target: pymssql.Cursor,
    contract: "TableContract",
    sg: str = "",
) -> "ValidationResult":
    """SIMDNIT(escopo CGCONT) >= SUPRA(mesmo escopo) — detecta deleção real de linhas.

    Ambos os lados são restritos ao mesmo conjunto de contratos CGCONT:
      - SIMDNIT: via subquery em Dados_Contrato WHERE SG_UND_GESTORA = sg
      - SUPRA:   via IN-list dos mesmos NU_CON_FORMATADO (JOIN col = join_supra)

    Isso evita falso-positivo causado por dados históricos no SUPRA para contratos
    que não existem mais no escopo SIMDNIT/CGCONT.
    """
    sim_schema, sim_table = _split_schema_table(contract.simdnit_table)
    supra_schema, supra_table = _split_schema_table(contract.supra_table)

    # Lado SIMDNIT — escopo CGCONT via subquery
    where_sim = _scope_where_simdnit(contract, sg)
    src_n = count_rows(source, sim_schema, sim_table, where_sim)

    # Lado SUPRA — mesmo escopo: busca lista de contratos CGCONT do SIMDNIT
    if sg:
        scope_contracts = _fetch_scope_contracts(source, sg)
        tgt_n = count_rows_in(target, supra_schema, supra_table, contract.join_supra, scope_contracts)
    else:
        tgt_n = count_rows(target, supra_schema, supra_table)

    scope_label = f" [escopo {sg}]" if sg else ""
    details: dict[str, Any] = {
        "simdnit_table": contract.simdnit_table,
        "supra_table": contract.supra_table,
        "simdnit_row_count": src_n,
        "supra_row_count": tgt_n,
        "scope": sg or "sem escopo",
    }

    if src_n is None:
        return ValidationResult(
            ok=False,
            message=(
                f"{contract.simdnit_table}: tabela não encontrada ou "
                "sem permissão na origem (SIMDNIT)."
            ),
            details=details,
        )
    if tgt_n is None:
        return ValidationResult(
            ok=False,
            message=(
                f"{contract.supra_table}: tabela não encontrada ou "
                "sem permissão no destino (SUPRA)."
            ),
            details=details,
        )

    if src_n < tgt_n:
        return ValidationResult(
            ok=False,
            message=(
                f"{contract.supra_table}: SIMDNIT{scope_label} tem {src_n:,} linhas e "
                f"SUPRA{scope_label} tem {tgt_n:,} — diferença de {tgt_n - src_n:,} "
                "registro(s) a verificar."
            ),
            details=details,
        )

    return ValidationResult(
        ok=True,
        message=(
            f"{contract.supra_table}: contagem OK{scope_label} "
            f"(SIMDNIT={src_n:,} >= SUPRA={tgt_n:,})."
        ),
        details=details,
    )


def _check_no_1900_date_regression(
    source: pymssql.Cursor,
    target: pymssql.Cursor,
    contract: TableContract,
) -> ValidationResult:
    """Verifica que datas válidas no SUPRA não diminuíram no SIMDNIT por contrato.

    NULL e 1900-01-01 são tratados como equivalentes ("sem data") nos dois lados.
    Só dispara alerta se o COUNT de datas válidas de um contrato no SIMDNIT ficou
    menor que no SUPRA — o que indica que datas reais foram apagadas/zeradas.
    """
    if not contract.date_columns:
        return ValidationResult(
            ok=True,
            message=f"{contract.supra_table}: nenhuma coluna de data configurada.",
            details={},
        )

    j_supra = f"[{contract.join_supra}]"
    j_sim = f"[{contract.join_simdnit}]"
    regressions: list[str] = []
    details: dict[str, Any] = {}

    for cp in contract.date_columns:
        sc = f"[{cp.supra}]"
        mc = f"[{cp.simdnit}]"

        # contagem de datas válidas por contrato no SUPRA
        # (ignora NULL e 1900 — ambos representam "sem data")
        try:
            target.execute(
                f"SELECT {j_supra}, COUNT(*) "
                f"FROM {contract.supra_table} "
                f"WHERE {sc} IS NOT NULL AND YEAR({sc}) != 1900 "
                f"GROUP BY {j_supra}"
            )
            supra_counts: dict[str, int] = {str(r[0]): int(r[1]) for r in target.fetchall()}
        except Exception as exc:
            details[f"{cp.supra}_query_error"] = str(exc)
            continue

        if not supra_counts:
            details[cp.supra] = "nenhum contrato com data válida no SUPRA"
            continue

        # contagem de datas válidas por contrato no SIMDNIT (mesma lógica)
        sim_counts: dict[str, int] = {}
        for chunk in _chunks(list(supra_counts.keys()), _CHUNK):
            ph = ",".join(["%s"] * len(chunk))
            try:
                source.execute(
                    f"SELECT {j_sim}, COUNT(*) "
                    f"FROM {contract.simdnit_table} "
                    f"WHERE {j_sim} IN ({ph}) "
                    f"AND {mc} IS NOT NULL AND YEAR({mc}) != 1900 "
                    f"GROUP BY {j_sim}",
                    chunk,
                )
                for r in source.fetchall():
                    sim_counts[str(r[0])] = int(r[1])
            except Exception as exc:
                details[f"{cp.simdnit}_query_error"] = str(exc)
                break

        # contratos onde SIMDNIT tem menos datas válidas que SUPRA
        _SAMPLE = 50
        regressed: list[dict] = []
        for cont, supra_n in supra_counts.items():
            sim_n = sim_counts.get(cont, 0)
            if sim_n < supra_n:
                regressed.append({
                    "contrato": cont,
                    "datas_validas_supra": supra_n,
                    "datas_validas_simdnit": sim_n,
                    "diferenca": supra_n - sim_n,
                })

        regressed.sort(key=lambda x: x["diferenca"], reverse=True)

        # busca valores reais de data para cada contrato com regressão (até _VAL_SAMPLE por lado)
        _VAL_SAMPLE = 5
        regressed_cons = [r["contrato"] for r in regressed[:_SAMPLE]]
        supra_vals: dict[str, list[str]] = {}
        sim_vals: dict[str, list[str]] = {}
        for chunk in _chunks(regressed_cons, _CHUNK):
            ph = ",".join(["%s"] * len(chunk))
            try:
                target.execute(
                    f"SELECT {j_supra}, {sc} FROM {contract.supra_table} "
                    f"WHERE {j_supra} IN ({ph}) AND {sc} IS NOT NULL AND YEAR({sc}) != 1900 "
                    f"ORDER BY {j_supra}",
                    chunk,
                )
                for r2 in target.fetchall():
                    k = str(r2[0])
                    lst = supra_vals.setdefault(k, [])
                    if len(lst) < _VAL_SAMPLE:
                        lst.append(str(r2[1]) if r2[1] is not None else "NULL")
            except Exception:
                pass
            try:
                source.execute(
                    f"SELECT {j_sim}, {mc} FROM {contract.simdnit_table} "
                    f"WHERE {j_sim} IN ({ph}) "
                    f"ORDER BY {j_sim}",
                    chunk,
                )
                for r2 in source.fetchall():
                    k = str(r2[0])
                    lst = sim_vals.setdefault(k, [])
                    if len(lst) < _VAL_SAMPLE:
                        lst.append(str(r2[1]) if r2[1] is not None else "NULL")
            except Exception:
                pass
        for r in regressed:
            cont = r["contrato"]
            r["valor_supra"]   = ", ".join(supra_vals.get(cont, [])) or "—"
            r["valor_simdnit"] = ", ".join(sim_vals.get(cont, []))   or "—"

        details[cp.supra] = {
            "col_supra":     cp.supra,
            "col_simdnit":   cp.simdnit,
            "table_supra":   contract.supra_table,
            "table_simdnit": contract.simdnit_table,
            "contratos_com_data_valida_no_supra": len(supra_counts),
            "contratos_com_regressao": len(regressed),
            "amostra": regressed[:_SAMPLE],
        }

        if regressed:
            regressions.append(
                f"  [{cp.supra} ← {cp.simdnit}]: "
                f"{len(regressed)} contrato(s) com menos datas válidas no SIMDNIT "
                f"do que no SUPRA."
            )

    if regressions:
        return ValidationResult(
            ok=False,
            message=(
                f"{contract.supra_table}: regressão de datas detectada!\n"
                + "\n".join(regressions)
            ),
            details=details,
        )

    return ValidationResult(
        ok=True,
        message=f"{contract.supra_table}: datas OK — sem regressão para 1900.",
        details=details,
    )


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_RULE_HANDLERS: dict[str, Any] = {
    "source_row_count_gte_target": _check_source_row_count_gte_target,
    "no_1900_date_regression": _check_no_1900_date_regression,
}


def validate_table_contract(
    source: pymssql.Cursor,
    target: pymssql.Cursor,
    contract: "TableContract",
    sg: str = "",
) -> "list[ValidationResult]":
    """Executa todas as regras configuradas e devolve os resultados."""
    results: list[ValidationResult] = []
    for rule in contract.rules:
        if rule == "source_row_count_gte_target":
            results.append(_check_source_row_count_gte_target(source, target, contract, sg=sg))
        elif rule == "no_1900_date_regression":
            results.append(_check_no_1900_date_regression(source, target, contract))
        else:
            results.append(ValidationResult(
                ok=False,
                message=f"Regra desconhecida: {rule!r}",
                details={"rule": rule, "table": contract.supra_table},
            ))
    return results


# ---------------------------------------------------------------------------
# Backward-compat (usado por cmd_validate_tables / testes existentes)
# ---------------------------------------------------------------------------

def rules_for_table(
    contracts: dict[str, TableContract],
    qualified: str,
) -> list[str]:
    entry = contracts.get(qualified)
    return list(entry.rules) if entry else []


def validate_before_migration(
    source: pymssql.Cursor,
    target: pymssql.Cursor,
    qualified_table: str,
    rules: list[str],
) -> ValidationResult:
    """Interface legada — usa simdnit_table == supra_table (sem mapeamento cruzado)."""
    contract = TableContract(
        supra_table=qualified_table,
        simdnit_table=qualified_table,
        rules=rules,
    )
    results = validate_table_contract(source, target, contract)
    if not results:
        return ValidationResult(ok=True, message=f"{qualified_table}: sem regras.", details={})
    # retorna o primeiro resultado negativo, ou o último se todos ok
    for r in results:
        if not r.ok:
            return r
    return results[-1]
