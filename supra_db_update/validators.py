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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymssql
import yaml

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

def load_table_contracts(path: Path | None = None) -> dict[str, TableContract]:
    if path is None:
        from supra_db_update._paths import bundle_root
        path = bundle_root() / "table_contracts.yaml"
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    result: dict[str, TableContract] = {}
    for supra_table, cfg in (raw.get("tables") or {}).items():
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_schema_table(qualified: str) -> tuple[str, str]:
    parts = qualified.strip().split(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Tabela inválida (esperado schema.nome): {qualified!r}")
    return parts[0], parts[1]


def count_rows(cursor: pymssql.Cursor, schema: str, table: str) -> int | None:
    try:
        cursor.execute(f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]")
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return None


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ---------------------------------------------------------------------------
# Regras
# ---------------------------------------------------------------------------

def _check_source_row_count_gte_target(
    source: pymssql.Cursor,
    target: pymssql.Cursor,
    contract: TableContract,
) -> ValidationResult:
    """SIMDNIT.total >= SUPRA.total — detecta deleção de linhas no SIMDNIT."""
    sim_schema, sim_table = _split_schema_table(contract.simdnit_table)
    supra_schema, supra_table = _split_schema_table(contract.supra_table)

    src_n = count_rows(source, sim_schema, sim_table)
    tgt_n = count_rows(target, supra_schema, supra_table)

    details: dict[str, Any] = {
        "simdnit_table": contract.simdnit_table,
        "supra_table": contract.supra_table,
        "simdnit_row_count": src_n,
        "supra_row_count": tgt_n,
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
                f"{contract.supra_table}: SIMDNIT tem {src_n:,} linhas e "
                f"SUPRA tem {tgt_n:,} — SIMDNIT perdeu {tgt_n - src_n:,} "
                "registro(s). Possível perda de dados no SIMDNIT."
            ),
            details=details,
        )

    return ValidationResult(
        ok=True,
        message=(
            f"{contract.supra_table}: contagem OK "
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

        details[cp.supra] = {
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
    contract: TableContract,
) -> list[ValidationResult]:
    """Executa todas as regras configuradas e devolve os resultados."""
    results: list[ValidationResult] = []
    for rule in contract.rules:
        handler = _RULE_HANDLERS.get(rule)
        if handler is None:
            results.append(ValidationResult(
                ok=False,
                message=f"Regra desconhecida: {rule!r}",
                details={"rule": rule, "table": contract.supra_table},
            ))
        else:
            results.append(handler(source, target, contract))
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
