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
from supra_db_update.connection import connect_endpoint
from supra_db_update.differ import diff_rows_for_contract
from supra_db_update.table_map import load_table_map


def _safe(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float, bool)):
        return str(v)
    try:
        return str(v)
    except Exception:
        return repr(v)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diff JSON de contrato para a interface web.")
    parser.add_argument("--contract-id", required=True, help="ID do contrato no JSON (ex: C0001)")
    parser.add_argument("--limit", type=int, default=200, help="Máximo de linhas por seção (+/-)")
    args = parser.parse_args()

    load_env()

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

    result = {
        "contract_id":   args.contract_id,
        "contract":      contract_change.contract,
        "table_supra":   table_change.table_supra,
        "action":        contract_change.action,
        "cols":          rd.cols,
        "added":         [[_safe(v) for v in row] for row in rd.added[: args.limit]],
        "added_total":   len(rd.added),
        "removed":       [[_safe(v) for v in row] for row in rd.removed[: args.limit]],
        "removed_total": len(rd.removed),
        "common":        rd.common,
        "warning":       rd.warning,
        "error":         rd.error,
        "truncated":     len(rd.added) > args.limit or len(rd.removed) > args.limit,
    }

    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
