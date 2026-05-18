#!/usr/bin/env python3
"""
Reconstrói reports/tb_siac_mapping_final.json a partir dos catálogos SUPRA + SIMDNIT.

- Lista dbo.TB_SIAC_* no SUPRA (exclui TB_CIPI_* e cópias/arquivo: old, _23, *_yyyyMMdd_hhmm).
- Escolhe primary_simdnit = primeira tabela SIMDNIT em dbo que não seja SUPRA_CGCONT_*,
  por heurística no sufixo TB_SIAC (regra mais longa primeiro).
- column_pairs_exact_name = colunas com o mesmo nome (case-insensitive) + tipos DATA_TYPE.

Não substitui trabalho manual fino; serve para repor o ficheiro apagado e voltar a correr
infer_column_pairs / analyze_supra_simdnit_column_gaps.

Uso:
  python scripts/build_tb_siac_mapping_from_db.py
  DOTENV_PATH=.env .venv/bin/python scripts/build_tb_siac_mapping_from_db.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from supra_db_update.config import load_env, simdnit_endpoint, supra_local_endpoint
from supra_db_update.connection import connect_endpoint
from supra_db_update.tb_siac_scope import include_tb_siac_for_analysis

OUT = ROOT / "reports" / "tb_siac_mapping_final.json"

# (sufixo normalizado após TB_SIAC_, nome curto da tabela SIMDNIT sem schema)
# Ordem irrelevante — aplicamos “maior sufixo primeiro”.
SIMDNIT_BY_SUPRA_SUFFIX: list[tuple[str, str]] = [
    ("CONSULTA_SITUACAO_OBRA_SERVICO", "TB_CONSULTA_SITUACAO_OBRA_SERVICO"),
    ("CONVENIO_CONTRATO", "Dados_Convenio"),
    ("CONTRATO_SUPERVISORA", "Dados_Contrato_Supervisor_Supervisonado"),
    ("EMPENHO_CONTA_CORRENTE", "Dados_Empenho"),
    ("EMPENHO_ORG", "Dados_Empenho"),
    ("FISCAL_CONTRATO", "Dados_Fiscais_Contrato"),
    ("MEDICAO_MENOR", "Dados_Hist_Medicoes"),
    ("MEDICAO_MAIOR", "Dados_Medicao"),
    ("TERMO_ADITIVO", "Dados_Aditivo"),
    ("REAJUSTE", "Dados_Reajuste"),
    ("SEGMENTO", "Dados_Segmento"),
    ("RODOVIA", "Dados_Segmento"),
    ("IPG", "Dados_IPG"),
    ("SUPERVISORA", "Dados_Contrato_Supervisor_Supervisonado"),
    ("FINANCEIRO", "Dados_Pagamento"),
    ("EMPENHO", "Dados_Empenho"),
    ("CONVENIO", "Dados_Convenio"),
    ("CONTRATO", "Dados_Contrato"),
]

# Candidatos extra (sem CGCONT); primary continua o primeiro que existir na base.
EXTRA_CANDIDATES: dict[str, list[str]] = {
    "Dados_Contrato": ["Dados_Contrato_Item_Servico"],
}


def list_tb_siac_supra(cs) -> list[str]:
    cur = cs.cursor()
    cur.execute(
        """
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = N'dbo' AND TABLE_TYPE = N'BASE TABLE'
          AND TABLE_NAME LIKE N'TB_SIAC_%'
        ORDER BY TABLE_NAME
        """
    )
    return [
        r[0] for r in cur.fetchall() if include_tb_siac_for_analysis("dbo", r[0])
    ]


def simdnit_base_tables(cm) -> set[str]:
    cur = cm.cursor()
    cur.execute(
        """
        SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = N'dbo' AND TABLE_TYPE = N'BASE TABLE'
        """
    )
    out = set()
    for (t,) in cur.fetchall():
        if t.upper().startswith("SUPRA_CGCONT_"):
            continue
        out.add(t)
    return out


def normalize_siac_suffix(table: str) -> str:
    u = table.upper()
    if u.startswith("TB_SIAC_"):
        u = u[8:]
    for marker in ("_OLD_", "_old_"):
        if marker in u:
            u = u.split(marker)[0]
    u = re.sub(r"_\d{6,}_\d{4}.*$", "", u)
    return u


def pick_simdnit_table(norm: str, sim_tables: set[str]) -> tuple[str | None, list[str]]:
    rules = sorted(SIMDNIT_BY_SUPRA_SUFFIX, key=lambda x: len(x[0]), reverse=True)
    chosen_short: str | None = None
    for suffix, sim_short in rules:
        if norm == suffix or (
            len(suffix) < len(norm) and norm.startswith(suffix + "_")
        ):
            chosen_short = sim_short
            break
    if chosen_short is None or chosen_short not in sim_tables:
        return None, []

    qual = f"dbo.{chosen_short}"
    cands = [qual]
    for alt in EXTRA_CANDIDATES.get(chosen_short, []):
        if alt in sim_tables and f"dbo.{alt}" not in cands:
            cands.append(f"dbo.{alt}")
    return qual, cands


def table_columns_typed(conn, sch: str, tbl: str) -> dict[str, dict[str, str]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COLUMN_NAME, DATA_TYPE
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
        """,
        (sch, tbl),
    )
    return {
        r[0].lower(): {"name": r[0], "type": r[1] or ""}
        for r in cur.fetchall()
    }


