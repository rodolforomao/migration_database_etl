"""Contratos e validações pré-migração por tabela."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyodbc
import yaml


@dataclass
class ValidationResult:
    ok: bool
    message: str
    details: dict[str, Any]


def load_table_contracts(path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = Path(__file__).resolve().parent.parent / "table_contracts.yaml"
    if not path.is_file():
        return {"tables": {}}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {"tables": {}}


def _split_schema_table(qualified: str) -> tuple[str, str]:
    parts = qualified.strip().split(".", 1)
    if len(parts) != 2:
        raise ValueError(f"Tabela inválida (esperado schema.nome): {qualified!r}")
    return parts[0], parts[1]


def count_rows(cursor: pyodbc.Cursor, schema: str, table: str) -> int | None:
    try:
        cursor.execute(
            f"SELECT COUNT_BIG(*) FROM [{schema}].[{table}]",
        )
        row = cursor.fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return None


def validate_before_migration(
    source: pyodbc.Cursor,
    target: pyodbc.Cursor,
    qualified_table: str,
    rules: list[str],
) -> ValidationResult:
    schema, table = _split_schema_table(qualified_table)
    src_n = count_rows(source, schema, table)
    tgt_n = count_rows(target, schema, table)
    details = {
        "qualified": qualified_table,
        "source_row_count": src_n,
        "target_row_count": tgt_n,
    }

    if src_n is None:
        return ValidationResult(
            ok=False,
            message=f"{qualified_table}: tabela não encontrada ou sem permissão na origem (SIMDNIT).",
            details=details,
        )
    if tgt_n is None:
        return ValidationResult(
            ok=False,
            message=f"{qualified_table}: tabela não encontrada ou sem permissão no destino (SUPRA).",
            details=details,
        )

    for rule in rules:
        if rule == "source_row_count_gte_target":
            if src_n < tgt_n:
                return ValidationResult(
                    ok=False,
                    message=(
                        f"{qualified_table}: origem tem {src_n} linhas, destino tem {tgt_n}; "
                        "contrato source_row_count_gte_target falhou."
                    ),
                    details=details,
                )
        else:
            return ValidationResult(
                ok=False,
                message=f"Regra desconhecida: {rule!r}",
                details=details,
            )

    return ValidationResult(
        ok=True,
        message=f"{qualified_table}: validação OK (origem={src_n}, destino={tgt_n}).",
        details=details,
    )


def rules_for_table(contracts: dict[str, Any], qualified: str) -> list[str]:
    tables = contracts.get("tables") or {}
    entry = tables.get(qualified)
    if not entry:
        return []
    return list(entry.get("rules") or [])
