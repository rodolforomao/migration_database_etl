#!/usr/bin/env python3
"""
Gera column_mapping.json — arquivo mestre editável para o mapeamento de colunas
SUPRA (TB_SIAC_*) → SIMDNIT (Dados_*).

Combina:
  - reports/tb_siac_mapping_final.json        (lista completa de colunas SUPRA + tipos)
  - reports/inferencia_colunas_por_contrato_*.json  (pares inferidos por valor)

Saída: column_mapping.json (raiz do projeto)

Como usar:
  python scripts/generate_column_mapping.py

Depois edite column_mapping.json para:
  - Preencher "simdnit_col" nas colunas marcadas como null
  - Ajustar "enabled": false para excluir tabelas ou colunas
  - Adicionar pares manualmente quando souber a correspondência

O sync (python -m supra_db_update sync) usa este arquivo automaticamente.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAPPING_FINAL = ROOT / "reports" / "tb_siac_mapping_final.json"
INFERENCE_GLOB = "inferencia_colunas_por_contrato_*.json"
OUT = ROOT / "column_mapping.json"

_UNSCOPEABLE = "sem NU_CON_FORMATADO nem SK_CONTRATO"
_CONTRACT_REF_COLS = {"NU_CON_FORMATADO", "CONTRATO", "NU_CONTRATO"}


def _find_inference() -> Path | None:
    candidates = sorted((ROOT / "reports").glob(INFERENCE_GLOB))
    return candidates[-1] if candidates else None


def _build_known_pairs(inf_entry: dict) -> dict[str, dict]:
    """Devolve {supra_col_lower: {simdnit_col, source}} de todas as inferências."""
    known: dict[str, dict] = {}
    for src, key in (
        ("homonym", "pairs_homonym"),
        ("semantic", "pairs_semantic"),
        ("inferred", "pairs_inferidos_valor"),
    ):
        for p in inf_entry.get(key, []):
            sl = p["supra"].lower()
            if sl not in known:
                known[sl] = {"simdnit_col": p["simdnit"], "source": src}
    return known


def _is_join_key(simdnit_col: str | None) -> bool:
    if simdnit_col is None:
        return False
    return simdnit_col.upper() in _CONTRACT_REF_COLS


def _is_syncable(inf_entry: dict | None) -> bool:
    if inf_entry is None:
        return False
    fs = inf_entry.get("fetch_supra", "")
    fd = inf_entry.get("fetch_simdnit", "")
    return _UNSCOPEABLE not in fs and _UNSCOPEABLE not in fd


def main() -> None:
    if not MAPPING_FINAL.is_file():
        sys.exit(f"Não encontrado: {MAPPING_FINAL}")

    mapping_final: list[dict] = json.loads(MAPPING_FINAL.read_text(encoding="utf-8"))

    inf_path = _find_inference()
    inference_by_table: dict[str, dict] = {}
    if inf_path:
        print(f"Usando inferência: {inf_path.name}")
        for entry in json.loads(inf_path.read_text(encoding="utf-8")):
            inference_by_table[entry["supra_table"]] = entry
    else:
        print("Aviso: nenhum arquivo de inferência encontrado em reports/.")

    tables_out: list[dict] = []

    for mf in mapping_final:
        supra_table = mf["supra_table"]
        simdnit_table = mf.get("primary_simdnit") or ""
        supra_cols: list[dict] = mf.get("supra_columns", [])

        inf_entry = inference_by_table.get(supra_table)
        syncable = _is_syncable(inf_entry)
        known_pairs = _build_known_pairs(inf_entry) if inf_entry else {}

        # verifica se tem chave de join
        has_join = any(_is_join_key(v["simdnit_col"]) for v in known_pairs.values())

        columns_out: list[dict] = []
        mapped_count = 0
        for col in supra_cols:
            cname = col["name"]
            ctype = col.get("type", "")
            pair_info = known_pairs.get(cname.lower())

            simdnit_col = pair_info["simdnit_col"] if pair_info else None
            source = pair_info["source"] if pair_info else "none"
            is_jk = _is_join_key(simdnit_col)

            if simdnit_col:
                mapped_count += 1

            col_entry: dict = {
                "enabled": True,
                "supra_col": cname,
                "supra_type": ctype,
                "simdnit_col": simdnit_col,
                "simdnit_type": None,
                "is_join_key": is_jk,
                "source": source,
                "note": "" if simdnit_col else "TODO: sem correspondência conhecida",
            }
            columns_out.append(col_entry)

        unmapped = len(supra_cols) - mapped_count
        comment = ""
        if not syncable:
            comment = (
                "Tabela fora do escopo de sincronização automática "
                "(SIMDNIT não tem coluna de contrato ligada a Dados_Contrato). "
                "Defina a estratégia manualmente antes de habilitar."
            )
        elif not has_join:
            comment = "Atenção: nenhuma coluna de join identificada. Revise os pares."
        elif unmapped > 0:
            comment = f"{unmapped} coluna(s) sem correspondência — preencha 'simdnit_col' para incluir no sync."

        table_entry: dict = {
            "enabled": syncable and has_join,
            "supra": supra_table,
            "simdnit": simdnit_table,
            "comment": comment,
            "columns": columns_out,
        }
        tables_out.append(table_entry)
        status = "OK" if syncable and has_join else "DESABILITADA"
        print(
            f"  [{status}] {supra_table} "
            f"({mapped_count}/{len(supra_cols)} colunas mapeadas)"
        )

    output = {
        "_doc": (
            "Mapeamento de colunas SUPRA (TB_SIAC_*) → SIMDNIT (Dados_*). "
            "Edite 'simdnit_col' para completar os links. "
            "null = coluna sem correspondência ainda conhecida. "
            "Colunas com simdnit_col=null são ignoradas no sync (SUPRA recebe NULL/default do banco)."
        ),
        "_instrucoes": [
            "1. Preencha 'simdnit_col' com o nome EXATO da coluna na tabela SIMDNIT indicada.",
            "2. Marque 'enabled': false para excluir uma tabela ou coluna da sincronização.",
            "3. Para habilitar tabelas DESABILITADAS: defina a estratégia de join e mude 'enabled' para true.",
            "4. Execute 'python -m supra_db_update compare' para ver o efeito das alterações.",
            "5. Execute 'python -m supra_db_update sync' para sincronizar.",
        ],
        "scope": {
            "sg_und_gestora": "CGCONT",
            "description": "Contratos geridos pela CGCONT no SIMDNIT (configurável via SIMDNIT_SCOPE_SG_UND_GESTORA no .env)",
        },
        "tables": tables_out,
    }

    OUT.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    total_enabled = sum(1 for t in tables_out if t["enabled"])
    print(f"\nGerado: {OUT}")
    print(f"Tabelas habilitadas: {total_enabled}/{len(tables_out)}")
    print("Edite column_mapping.json para completar os mapeamentos marcados como TODO.")


if __name__ == "__main__":
    main()
