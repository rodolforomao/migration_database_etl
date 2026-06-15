"""
Analisa todos os contratos D/I de uma tabela e detecta duplicatas no SIMDNIT.
Retorna apenas contratos onde as linhas extras são cópias exatas (SUPRA já correto).

Uso:
    python scripts/web_analise_duplicatas_tabela.py --table-id T03 [--json-path ...]
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
from supra_db_update.connection import connect_endpoint
from supra_db_update.differ import diff_rows_for_contract
from supra_db_update.table_map import load_table_map


def _safe(v) -> str | None:
    if v is None:
        return None
    try:
        return str(v)
    except Exception:
        return repr(v)


def _row_sort_key(row) -> list:
    result = []
    for v in row:
        s = str(v) if v is not None else ""
        try:
            result.append((0, float(s), ""))
        except (ValueError, TypeError):
            result.append((1, 0.0, s))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Analisa duplicatas SIMDNIT para uma tabela inteira.")
    parser.add_argument("--table-id",  required=True, help="ID da tabela (ex: T03)")
    parser.add_argument("--json-path", default=None,  help="Caminho para pending_changes.json")
    parser.add_argument("--limit",     type=int, default=50, help="Máximo de linhas brutas por contrato")
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
    table_change = cs.get_table(args.table_id)
    if not table_change:
        print(json.dumps({"erro": f"Tabela {args.table_id!r} não encontrada"}, ensure_ascii=False))
        return 1

    # candidatos: D/I com contagens divergentes
    candidates = [
        c for c in table_change.contracts
        if c.action == "D/I" and c.simdnit_count != c.supra_count
    ]

    pairs = load_table_map()
    pair = next((p for p in pairs if p.supra_table.lower() == table_change.table_supra.lower()), None)
    if not pair:
        print(json.dumps({"erro": f"Mapeamento não encontrado para {table_change.table_supra}"}, ensure_ascii=False))
        return 1

    sg      = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    sim_ep  = simdnit_endpoint()
    mode    = pick_supra_mode()
    targets = supra_targets_for_mode(mode)

    results: list[dict] = []

    if candidates:
        with connect_endpoint(sim_ep) as src_conn, connect_endpoint(targets[0]) as dst_conn:
            for cc in candidates:
                try:
                    rd = diff_rows_for_contract(
                        src_conn.cursor(), dst_conn.cursor(), pair, cc.contract, sg
                    )
                except Exception as exc:
                    results.append({"contract": cc.contract, "erro": str(exc)})
                    continue

                if not rd.sim_raw:
                    continue

                sim_set   = set(rd.sim_raw)
                supra_set = set(rd.supra_raw)
                sim_dupes     = len(rd.sim_raw) - len(sim_set)
                genuinely_new = len(sim_set - supra_set)
                supra_only    = len(supra_set - sim_set)

                if sim_dupes > 0 and genuinely_new == 0 and supra_only == 0:
                    dupe_sample   = sorted(sim_set,    key=_row_sort_key)
                    sim_raw_sorted = sorted(rd.sim_raw, key=_row_sort_key)
                    results.append({
                        "contract":       cc.contract,
                        "simdnit_total":  len(rd.sim_raw),
                        "simdnit_unique": len(sim_set),
                        "simdnit_dupes":  sim_dupes,
                        "cols":           rd.cols,
                        "sim_raw":        [[_safe(v) for v in r] for r in sim_raw_sorted[: args.limit]],
                        "dupe_sample":    [[_safe(v) for v in r] for r in dupe_sample[:args.limit]],
                    })

    print(json.dumps({
        "table_id":        args.table_id,
        "table_supra":     table_change.table_supra,
        "table_simdnit":   pair.simdnit_table,
        "simdnit_host":    sim_ep.host,
        "simdnit_port":    sim_ep.port,
        "simdnit_database": sim_ep.database,
        "simdnit_label":   sim_ep.label,
        "total_candidates": len(candidates),
        "results":         results,
    }, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
