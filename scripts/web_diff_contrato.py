"""
Gera diff JSON de um contrato específico para a interface web do SUPRA.

Uso:
    python scripts/web_diff_contrato.py --contract-id C0001 [--limit 200]

Saída: JSON em stdout
  {
    "contract_id": "C0001",
    "contract":    "00 00007/2005",
    "table_supra": "dbo.TB_SIAC_CONTRATO",
    "action":      "UPDATE",
    "cols":        ["col1", "col2", ...],
    "added":       [[val1, val2, ...], ...],   // linhas que entram no SUPRA
    "added_total": 1,
    "removed":     [[val1, val2, ...], ...],   // linhas que saem do SUPRA
    "removed_total": 1,
    "common":      0,
    "warning":     "",
    "error":       ""
  }
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supra_db_update.change_queue import load_changeset
from supra_db_update.config import (
    get_setting,
    load_env,
    pick_supra_mode,
    simdnit_endpoint,
    supra_targets_for_mode,
)
from supra_db_update._paths import runtime_root
from supra_db_update.connection import connect_endpoint
from supra_db_update.differ import diff_rows_for_contract
from supra_db_update.table_map import load_table_map


def _mapping_meta(table_supra: str) -> dict:
    """Retorna join_col e colunas desabilitadas com mapeamento para a tabela."""
    mapping_path = runtime_root() / "column_mapping.json"
    if not mapping_path.exists():
        return {"join_col": None, "disabled_mapped": [], "sub_key_supra": None}
    try:
        raw = json.loads(mapping_path.read_text(encoding="utf-8"))
    except Exception:
        return {"join_col": None, "disabled_mapped": [], "sub_key_supra": None}

    tbl_key = table_supra.lower()
    for tbl in raw.get("tables", []):
        supra_val = ""
        for k, v in tbl.items():
            if k.lower() in ("supra", "supra_table") and isinstance(v, str):
                supra_val = v
                break
        if supra_val.lower() != tbl_key:
            continue
        join_col = None
        disabled_mapped: list[dict] = []
        sub_key_supra = tbl.get("sub_key_supra") or None
        for col in tbl.get("columns", []):
            simdnit_col = col.get("simdnit_col")
            supra_col = col.get("supra_col")
            if not simdnit_col or not supra_col:
                continue
            if col.get("is_join_key"):
                join_col = {"supra": supra_col, "simdnit": simdnit_col}
            elif not col.get("enabled", True):
                disabled_mapped.append({"supra": supra_col, "simdnit": simdnit_col})
        return {"join_col": join_col, "disabled_mapped": disabled_mapped, "sub_key_supra": sub_key_supra}
    return {"join_col": None, "disabled_mapped": [], "sub_key_supra": None}


def _safe(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float, bool)):
        return str(v)
    try:
        return str(v)
    except Exception:
        return repr(v)


def _row_sort_key(row: tuple) -> list:
    """Chave de ordenação multi-coluna: numérica quando possível, string caso contrário."""
    result = []
    for v in row:
        s = str(v) if v is not None else ""
        try:
            result.append((0, float(s), ""))
        except (ValueError, TypeError):
            result.append((1, 0.0, s))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Diff JSON de contrato para a interface web.")
    parser.add_argument("--contract-id", required=True, help="ID do contrato no JSON (ex: C0001)")
    parser.add_argument("--limit", type=int, default=200, help="Máximo de linhas por seção (+/-)")
    parser.add_argument("--json-path", default=None, help="Caminho explícito para pending_changes.json")
    args = parser.parse_args()

    load_env()

    if args.json_path:
        json_path = Path(args.json_path).resolve()
    else:
        json_path = Path(__file__).resolve().parent.parent / "pending_changes.json"
    if not json_path.exists():
        print(json.dumps({"erro": "pending_changes.json não encontrado"}, ensure_ascii=False))
        return 1

    cs = load_changeset(json_path)
    contract_change = cs.get_contract(args.contract_id)
    if not contract_change:
        print(json.dumps({"erro": f"Contrato {args.contract_id!r} não encontrado"}, ensure_ascii=False))
        return 1

    table_change = cs.get_table(contract_change.table_id)
    if not table_change:
        print(json.dumps({"erro": f"Tabela {contract_change.table_id!r} não encontrada"}, ensure_ascii=False))
        return 1

    pairs = load_table_map()
    pair = next((p for p in pairs if p.supra_table.lower() == table_change.table_supra.lower()), None)
    if not pair:
        print(json.dumps({
            "erro": f"Mapeamento de colunas não encontrado para {table_change.table_supra}",
            "contract_id": args.contract_id,
            "contract":    contract_change.contract,
            "table_supra": table_change.table_supra,
            "action":      contract_change.action,
            "cols": [], "added": [], "added_total": 0,
            "removed": [], "removed_total": 0, "common": 0,
            "warning": "", "error": f"Sem mapeamento para {table_change.table_supra}",
        }, ensure_ascii=False))
        return 1

    sg      = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    sim_ep  = simdnit_endpoint()
    mode    = pick_supra_mode()
    targets = supra_targets_for_mode(mode)

    try:
        with connect_endpoint(sim_ep) as src_conn, connect_endpoint(targets[0]) as dst_conn:
            rd = diff_rows_for_contract(
                src_conn.cursor(),
                dst_conn.cursor(),
                pair,
                contract_change.contract,
                sg,
            )
    except Exception as exc:
        print(json.dumps({
            "contract_id": args.contract_id,
            "contract":    contract_change.contract,
            "table_supra": table_change.table_supra,
            "action":      contract_change.action,
            "cols": [], "added": [], "added_total": 0,
            "removed": [], "removed_total": 0, "common": 0,
            "warning": "", "error": str(exc),
        }, ensure_ascii=False))
        return 1

    # inclui linhas brutas quando a contagem difere mas o diff de conteúdo não detectou mudanças
    count_mismatch = (
        len(rd.added) == 0 and len(rd.removed) == 0
        and contract_change.simdnit_count != contract_change.supra_count
    )
    raw_limit = min(args.limit, 50)
    # sub_key_supra é sempre necessário (inclusive para D/I); join_col/disabled_mapped só para count_mismatch
    full_meta = _mapping_meta(table_change.table_supra)
    meta = full_meta if count_mismatch else {"join_col": None, "disabled_mapped": [], "sub_key_supra": full_meta["sub_key_supra"]}

    # análise de divergência: duplicatas no SIMDNIT vs linhas genuinamente novas
    mismatch_analysis: dict = {}
    if count_mismatch and rd.sim_raw:
        sim_set   = set(rd.sim_raw)
        supra_set = set(rd.supra_raw)
        sim_dupes     = len(rd.sim_raw) - len(sim_set)      # linhas repetidas no SIMDNIT
        genuinely_new = sorted(sim_set - supra_set, key=_row_sort_key)
        supra_only    = sorted(supra_set - sim_set, key=_row_sort_key)
        dupe_sample   = sorted(sim_set,             key=_row_sort_key)
        sim_raw_sorted   = sorted(rd.sim_raw,   key=_row_sort_key)
        supra_raw_sorted = sorted(rd.supra_raw, key=_row_sort_key)
        mismatch_analysis = {
            "simdnit_total":     len(rd.sim_raw),
            "simdnit_unique":    len(sim_set),
            "simdnit_dupes":     sim_dupes,
            "supra_total":       len(rd.supra_raw),
            "supra_unique":      len(supra_set),
            "genuinely_new":     len(genuinely_new),
            "supra_only":        len(supra_only),
            "new_rows":          [[_safe(v) for v in r] for r in genuinely_new[:raw_limit]],
            "old_rows":          [[_safe(v) for v in r] for r in supra_only[:raw_limit]],
            "dupe_sample":       [[_safe(v) for v in r] for r in dupe_sample[:raw_limit]],
            "sim_raw_sorted":    [[_safe(v) for v in r] for r in sim_raw_sorted[:raw_limit]],
            "supra_raw_sorted":  [[_safe(v) for v in r] for r in supra_raw_sorted[:raw_limit]],
        }

    # colunas críticas que não devem ser NULL quando inseridas no SUPRA
    _NULL_WARN: dict[str, list[str]] = {
        "dbo.tb_siac_reajuste":             ["data_da_assinatura_do_reajuste"],
        "dbo.tb_siac_empenho_conta_corrente": ["Nota_de_Empenho"],
    }
    table_key = table_change.table_supra.lower()
    critical_cols = _NULL_WARN.get(table_key, [])
    # índices das colunas críticas dentro de rd.cols (case-insensitive)
    cols_lower = [c.lower() for c in rd.cols]
    warn_null_col_indices = [
        cols_lower.index(cc.lower())
        for cc in critical_cols
        if cc.lower() in cols_lower
    ]
    # verifica se alguma linha adicionada tem NULL nas colunas críticas
    null_warn_rows = []
    if warn_null_col_indices and rd.added:
        for ri, row in enumerate(rd.added[: args.limit]):
            for ci in warn_null_col_indices:
                if ci < len(row) and row[ci] is None:
                    null_warn_rows.append(ri)
                    break

    result = {
        "contract_id":        args.contract_id,
        "contract":           contract_change.contract,
        "table_supra":        table_change.table_supra,
        "table_simdnit":      pair.simdnit_table,
        "simdnit_host":       sim_ep.host,
        "simdnit_port":       sim_ep.port,
        "simdnit_database":   sim_ep.database,
        "simdnit_label":      sim_ep.label,
        "action":             contract_change.action,
        "simdnit_count":      contract_change.simdnit_count,
        "supra_count":        contract_change.supra_count,
        "cols":               rd.cols,
        "added":              [[_safe(v) for v in row] for row in rd.added[: args.limit]],
        "added_total":        len(rd.added),
        "removed":            [[_safe(v) for v in row] for row in rd.removed[: args.limit]],
        "removed_total":      len(rd.removed),
        "common":             rd.common,
        "warning":            rd.warning,
        "error":              rd.error,
        "truncated":          len(rd.added) > args.limit or len(rd.removed) > args.limit,
        "sim_raw":   ([[_safe(v) for v in r] for r in sorted(rd.sim_raw,   key=_row_sort_key)[:raw_limit]] if count_mismatch else []),
        "supra_raw": ([[_safe(v) for v in r] for r in sorted(rd.supra_raw, key=_row_sort_key)[:raw_limit]] if count_mismatch else []),
        "warn_null_col_idx":  warn_null_col_indices,
        "null_warn_rows":     null_warn_rows,
        "mapping_join_col":   meta["join_col"],
        "mapping_disabled":   meta["disabled_mapped"],
        "sub_key_supra":      meta["sub_key_supra"],
        "mismatch_analysis":  mismatch_analysis,
    }

    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