def homonyms_from_schemas(
    sup_cols: dict[str, dict[str, str]], sim_cols: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    for low, s_meta in sup_cols.items():
        if low not in sim_cols:
            continue
        m_meta = sim_cols[low]
        pairs.append(
            {
                "supra": s_meta["name"],
                "supra_type": s_meta["type"],
                "simdnit": m_meta["name"],
                "simdnit_type": m_meta["type"],
            }
        )
    return pairs


def main() -> None:
    load_env()
    OUT.parent.mkdir(parents=True, exist_ok=True)

    with connect_endpoint(supra_local_endpoint()) as cs, connect_endpoint(
        simdnit_endpoint()
    ) as cm:
        sim_tables = simdnit_base_tables(cm)
        entries: list[dict[str, Any]] = []
        skipped: list[str] = []

        for tbl in list_tb_siac_supra(cs):
            norm = normalize_siac_suffix(tbl)
            primary, candidates = pick_simdnit_table(norm, sim_tables)
            if not primary:
                skipped.append(tbl)
                continue
            sch_s, t_s = "dbo", tbl
            sch_m, t_m = "dbo", primary.split(".", 1)[1]

            cur = cs.cursor()
            cur.execute(
                """
                SELECT COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
                ORDER BY ORDINAL_POSITION
                """,
                (sch_s, t_s),
            )
            sup_rows = cur.fetchall()
            supra_columns = [
                {"name": r[0], "type": r[1] or ""} for r in sup_rows
            ]
            sup_ct = {
                r[0].lower(): {"name": r[0], "type": r[1] or ""} for r in sup_rows
            }
            sim_ct = table_columns_typed(cm, sch_m, t_m)
            pairs = homonyms_from_schemas(sup_ct, sim_ct)

            entries.append(
                {
                    "supra_table": f"{sch_s}.{t_s}",
                    "supra_columns": supra_columns,
                    "primary_simdnit": primary,
                    "simdnit_candidates": candidates,
                    "column_pairs_exact_name": pairs,
                    "notes": "Auto: build_tb_siac_mapping_from_db.py; homónimos = interseção de nomes; alvo SIM sem SUPRA_CGCONT_*.",
                }
            )

        OUT.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(OUT, f"({len(entries)} tabelas SUPRA)")
        if skipped:
            print(
                "Sem mapeamento SIM (heurística / tabela inexistente):",
                ", ".join(skipped[:20]),
                ("…" if len(skipped) > 20 else ""),
            )


if __name__ == "__main__":
    main()
