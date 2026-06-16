"""Comandos CLI: test-connections, validate, compare, sync, inspect, review, apply."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pymssql

from supra_db_update.change_queue import (
    ChangeSet,
    TableChange,
    build_changeset,
    load_changeset,
    save_changeset,
)
from supra_db_update.config import (
    UpdateMode,
    get_setting,
    load_env,
    pick_supra_mode,
    simdnit_endpoint,
    supra_targets_for_mode,
)
from supra_db_update.connection import connect_endpoint, test_connection
from supra_db_update.differ import ContractDiff, TableDiff, RowDiff, _build_scope_where, compare_table, diff_rows_for_contract
from supra_db_update.migrator import SyncResult, dedup_tables, stamp_injected_cols, sync_table
from supra_db_update.table_map import TablePair, _br, load_table_map
from supra_db_update.validators import (
    TableContract,
    _chunks as _val_chunks,
    _CHUNK as _VAL_CHUNK,
    _fetch_scope_contracts,
    load_table_contracts,
    rules_for_table,
    validate_before_migration,
    validate_table_contract,
)

import json as _json

log = logging.getLogger(__name__)

_COL_W = 46  # largura da coluna de nome de tabela na saída


def _emit_rule_details(data: dict) -> None:
    """Emite bloco JSON estruturado na última linha para parsing pelo frontend."""
    print(f"__RULE_DETAILS_JSON__:{_json.dumps(data, ensure_ascii=False, separators=(',', ':'))}")


# ---------------------------------------------------------------------------
# Helpers de apresentação
# ---------------------------------------------------------------------------

def _fmt(n: int) -> str:
    return f"{n:>10,}"


def _print_alert_details(details: dict) -> None:
    """Exibe amostra de contratos com regressão para alertas que falharam."""
    for col_key, col_info in details.items():
        if not isinstance(col_info, dict):
            continue
        amostra = col_info.get("amostra")
        if not amostra:
            continue
        total = col_info.get("contratos_com_regressao", len(amostra))
        print(f"      {col_key} — {len(amostra)} de {total:,} contrato(s) com regressão:")
        headers = list(amostra[0].keys())
        col_w = [max(len(h), 10) for h in headers]
        for row in amostra:
            for i, h in enumerate(headers):
                col_w[i] = min(40, max(col_w[i], len(str(row.get(h) or ""))))
        sep = "      +" + "+".join("-" * (w + 2) for w in col_w) + "+"
        def _row_line(vals):
            return "      |" + "|".join(
                f" {str(v or '')[:w].ljust(w)} " for v, w in zip(vals, col_w)
            ) + "|"
        print(sep)
        print(_row_line(headers))
        print(sep)
        for row in amostra:
            print(_row_line([row.get(h) for h in headers]))
        print(sep)


def _print_alert_results(results: list[dict]) -> None:
    if not results:
        return
    for r in results:
        status = " OK " if r.get("ok") else "FAIL"
        print(f"  [{status}] {r['message']}")
        if not r.get("ok"):
            _print_alert_details(r.get("details") or {})


def _print_count_breakdown(
    src_cur: "pymssql.Cursor",
    dst_cur: "pymssql.Cursor",
    contract: "TableContract",
    sg: str,
    sample: int = 50,
) -> None:
    """Mostra quais contratos têm SUPRA count > SIMDNIT count quando check-counts falha."""
    try:
        scope = _fetch_scope_contracts(src_cur, sg) if sg else []
        if not scope:
            return

        # contagem por contrato no SUPRA
        supra_by: dict[str, int] = {}
        for chunk in _val_chunks(scope, _VAL_CHUNK):
            ph = ",".join(["%s"] * len(chunk))
            dst_cur.execute(
                f"SELECT [{contract.join_supra}], COUNT(*) "
                f"FROM {contract.supra_table} "
                f"WHERE [{contract.join_supra}] IN ({ph}) "
                f"GROUP BY [{contract.join_supra}]",
                chunk,
            )
            for r in dst_cur.fetchall():
                supra_by[str(r[0])] = int(r[1])

        # contagem por contrato no SIMDNIT
        simdnit_by: dict[str, int] = {}
        for chunk in _val_chunks(scope, _VAL_CHUNK):
            ph = ",".join(["%s"] * len(chunk))
            src_cur.execute(
                f"SELECT [{contract.join_simdnit}], COUNT(*) "
                f"FROM {contract.simdnit_table} "
                f"WHERE [{contract.join_simdnit}] IN ({ph}) "
                f"GROUP BY [{contract.join_simdnit}]",
                chunk,
            )
            for r in src_cur.fetchall():
                simdnit_by[str(r[0])] = int(r[1])

        problematic = [
            {"contrato": cont, "simdnit": simdnit_by.get(cont, 0), "supra": sup_n,
             "diferenca": sup_n - simdnit_by.get(cont, 0)}
            for cont, sup_n in supra_by.items()
            if sup_n > simdnit_by.get(cont, 0)
        ]

        if not problematic:
            print("  (diferença total mas sem contrato individual com SUPRA > SIMDNIT"
                  " — possível variação de escopo ou duplicatas globais)")
            return

        problematic.sort(key=lambda x: (-x["diferenca"], x["contrato"]))
        total = len(problematic)
        amostra = problematic[:sample]

        print(f"  Contratos com SUPRA > SIMDNIT ({total} encontrado(s)"
              + (f" — exibindo {sample}" if total > sample else "") + "):")

        headers = ["contrato", "simdnit", "supra", "diferenca"]
        col_w = [max(len(h), 8) for h in headers]
        for row in amostra:
            for i, h in enumerate(headers):
                col_w[i] = min(35, max(col_w[i], len(str(row[h] if row[h] is not None else ""))))

        sep = "  +" + "+".join("-" * (w + 2) for w in col_w) + "+"
        def _rline(vals: list) -> str:
            return "  |" + "|".join(
                f" {str(v if v is not None else '')[:w].ljust(w)} "
                for v, w in zip(vals, col_w)
            ) + "|"

        print(sep)
        print(_rline(headers))
        print(sep)
        for row in amostra:
            print(_rline([row[h] for h in headers]))
        print(sep)

        if total > sample:
            print(f"  ... e mais {total - sample} contrato(s)")

    except Exception as exc:
        print(f"  [breakdown] Erro ao detalhar por contrato: {exc}")


def _print_diff_table(diffs: list[TableDiff]) -> None:
    header = f"{'Tabela SUPRA':<{_COL_W}} {'SIMDNIT':>10} {'SUPRA':>10} {'Δ':>8}  Status"
    print(header)
    print("-" * len(header))
    for d in diffs:
        sign = "+" if d.delta >= 0 else ""
        print(
            f"{d.pair.supra_table:<{_COL_W}}"
            f"{_fmt(d.simdnit_total)}"
            f"{_fmt(d.supra_total)}"
            f"  {sign}{d.delta:>6,}  {d.status_label}"
        )


_ACTION_LABELS = {
    "INSERT": "INSERT",
    "DELETE": "DELETE",
    "D/I":    "D/I   ",  # DELETE + INSERT
    "UPDATE": "UPDATE",
    "OK":     "OK    ",
}

_LEGEND = (
    "  Legenda: INSERT=novo contrato  DELETE=removido  "
    "D/I=contagens divergem (DELETE+INSERT)  UPDATE=valores alterados (--deep)"
)

_MAX_DETAIL_ROWS = 50  # máximo de contratos exibidos por tabela no --detail


def _print_diff_detail(diffs: list[TableDiff]) -> None:
    """Detalhe por contrato para cada tabela com diferença."""
    any_diff = False
    for d in diffs:
        changed = d.active_changed
        protected_changed = d.protected_changed
        if not changed and not protected_changed:
            continue
        any_diff = True
        sign = "+" if d.delta >= 0 else ""
        print(f"\n[DIFF {sign}{d.delta:,}]  {d.pair.supra_table}")

        # contagem por tipo de ação
        action_counts: dict[str, int] = {}
        for c in changed:
            action_counts[c.action] = action_counts.get(c.action, 0) + 1
        summary = "  ".join(f"{v}x {k}" for k, v in sorted(action_counts.items()))
        print(f"  {summary}")

        # lista de contratos a sincronizar (limitado)
        shown = changed[:_MAX_DETAIL_ROWS]
        for c in shown:
            lbl = _ACTION_LABELS.get(c.action, c.action)
            print(
                f"    {lbl}  {c.contract:<30}"
                f"  SIM={c.simdnit_count:>6,}"
                f"  SUPRA={c.supra_count:>6,}"
            )
        if len(changed) > _MAX_DETAIL_ROWS:
            print(f"    ... e mais {len(changed) - _MAX_DETAIL_ROWS} contratos")

        # contratos protegidos com diferença
        if protected_changed:
            print(f"  Contratos protegidos com diferença (NÃO serão alterados):")
            for c in protected_changed[:20]:
                lbl = _ACTION_LABELS.get(c.action, c.action)
                print(
                    f"    [PROTEGIDO]  {c.contract:<30}"
                    f"  SIM={c.simdnit_count:>6,}"
                    f"  SUPRA={c.supra_count:>6,}"
                    f"  → seria: {c.action}"
                )
            if len(protected_changed) > 20:
                print(f"    ... e mais {len(protected_changed) - 20} protegidos")

    if any_diff:
        print(_LEGEND)
    else:
        print("\nNenhuma diferença de contrato detectada.")


def _select_tables(diffs: list[TableDiff], force: bool) -> list[TableDiff]:
    """Apresenta menu e retorna os TableDiffs escolhidos pelo utilizador."""
    if force:
        return diffs

    candidates = [d for d in diffs if d.needs_sync]
    if not candidates:
        print("\nNenhuma tabela com diferenças detectadas.")
        return []

    print(f"\nTabelas com diferenças ({len(candidates)}):")
    for i, d in enumerate(candidates, 1):
        print(f"  [{i}] {d.pair.supra_table}  →  {d.status_label}")

    print("\nSelecione tabelas para sincronizar:")
    print("  • Números separados por vírgula: 1,3")
    print("  • all  — sincronizar todas")
    print("  • Enter — cancelar")
    raw = input("\n> ").strip().lower()

    if not raw:
        print("Cancelado.")
        return []
    if raw == "all":
        return candidates

    chosen = []
    for tok in raw.split(","):
        tok = tok.strip()
        if tok.isdigit():
            idx = int(tok) - 1
            if 0 <= idx < len(candidates):
                chosen.append(candidates[idx])
    return chosen


# ---------------------------------------------------------------------------
# cmd_test_connections
# ---------------------------------------------------------------------------

def cmd_test_connections() -> int:
    load_env()
    mode = pick_supra_mode()
    sim = simdnit_endpoint()
    targets = supra_targets_for_mode(mode)

    ok, msg = test_connection(sim)
    print(f"SIMDNIT ({sim.database}@{sim.host}): {'OK' if ok else msg}")
    if not ok:
        return 1

    for ep in targets:
        o, m = test_connection(ep)
        print(f"{ep.label} ({ep.database}@{ep.host}): {'OK' if o else m}")
        if not o:
            return 1
    return 0


# ---------------------------------------------------------------------------
# cmd_validate_tables
# ---------------------------------------------------------------------------

def run_validations_only(
    src: pymssql.Cursor,
    dest_conn: pymssql.Connection,
    dest_label: str,
    tables: list[str],
    contracts_path: Path | None,
) -> bool:
    contracts = load_table_contracts(contracts_path)
    ok_all = True
    dest_cur = dest_conn.cursor()
    try:
        for qualified in tables:
            rules = rules_for_table(contracts, qualified)
            if not rules:
                log.info("%s — %s sem regras em import_rules.json; a ignorar.", dest_label, qualified)
                continue
            res = validate_before_migration(src, dest_cur, qualified, rules)
            log.info("[%s] %s", dest_label, res.message)
            if not res.ok:
                ok_all = False
    finally:
        dest_cur.close()
    return ok_all


def collect_alerts(
    src_cur: pymssql.Cursor,
    dst_conn: pymssql.Connection,
    contracts_path: Path | None = None,
    sg: str = "",
) -> list[dict]:
    """Coleta resultados de alertas como lista de dicts (para persistir no JSON)."""
    contracts = load_table_contracts(contracts_path)
    if not contracts:
        return []
    results: list[dict] = []
    dst_cur = dst_conn.cursor()
    try:
        for contract in contracts.values():
            for res in validate_table_contract(src_cur, dst_cur, contract, sg=sg):
                results.append({
                    "ok": res.ok,
                    "table": contract.supra_table,
                    "message": res.message,
                    "details": res.details,
                })
    finally:
        dst_cur.close()
    return results


def run_pre_migration_alerts(
    src_cur: pymssql.Cursor,
    dst_conn: pymssql.Connection,
    ep_label: str,
    contracts_path: Path | None = None,
    sg: str = "",
) -> bool:
    """Executa e imprime todos os alertas. Retorna True se nenhum falhou."""
    results = collect_alerts(src_cur, dst_conn, contracts_path, sg=sg)
    if not results:
        return True

    print(f"\nAlertas pré-migração ({ep_label}):")
    _print_alert_results(results)
    all_ok = all(r.get("ok", True) for r in results)

    if not all_ok:
        print(
            "\n[BLOQUEADO] Um ou mais alertas falharam. "
            "Corrija os dados no SIMDNIT antes de migrar."
        )
    return all_ok


def cmd_validate_tables(tables: list[str], contracts: Path | None) -> int:
    load_env()
    mode = pick_supra_mode()
    sim_ep = simdnit_endpoint()
    targets = supra_targets_for_mode(mode)

    with connect_endpoint(sim_ep) as src_conn:
        src_cur = src_conn.cursor()
        try:
            exit_code = 0
            for ep in targets:
                with connect_endpoint(ep) as dst_conn:
                    if not run_validations_only(src_cur, dst_conn, ep.label, tables, contracts):
                        exit_code = 1
            return exit_code
        finally:
            src_cur.close()


def cmd_alerts(contracts_path: Path | None) -> int:
    """Executa todos os alertas de segurança configurados. Não altera nenhum dado."""
    load_env()
    sg   = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    mode = pick_supra_mode()
    sim_ep = simdnit_endpoint()
    targets = supra_targets_for_mode(mode)

    exit_code = 0
    with connect_endpoint(sim_ep) as src_conn:
        src_cur = src_conn.cursor()
        try:
            for ep in targets:
                print(f"\n{'='*70}")
                print(f"Origem : {sim_ep.label} ({sim_ep.database}@{sim_ep.host})")
                print(f"Destino: {ep.label} ({ep.database}@{ep.host})")
                print(f"{'='*70}")
                with connect_endpoint(ep) as dst_conn:
                    if not run_pre_migration_alerts(src_cur, dst_conn, ep.label, contracts_path, sg=sg):
                        exit_code = 1
        finally:
            src_cur.close()

    if exit_code == 0:
        print("\nTodos os alertas passaram. Migração liberada.")
    return exit_code


# ---------------------------------------------------------------------------
# cmd_check_counts  —  count check for one SUPRA table
# ---------------------------------------------------------------------------

def cmd_check_counts(supra_table: str) -> int:
    load_env()
    sg        = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    contracts = load_table_contracts()
    contract  = contracts.get(supra_table)
    if contract is None:
        print(f"Tabela {supra_table!r} não encontrada em import_rules.json (table_contracts).")
        return 1

    sim_ep  = simdnit_endpoint()
    targets = supra_targets_for_mode(pick_supra_mode())

    exit_code = 0
    with connect_endpoint(sim_ep) as src_conn:
        src_cur = src_conn.cursor()
        try:
            for ep in targets:
                with connect_endpoint(ep) as dst_conn:
                    dst_cur = dst_conn.cursor()
                    try:
                        from supra_db_update.validators import _check_source_row_count_gte_target
                        res = _check_source_row_count_gte_target(src_cur, dst_cur, contract, sg=sg)
                        status = "OK" if res.ok else "FAIL"
                        print(f"[{status}] {res.message}")
                        if not res.ok:
                            exit_code = 1
                            _print_count_breakdown(src_cur, dst_cur, contract, sg)
                    finally:
                        dst_cur.close()
        finally:
            src_cur.close()
    return exit_code


# ---------------------------------------------------------------------------
# cmd_check_date_regression  —  1900-date regression check for all tables
# ---------------------------------------------------------------------------

def cmd_check_date_regression() -> int:
    load_env()
    contracts = load_table_contracts()
    if not contracts:
        print("Nenhuma tabela configurada em import_rules.json.")
        return 0

    sim_ep = simdnit_endpoint()
    targets = supra_targets_for_mode(pick_supra_mode())

    exit_code = 0
    details_for_json: list[dict] = []
    with connect_endpoint(sim_ep) as src_conn:
        src_cur = src_conn.cursor()
        try:
            for ep in targets:
                with connect_endpoint(ep) as dst_conn:
                    dst_cur = dst_conn.cursor()
                    try:
                        from supra_db_update.validators import _check_no_1900_date_regression
                        for contract in contracts.values():
                            if "no_1900_date_regression" not in contract.rules:
                                continue
                            res = _check_no_1900_date_regression(src_cur, dst_cur, contract)
                            status = "OK" if res.ok else "FAIL"
                            print(f"[{status}] {res.message}")
                            if not res.ok:
                                exit_code = 1
                                _print_alert_details(res.details)
                                for col_det in res.details.values():
                                    if isinstance(col_det, dict) and col_det.get("amostra"):
                                        details_for_json.append({
                                            "table_supra":   col_det.get("table_supra", contract.supra_table),
                                            "col_supra":     col_det.get("col_supra", ""),
                                            "col_simdnit":   col_det.get("col_simdnit", ""),
                                            "contratos_com_regressao": col_det.get("contratos_com_regressao", 0),
                                            "amostra":       col_det.get("amostra", []),
                                        })
                    finally:
                        dst_cur.close()
        finally:
            src_cur.close()
    if details_for_json:
        _emit_rule_details({"type": "date_regression", "regressoes": details_for_json})
    return exit_code


# ---------------------------------------------------------------------------
# cmd_check_contract_values  —  arithmetic check on Dados_Contrato (SIMDNIT)
# ---------------------------------------------------------------------------

def cmd_check_contract_values() -> int:
    load_env()
    sg     = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    sim_ep = simdnit_endpoint()

    with connect_endpoint(sim_ep) as conn:
        cur = conn.cursor()
        try:
            cur.execute(
                "SELECT [NU_CON_FORMATADO],"
                " [VALOR_INICIAL], [VALOR_TOTAL_DE_REAJUSTE],"
                " [VALOR_TOTAL_DE_ADITIVOS], [VALOR_INICIAL_ADIT_REAJUSTES]"
                " FROM [dbo].[Dados_Contrato]"
                " WHERE [SG_UND_GESTORA] = %s"
                "   AND [VALOR_INICIAL_ADIT_REAJUSTES] IS NOT NULL"
                "   AND ABS([VALOR_INICIAL] + [VALOR_TOTAL_DE_REAJUSTE]"
                "       + [VALOR_TOTAL_DE_ADITIVOS]"
                "       - [VALOR_INICIAL_ADIT_REAJUSTES]) > 0.01",
                (sg,),
            )
            rows = cur.fetchall()
        finally:
            cur.close()

    if not rows:
        print("OK dbo.Dados_Contrato: aritmética de valores consistente.")
        return 0

    print(f"FAIL dbo.Dados_Contrato: {len(rows)} contrato(s) com aritmética de valores inconsistente.")
    print(f"  (esperado: VALOR_INICIAL + VALOR_TOTAL_DE_REAJUSTE + VALOR_TOTAL_DE_ADITIVOS = VALOR_INICIAL_ADIT_REAJUSTES)")
    header = ["contrato", "val_inicial", "val_reajuste", "val_aditivo", "val_ini_adit_reajuste"]
    col_w = [max(len(h), 10) for h in header]
    for r in rows[:20]:
        for i, v in enumerate(r):
            col_w[i] = min(40, max(col_w[i], len(str(v) if v is not None else "")))
    sep = "  +" + "+".join("-" * (w + 2) for w in col_w) + "+"
    def _row_line(vals):
        return "  |" + "|".join(f" {str(v or '')[:w].ljust(w)} " for v, w in zip(vals, col_w)) + "|"
    print(sep)
    print(_row_line(header))
    print(sep)
    for r in rows[:20]:
        print(_row_line(r))
    print(sep)
    if len(rows) > 20:
        print(f"  ... e mais {len(rows) - 20} contrato(s)")
    _emit_rule_details({
        "type": "contract_values",
        "total_contratos": len(rows),
        "contratos": [
            {
                "contrato":             str(r[0] or ""),
                "val_inicial":          str(r[1]) if r[1] is not None else "",
                "val_reajuste":         str(r[2]) if r[2] is not None else "",
                "val_aditivo":          str(r[3]) if r[3] is not None else "",
                "val_ini_adit_reajuste": str(r[4]) if r[4] is not None else "",
            }
            for r in rows
        ],
    })
    return 1


# ---------------------------------------------------------------------------
# cmd_check_data_integrity  —  per-contract row count for 3 tables
# ---------------------------------------------------------------------------

_INTEGRITY_TABLE_PAIRS = [
    ("dbo.TB_SIAC_EMPENHO",       "dbo.Dados_Empenho",  "contrato", "NU_CON_FORMATADO"),
    ("dbo.TB_SIAC_MEDICAO_MAIOR", "dbo.Dados_Medicao",  "contrato", "NU_CON_FORMATADO"),
    ("dbo.TB_SIAC_REAJUSTE",      "dbo.Dados_Reajuste", "contrato", "Contrato"),
]

_CHUNK = 900


def _check_integrity_table(
    src_cur: pymssql.Cursor,
    dst_cur: pymssql.Cursor,
    supra_table: str,
    simdnit_table: str,
    join_supra: str,
    join_simdnit: str,
) -> bool:
    s_schema, s_tbl = supra_table.split(".", 1)
    m_schema, m_tbl = simdnit_table.split(".", 1)

    dst_cur.execute(
        f"SELECT [{join_supra}], COUNT(*) FROM [{s_schema}].[{s_tbl}]"
        f" GROUP BY [{join_supra}]"
    )
    supra_counts: dict[str, int] = {str(r[0]): int(r[1]) for r in dst_cur.fetchall()}

    if not supra_counts:
        print(f"OK {supra_table}: sem dados no SUPRA para verificar.")
        return True

    simdnit_counts: dict[str, int] = {}
    contracts = list(supra_counts.keys())
    for i in range(0, len(contracts), _CHUNK):
        chunk = contracts[i:i + _CHUNK]
        ph = ",".join(["%s"] * len(chunk))
        src_cur.execute(
            f"SELECT [{join_simdnit}], COUNT(*) FROM [{m_schema}].[{m_tbl}]"
            f" WHERE [{join_simdnit}] IN ({ph})"
            f" GROUP BY [{join_simdnit}]",
            chunk,
        )
        for r in src_cur.fetchall():
            simdnit_counts[str(r[0])] = int(r[1])

    issues = [
        {"contrato": c, "linhas_supra": n, "linhas_simdnit": simdnit_counts.get(c, 0)}
        for c, n in supra_counts.items()
        if simdnit_counts.get(c, 0) < n
    ]

    if not issues:
        print(f"OK {supra_table}: integridade verificada ({len(supra_counts):,} contratos).")
        return True

    print(f"ALERTA {supra_table}: {len(issues)} contrato(s) com menos linhas no SIMDNIT que no SUPRA.")
    for item in issues[:20]:
        print(
            f"  {item['contrato']}: SUPRA={item['linhas_supra']}"
            f" SIMDNIT={item['linhas_simdnit']}"
        )
    if len(issues) > 20:
        print(f"  ... e mais {len(issues) - 20} contrato(s)")
    return False


# cmd_check_reajuste_null_date — alerta quando SIMDNIT tem reajustes sem data de assinatura
# ---------------------------------------------------------------------------

def cmd_check_reajuste_null_date() -> int:
    load_env()
    sg = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    sim_ep = simdnit_endpoint()

    with connect_endpoint(sim_ep) as src_conn:
        cur = src_conn.cursor()
        cur.execute(
            """
            SELECT r.Contrato, COUNT(*) AS cnt
            FROM dbo.Dados_Reajuste r
            WHERE r.Contrato IN (
                SELECT NU_CON_FORMATADO FROM dbo.Dados_Contrato WHERE SG_UND_GESTORA = %s
            )
              AND r.Data_da_Assinatura_do_Reajuste IS NULL
            GROUP BY r.Contrato
            ORDER BY r.Contrato
            """,
            (sg,),
        )
        issues = cur.fetchall()
        cur.close()

    if not issues:
        print(f"OK dbo.Dados_Reajuste: nenhum reajuste com data de assinatura nula (escopo {sg}).")
        return 0

    total_rows = sum(int(r[1]) for r in issues)
    print(
        f"ALERTA dbo.Dados_Reajuste: {total_rows:,} linha(s) com "
        f"Data_da_Assinatura_do_Reajuste IS NULL em {len(issues):,} contrato(s) "
        f"(escopo {sg}). Estas linhas serão inseridas no SUPRA sem data de assinatura."
    )
    for contract, cnt in issues:
        print(f"  {contract}: {cnt} linha(s)")
    _emit_rule_details({
        "type": "null_column",
        "table": "dbo.Dados_Reajuste",
        "column": "Data_da_Assinatura_do_Reajuste",
        "total_rows": total_rows,
        "total_contratos": len(issues),
        "contratos": [{"contrato": str(r[0]), "linhas": int(r[1])} for r in issues],
    })
    return 1


# cmd_check_empenho_null_nota — alerta quando SIMDNIT tem empenhos sem Nota_de_Empenho
# ---------------------------------------------------------------------------

def cmd_check_empenho_null_nota() -> int:
    load_env()
    sg = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    sim_ep = simdnit_endpoint()

    with connect_endpoint(sim_ep) as src_conn:
        cur = src_conn.cursor()
        cur.execute(
            """
            SELECT e.NU_CON_FORMATADO, COUNT(*) AS cnt
            FROM dbo.Dados_Empenho e
            WHERE e.NU_CON_FORMATADO IN (
                SELECT NU_CON_FORMATADO FROM dbo.Dados_Contrato WHERE SG_UND_GESTORA = %s
            )
              AND e.NU_EMPENHO IS NULL
            GROUP BY e.NU_CON_FORMATADO
            ORDER BY e.NU_CON_FORMATADO
            """,
            (sg,),
        )
        issues = cur.fetchall()
        cur.close()

    if not issues:
        print(f"OK dbo.Dados_Empenho: nenhum empenho com NU_EMPENHO nulo (escopo {sg}).")
        return 0

    total_rows = sum(int(r[1]) for r in issues)
    print(
        f"ALERTA dbo.Dados_Empenho: {total_rows:,} linha(s) com "
        f"NU_EMPENHO IS NULL em {len(issues):,} contrato(s) "
        f"(escopo {sg}). Estas linhas serão inseridas no SUPRA sem nota de empenho."
    )
    for contract, cnt in issues:
        print(f"  {contract}: {cnt} linha(s)")
    _emit_rule_details({
        "type": "null_column",
        "table": "dbo.Dados_Empenho",
        "column": "NU_EMPENHO",
        "total_rows": total_rows,
        "total_contratos": len(issues),
        "contratos": [{"contrato": str(r[0]), "linhas": int(r[1])} for r in issues],
    })
    return 1


# ---------------------------------------------------------------------------
# Handlers genéricos — dirigidos por params do import_rules.json
# ---------------------------------------------------------------------------

def _generic_null_column(
    params: dict, sg: str, src_cur: "pymssql.Cursor"
) -> "tuple[int, dict | None, str]":
    """Verifica coluna IS NULL em tabela SIMDNIT, escopada por SG_UND_GESTORA."""
    simdnit_table = params.get("simdnit_table", "")
    column        = params.get("column", "")
    join_col      = params.get("join_col", "NU_CON_FORMATADO")
    scope_table   = params.get("scope_table", "dbo.Dados_Contrato")
    scope_col     = params.get("scope_col", "SG_UND_GESTORA")

    if not simdnit_table or not column:
        return 1, None, "ERRO: params insuficientes (simdnit_table e column obrigatórios)"

    _, scope_tbl = scope_table.split(".", 1)
    lines: list[str] = []
    try:
        src_cur.execute(
            f"SELECT t.[{join_col}], COUNT(*) AS cnt"
            f" FROM {simdnit_table} t"
            f" WHERE t.[{join_col}] IN ("
            f"   SELECT [NU_CON_FORMATADO] FROM [dbo].[{scope_tbl}]"
            f"   WHERE [{scope_col}] = %s"
            f" ) AND t.[{column}] IS NULL"
            f" GROUP BY t.[{join_col}]"
            f" ORDER BY t.[{join_col}]",
            (sg,),
        )
        issues = src_cur.fetchall()
    except Exception as exc:
        return 1, None, f"ERRO ao consultar {simdnit_table}: {exc}"

    if not issues:
        lines.append(f"OK {simdnit_table}: nenhuma linha com {column} nulo (escopo {sg}).")
        return 0, None, "\n".join(lines)

    total_rows = sum(int(r[1]) for r in issues)
    lines.append(
        f"ALERTA {simdnit_table}: {total_rows:,} linha(s) com {column} IS NULL"
        f" em {len(issues):,} contrato(s) (escopo {sg})."
    )
    for contract, cnt in issues:
        lines.append(f"  {contract}: {cnt} linha(s)")

    details = {
        "type":            "null_column",
        "table":           simdnit_table,
        "column":          column,
        "total_rows":      total_rows,
        "total_contratos": len(issues),
        "contratos":       [{"contrato": str(r[0]), "linhas": int(r[1])} for r in issues],
    }
    return 1, details, "\n".join(lines)


def _generic_arithmetic(
    params: dict, sg: str, src_cur: "pymssql.Cursor"
) -> "tuple[int, dict | None, str]":
    """Verifica colA + colB + ... = result_col na tabela SIMDNIT."""
    simdnit_table = params.get("simdnit_table", "")
    contract_col  = params.get("contract_col", "NU_CON_FORMATADO")
    scope_col     = params.get("scope_col", "")
    operands      = params.get("operands", [])
    result_col    = params.get("result", "")
    tolerance     = float(params.get("tolerance", 0.01))

    if not simdnit_table or not operands or not result_col:
        return 1, None, "ERRO: params insuficientes (simdnit_table, operands, result obrigatórios)"

    lines: list[str] = []
    sum_expr = " + ".join(f"[{c}]" for c in operands)
    cols_str = ", ".join(f"[{c}]" for c in operands)
    formula  = " + ".join(operands) + f" = {result_col}"

    tol_str = repr(float(tolerance))  # ex: '0.01' — embute direto no SQL evitando coerção
    try:
        if scope_col and sg:
            src_cur.execute(
                f"SELECT [{contract_col}], {cols_str}, [{result_col}]"
                f" FROM {simdnit_table}"
                f" WHERE [{scope_col}] = %s"
                f"   AND [{result_col}] IS NOT NULL"
                f"   AND ABS({sum_expr} - [{result_col}]) > {tol_str}",
                (sg,),
            )
        else:
            src_cur.execute(
                f"SELECT [{contract_col}], {cols_str}, [{result_col}]"
                f" FROM {simdnit_table}"
                f" WHERE [{result_col}] IS NOT NULL"
                f"   AND ABS({sum_expr} - [{result_col}]) > {tol_str}",
            )
        rows = src_cur.fetchall()
    except Exception as exc:
        return 1, {"type": "arithmetic", "error": str(exc)}, f"ERRO ao consultar {simdnit_table}: {exc}"

    if not rows:
        lines.append(f"OK {simdnit_table}: aritmética consistente ({formula}).")
        return 0, None, "\n".join(lines)

    lines.append(f"FAIL {simdnit_table}: {len(rows)} contrato(s) com aritmética inconsistente.")
    lines.append(f"  (esperado: {formula})")
    for r in rows[:20]:
        lines.append("  " + " | ".join(str(v) if v is not None else "NULL" for v in r))
    if len(rows) > 20:
        lines.append(f"  ... e mais {len(rows) - 20} contrato(s)")

    all_cols = [contract_col] + list(operands) + [result_col]
    details = {
        "type":            "arithmetic",
        "table":           simdnit_table,
        "formula":         formula,
        "operands":        list(operands),
        "result":          result_col,
        "contract_col":    contract_col,
        "total_contratos": len(rows),
        "contratos": [
            dict(zip(all_cols, [str(v) if v is not None else "" for v in r]))
            for r in rows
        ],
    }
    return 1, details, "\n".join(lines)


def _generic_row_count_per_contract(
    params: dict,
    src_cur: "pymssql.Cursor",
    dst_cur: "pymssql.Cursor",
) -> "tuple[int, dict | None, str]":
    """Por contrato: SIMDNIT deve ter >= linhas que SUPRA para cada par de tabelas."""
    pairs = params.get("pairs", [])
    if not pairs:
        return 1, None, "ERRO: params insuficientes (pairs obrigatório)"

    all_ok = True
    for pair in pairs:
        supra_t   = pair.get("supra_table", "")
        simdnit_t = pair.get("simdnit_table", "")
        j_supra   = pair.get("join_supra", "contrato")
        j_sim     = pair.get("join_simdnit", "NU_CON_FORMATADO")
        if not supra_t or not simdnit_t:
            continue
        if not _check_integrity_table(src_cur, dst_cur, supra_t, simdnit_t, j_supra, j_sim):
            all_ok = False

    return (0 if all_ok else 1), None, ""


def _run_typed_rule(rule_type: str, params: dict) -> "tuple[int, dict | None, str]":
    """Abre conexões e despacha para o handler genérico correto."""
    from supra_db_update._paths import runtime_root  # noqa: F401 (já importado localmente)
    sg     = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    sim_ep = simdnit_endpoint()

    if rule_type == "null_column":
        with connect_endpoint(sim_ep) as src_conn:
            cur = src_conn.cursor()
            try:
                return _generic_null_column(params, sg, cur)
            finally:
                cur.close()

    if rule_type == "arithmetic":
        with connect_endpoint(sim_ep) as src_conn:
            cur = src_conn.cursor()
            try:
                return _generic_arithmetic(params, sg, cur)
            finally:
                cur.close()

    if rule_type == "row_count_per_contract":
        targets = supra_targets_for_mode(pick_supra_mode())
        with connect_endpoint(sim_ep) as src_conn:
            src_cur = src_conn.cursor()
            try:
                with connect_endpoint(targets[0]) as dst_conn:
                    dst_cur = dst_conn.cursor()
                    try:
                        return _generic_row_count_per_contract(params, src_cur, dst_cur)
                    finally:
                        dst_cur.close()
            finally:
                src_cur.close()

    return 1, None, f"ERRO: tipo de regra desconhecido: {rule_type!r}"


# ---------------------------------------------------------------------------
def cmd_run_all() -> int:
    """Executa todas as regras habilitadas em import_rules.json, depois compare --detail.

    Formato de saída (lido pelo PHP getValidarStatus):
        RULE:<id>:<ok|erro>:<output_linha1>\\n...
        RULES_DONE
        <saída normal do compare --detail>
        __EXIT_CODE__:<N>          ← adicionado pelo wrapper shell no PHP
    """
    from supra_db_update._paths import runtime_root
    rules_path = runtime_root() / "import_rules.json"

    rules: list[dict] = []
    if rules_path.is_file():
        with rules_path.open(encoding="utf-8") as f:
            raw = __import__("json").load(f)
        rules = (raw.get("rules") if isinstance(raw, dict) else raw) or []

    # Pré-dedup: remove cópias exatas do SUPRA antes das regras de contagem,
    # evitando falsos positivos causados por linhas duplicadas acumuladas.
    print("Verificando e removendo duplicatas exatas das tabelas SUPRA...", flush=True)
    try:
        load_env()
        _targets = supra_targets_for_mode(pick_supra_mode())
        _all_pairs = load_table_map(None)
        if _targets and _all_pairs:
            for _ep in _targets:
                with connect_endpoint(_ep) as _dst:
                    dedup_tables(_dst, _all_pairs)
    except Exception as _e:
        print(f"  [dedup] Aviso: não foi possível deduplicar ({_e})", flush=True)

    dir_path = str(runtime_root())
    overall_ok = True
    rule_alerts_for_json: list[dict] = []

    import subprocess
    import shlex

    # inclui regras com command (tipo "command") ou com type genérico (null_column, arithmetic…)
    enabled_rules = [
        r for r in rules
        if r.get("enabled") is not False
        and (r.get("command", "").strip() or r.get("type", "command") not in ("command", ""))
    ]
    total = len(enabled_rules)

    for idx, rule in enumerate(enabled_rules, 1):
        rule_id   = rule.get("id", "rule")
        rule_name = rule.get("name", rule_id)
        rule_type = rule.get("type", "command")

        print(f"[Regra {idx}/{total}] {rule_name}...", flush=True)

        if rule_type not in ("command", ""):
            # ── handler genérico inline (sem subprocess) ──────────────────
            try:
                exit_code_r, details, text_out = _run_typed_rule(rule_type, rule.get("params", {}))
                status  = "ok" if exit_code_r == 0 else "erro"
                raw_out = text_out
                output  = text_out.strip().replace("\n", "\\n")
            except Exception as exc:
                status  = "erro"
                # preserva erro como details para que apareça no card de alerta
                details = {"type": rule_type, "error": str(exc)}
                raw_out = f"ERRO interno na regra ({rule_type}): {exc}"
                output  = raw_out.replace("\n", "\\n")

            if status != "ok":
                overall_ok = False
                if details is not None:
                    rule_alerts_for_json.append({
                        "rule_id":   rule_id,
                        "rule_name": rule_name,
                        "details":   details,
                    })
        else:
            # ── subprocess (comportamento anterior) ───────────────────────
            sub_cmd = rule.get("command", "").strip()
            try:
                if getattr(sys, "frozen", False):
                    argv = [sys.executable] + shlex.split(sub_cmd)
                else:
                    argv = [sys.executable, "-m", "supra_db_update"] + shlex.split(sub_cmd)
                result = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    cwd=dir_path,
                )
                status  = "ok" if result.returncode == 0 else "erro"
                raw_out = result.stdout + result.stderr
                output  = raw_out.strip().replace("\n", "\\n")
            except Exception as exc:
                status  = "erro"
                raw_out = str(exc)
                output  = raw_out.replace("\n", "\\n")

            if status != "ok":
                overall_ok = False
                details = None
                for line in raw_out.splitlines():
                    if line.startswith("__RULE_DETAILS_JSON__:"):
                        try:
                            details = _json.loads(line[len("__RULE_DETAILS_JSON__:"):])
                        except Exception:
                            pass
                        break
                if details is not None:
                    rule_alerts_for_json.append({
                        "rule_id":   rule_id,
                        "rule_name": rule_name,
                        "details":   details,
                    })

        print(f"RULE:{rule_id}:{status}:{output}", flush=True)

    print("RULES_DONE", flush=True)
    print("Iniciando comparação SIMDNIT↔SUPRA...", flush=True)

    # Executa o compare --detail (saída inline)
    cmp_code = cmd_compare(tables=[], mapping=None, deep=False, detail=True)

    # Post-processa pending_changes.json para adicionar rule_alerts (alertas de regras)
    json_path = runtime_root() / "pending_changes.json"
    if json_path.is_file():
        try:
            with json_path.open(encoding="utf-8") as f:
                pj = _json.load(f)
            pj["rule_alerts"] = rule_alerts_for_json
            with json_path.open("w", encoding="utf-8") as f:
                _json.dump(pj, f, ensure_ascii=False)
        except Exception:
            pass

    return 0 if (overall_ok and cmp_code == 0) else 1


def cmd_check_data_integrity() -> int:
    load_env()
    sim_ep = simdnit_endpoint()
    targets = supra_targets_for_mode(pick_supra_mode())

    exit_code = 0
    with connect_endpoint(sim_ep) as src_conn:
        src_cur = src_conn.cursor()
        try:
            for ep in targets:
                with connect_endpoint(ep) as dst_conn:
                    dst_cur = dst_conn.cursor()
                    try:
                        for args in _INTEGRITY_TABLE_PAIRS:
                            if not _check_integrity_table(src_cur, dst_cur, *args):
                                exit_code = 1
                    finally:
                        dst_cur.close()
        finally:
            src_cur.close()
    return exit_code


# ---------------------------------------------------------------------------
# cmd_compare
# ---------------------------------------------------------------------------

def cmd_compare(
    tables: list[str],
    mapping: Path | None,
    deep: bool,
    detail: bool,
) -> int:
    load_env()
    sg = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    mode = pick_supra_mode()
    sim_ep = simdnit_endpoint()
    targets = supra_targets_for_mode(mode)
    all_pairs = load_table_map(mapping)

    if tables:
        tl = {t.lower() for t in tables}
        all_pairs = [p for p in all_pairs if p.supra_table.lower() in tl]

    if not all_pairs:
        print("Nenhuma tabela sincronizável encontrada com os filtros indicados.")
        return 1

    exit_code = 0
    with connect_endpoint(sim_ep) as src_conn:
        src_cur = src_conn.cursor()
        for ep in targets:
            print(f"\n{'='*70}")
            print(f"Origem : {sim_ep.label} ({sim_ep.database}@{sim_ep.host})")
            print(f"Destino: {ep.label} ({ep.database}@{ep.host})")
            print(f"{'='*70}")
            with connect_endpoint(ep) as dst_conn:
                dst_cur = dst_conn.cursor()
                diffs: list[TableDiff] = []
                for pair in all_pairs:
                    print(f"  Comparando {pair.supra_table}...", end="\r", flush=True)
                    d = compare_table(src_cur, dst_cur, pair, sg, deep=deep)
                    diffs.append(d)

                print(" " * 70)
                _print_diff_table(diffs)

                needs = [d for d in diffs if d.needs_sync]
                errors = [d for d in diffs if d.error]
                warns = [d for d in diffs if d.warning]
                n_ok = len(diffs) - len(needs) - len(errors) - len(warns)
                print(f"\nTabelas OK: {n_ok}")
                print(f"Tabelas com diferença: {len(needs)}")
                if warns:
                    print(f"Tabelas com aviso (escopo vazio): {len(warns)}")
                    exit_code = 1

                # causa-raiz: Dados_Contrato vazia impede comparar as filhas
                dados_contrato_diff = next(
                    (d for d in warns if d.pair.simdnit_table.lower() == "dbo.dados_contrato"),
                    None,
                )
                if dados_contrato_diff:
                    supra_n = dados_contrato_diff.supra_total
                    print(
                        f"\n[CAUSA RAIZ] dbo.Dados_Contrato está vazia para SG={sg!r}.\n"
                        f"  SUPRA tem {supra_n:,} linha(s) em TB_SIAC_CONTRATO.\n"
                        "  As demais tabelas dependem de Dados_Contrato para filtrar contratos\n"
                        "  e não foram comparadas — seus valores no SUPRA são desconhecidos.\n"
                        "  Repopule dbo.Dados_Contrato e execute novamente."
                    )

                if errors:
                    print(f"Tabelas com erro: {len(errors)}")
                    exit_code = 1

                if detail:
                    _print_diff_detail(diffs)
                elif needs:
                    print(
                        "\nDica: use 'compare --detail' para ver quais contratos "
                        "serão afetados em cada tabela."
                    )

                # ── alertas de segurança ───────────────────────────────────
                print("\nColetando alertas de segurança...")
                alert_results = collect_alerts(src_cur, dst_conn, sg=sg)
                _print_alert_results(alert_results)

                # ── gera fila de pendências ────────────────────────────────
                if needs:
                    cs = build_changeset(diffs, ep.label, sg)
                    cs.alerts = alert_results
                    if len(targets) > 1:
                        safe = ep.label.replace(" ", "_").replace("/", "-")
                        out_path = Path(f"pending_changes_{safe}.json")
                    else:
                        out_path = Path("pending_changes.json")
                    save_changeset(cs, out_path)
                    blocked = cs.alerts_blocked
                    print(
                        f"\nPendências salvas em: {out_path}"
                        f"  ({cs.total_contracts} contrato(s) em {len(cs.tables)} tabela(s))"
                    )
                    if blocked:
                        print(
                            "  ⚠  Alertas de segurança registrados no JSON. "
                            "Resolva antes de executar 'apply'."
                        )
                    else:
                        print("  Use 'review' para aceitar/rejeitar e 'apply' para aplicar.")

    return exit_code


# ---------------------------------------------------------------------------
# cmd_sync
# ---------------------------------------------------------------------------

def cmd_sync(
    tables: list[str],
    mapping: Path | None,
    force: bool,
    yes: bool,
    deep: bool,
    batch_size: int,
    dry_run: bool,
) -> int:
    load_env()
    sg = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    mode = pick_supra_mode()
    sim_ep = simdnit_endpoint()
    targets = supra_targets_for_mode(mode)
    all_pairs = load_table_map(mapping)

    if tables:
        tl = {t.lower() for t in tables}
        all_pairs = [p for p in all_pairs if p.supra_table.lower() in tl]

    if not all_pairs:
        print("Nenhuma tabela sincronizável encontrada.")
        return 1

    exit_code = 0
    with connect_endpoint(sim_ep) as src_conn:
        src_cur = src_conn.cursor()

        for ep in targets:
            print(f"\n{'='*70}")
            print(f"Origem : {sim_ep.label} ({sim_ep.database}@{sim_ep.host})")
            print(f"Destino: {ep.label} ({ep.database}@{ep.host})")
            print(f"{'='*70}")

            with connect_endpoint(ep) as dst_conn:
                dst_cur = dst_conn.cursor()

                # ── 1. Comparação ──────────────────────────────────────────
                if force:
                    diffs = [
                        compare_table(src_cur, dst_cur, p, sg, deep=False)
                        for p in all_pairs
                    ]
                    selected = diffs
                else:
                    print("\nComparando tabelas...")
                    diffs: list[TableDiff] = []
                    for pair in all_pairs:
                        print(f"  {pair.supra_table}...", end="\r", flush=True)
                        d = compare_table(src_cur, dst_cur, pair, sg, deep=deep)
                        diffs.append(d)
                    print(" " * 70)
                    _print_diff_table(diffs)

                    # ── 2. Seleção ─────────────────────────────────────────
                    if yes:
                        selected = [d for d in diffs if d.needs_sync]
                    else:
                        selected = _select_tables(diffs, force=False)

                if not selected:
                    print("Nada a sincronizar.")
                    continue

                # ── 3. Preview de ações (sempre mostrado antes de executar) ─
                _print_diff_detail(selected)

                if dry_run:
                    print("\n[DRY-RUN] Nenhuma alteração foi feita.")
                    continue

                # ── 4. Alertas de segurança pré-migração ───────────────────
                if not run_pre_migration_alerts(src_cur, dst_conn, ep.label, sg=sg):
                    exit_code = 1
                    continue

                # ── 5. Confirmação final ───────────────────────────────────
                if not yes:
                    total_contracts = sum(len(d.changed_contracts) for d in selected)
                    print(
                        f"\nVai sincronizar {len(selected)} tabela(s) "
                        f"({total_contracts} contratos alterados)."
                    )
                    resp = input("Confirmar? (s/N): ").strip().lower()
                    if resp not in ("s", "sim", "y", "yes"):
                        print("Cancelado.")
                        continue

                # ── 6. Sincronização ───────────────────────────────────────
                print(f"\nSincronizando {len(selected)} tabela(s) em lotes de {batch_size}...")
                results: list[SyncResult] = []

                for td in selected:
                    if force:
                        contracts = _get_all_cgcont_contracts(src_cur, sg)
                    else:
                        contracts = td.changed_contracts or _get_all_cgcont_contracts(src_cur, sg)

                    print(f"\n[{td.pair.supra_table}]")
                    print(f"  Contratos a sincronizar: {len(contracts)}")

                    def _progress(done: int, total: int) -> None:
                        pct = int(done / total * 100) if total else 100
                        bar = "#" * (pct // 4) + "." * (25 - pct // 4)
                        print(f"  [{bar}] {done:,}/{total:,} ({pct}%)", end="\r", flush=True)

                    res = sync_table(
                        src_conn=src_conn,
                        dst_conn=dst_conn,
                        pair=td.pair,
                        contracts=contracts,
                        sg=sg,
                        batch_size=batch_size,
                        progress_cb=_progress,
                    )
                    results.append(res)
                    print(" " * 70)  # limpa barra de progresso

                    if res.ok:
                        print(f"  Removidas: {res.deleted:,}  |  Inseridas: {res.inserted:,}  ✓")
                    else:
                        print(f"  FALHA: {res.errors[0]}")
                        exit_code = 1

                # ── 6. Carimbar colunas injetadas em todos os contratos ────
                stamp_pairs = [td.pair for td in diffs if any(p.is_injected for p in td.pair.pairs)]
                if stamp_pairs and not dry_run:
                    all_contracts = _get_all_cgcont_contracts(src_cur, sg)
                    for pair in stamp_pairs:
                        try:
                            n = stamp_injected_cols(dst_conn, pair, all_contracts)
                            if n:
                                cols = ", ".join(
                                    p.supra for p in pair.pairs
                                    if p.is_injected and "{M}" not in p.simdnit
                                )
                                print(f"  [{pair.supra_table}] {cols} carimbada(s) em {n:,} linha(s).")
                        except Exception as exc:
                            print(f"  [{pair.supra_table}] FALHA ao carimbar: {exc}")
                            exit_code = 1

                # ── 7. Resumo final ────────────────────────────────────────
                total_ins = sum(r.inserted for r in results)
                total_del = sum(r.deleted for r in results)
                failed = [r for r in results if not r.ok]
                print(f"\nResumo {ep.label}: {len(results) - len(failed)} ok, {len(failed)} com erro")
                print(f"  Total removidas: {total_del:,} | Total inseridas: {total_ins:,}")

    return exit_code


def _get_all_cgcont_contracts(cur: pymssql.Cursor, sg: str) -> list[str]:
    cur.execute(
        "SELECT DISTINCT NU_CON_FORMATADO FROM dbo.Dados_Contrato WHERE SG_UND_GESTORA = %s",
        (sg,),
    )
    return [str(r[0]) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# cmd_inspect — inspeciona um contrato específico
# ---------------------------------------------------------------------------

def _cell(val: object, width: int) -> str:
    s = "" if val is None else str(val)
    return s[:width].ljust(width)


def _print_rows(headers: list[str], rows: list, label: str, total: int, limit: int) -> None:
    if not rows:
        print(f"  {label}: sem linhas.")
        return
    col_w = [max(12, len(h)) for h in headers]
    for row in rows:
        for i, v in enumerate(row):
            col_w[i] = min(35, max(col_w[i], len(str(v or ""))))
    sep = "  +" + "+".join("-" * (w + 2) for w in col_w) + "+"
    def _row_line(vals):
        return "  |" + "|".join(f" {_cell(v, w)} " for v, w in zip(vals, col_w)) + "|"
    shown = min(limit, len(rows))
    print(f"\n  {label} — {total:,} linhas (mostrando {shown}):")
    print(sep)
    print(_row_line(headers))
    print(sep)
    for row in rows:
        print(_row_line(row))
    print(sep)
    if total > limit:
        print(f"  ... e mais {total - limit} linhas (use --limit N para ver mais)")


def cmd_inspect(
    contract: str,
    filter_tables: list[str],
    mapping: Path | None,
    show_rows: bool,
    limit: int,
) -> int:
    load_env()
    sg = get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    mode = pick_supra_mode()
    sim_ep = simdnit_endpoint()
    targets = supra_targets_for_mode(mode)
    all_pairs = load_table_map(mapping)

    if filter_tables:
        tl = {t.lower() for t in filter_tables}
        all_pairs = [p for p in all_pairs if p.supra_table.lower() in tl]

    if not all_pairs:
        print("Nenhuma tabela sincronizável encontrada com os filtros indicados.")
        return 1

    with connect_endpoint(sim_ep) as src_conn:
        src_cur = src_conn.cursor()
        for ep in targets:
            print(f"\n{'='*70}")
            print(f"Contrato: {contract!r}")
            print(f"Destino : {ep.label} ({ep.database}@{ep.host})")
            print(f"{'='*70}")

            with connect_endpoint(ep) as dst_conn:
                dst_cur = dst_conn.cursor()

                # ── cabeçalho da tabela de resumo ─────────────────────────
                if not show_rows:
                    hdr = f"  {'Tabela SUPRA':<44} {'SIM':>7} {'SUPRA':>7} {'Δ':>6}  Status"
                    print(hdr)
                    print("  " + "-" * (len(hdr) - 2))

                for pair in all_pairs:
                    jcol_sim = _br(pair.join_simdnit)
                    jcol_supra = _br(pair.join_supra)
                    where_scope, scope_params = _build_scope_where(pair, sg)

                    # contagens — SIMDNIT sempre com escopo correto por SG_UND_GESTORA
                    src_cur.execute(
                        f"""SELECT COUNT(*) FROM {pair.simdnit_table}
                            WHERE {jcol_sim} = %s AND {where_scope}""",
                        (contract, *scope_params),
                    )
                    sim_n: int = src_cur.fetchone()[0]

                    dst_cur.execute(
                        f"SELECT COUNT(*) FROM {pair.supra_table} WHERE {jcol_supra} = %s",
                        (contract,),
                    )
                    supra_n: int = dst_cur.fetchone()[0]

                    delta = sim_n - supra_n
                    sign = "+" if delta >= 0 else ""
                    status = "OK" if delta == 0 else f"DIFF ({sign}{delta})"

                    if not show_rows:
                        print(
                            f"  {pair.supra_table:<44}"
                            f" {sim_n:>7,} {supra_n:>7,} {sign}{delta:>5}  {status}"
                        )
                        continue

                    # ── detalhe de linhas ──────────────────────────────────
                    print(f"\n{'─'*70}")
                    print(f"{pair.supra_table}")
                    print(f"  SIMDNIT : {pair.simdnit_table}  →  {sim_n:,} linhas")
                    print(f"  SUPRA   : {pair.supra_table}  →  {supra_n:,} linhas")
                    print(f"  Status  : {status}")

                    sim_cols = [p.simdnit for p in pair.pairs]
                    supra_cols = [p.supra for p in pair.pairs]

                    if sim_n > 0:
                        col_list = ", ".join(_br(c) for c in sim_cols)
                        src_cur.execute(
                            f"""SELECT TOP {limit} {col_list}
                                FROM {pair.simdnit_table}
                                WHERE {jcol_sim} = %s AND {where_scope}""",
                            (contract, *scope_params),
                        )
                        _print_rows(sim_cols, src_cur.fetchall(),
                                    f"SIMDNIT ({pair.simdnit_table})", sim_n, limit)

                    if supra_n > 0:
                        col_list = ", ".join(_br(c) for c in supra_cols)
                        dst_cur.execute(
                            f"SELECT TOP {limit} {col_list} "
                            f"FROM {pair.supra_table} WHERE {jcol_supra} = %s",
                            (contract,),
                        )
                        _print_rows(supra_cols, dst_cur.fetchall(),
                                    f"SUPRA ({pair.supra_table})", supra_n, limit)

                    if sim_n == 0 and supra_n == 0:
                        print("  Nenhuma linha em nenhum dos lados para este contrato.")

    return 0


# ---------------------------------------------------------------------------
# cmd_review — aceitar / rejeitar contratos interativamente
# ---------------------------------------------------------------------------

def _print_review_status(cs: ChangeSet) -> None:
    print(f"\n{'='*65}")
    print(f"Changeset : {cs.generated_at}  —  {cs.target_label}")
    print(
        f"Contratos : {cs.total_contracts} total"
        f"  |  {cs.n_accepted} aceito(s)"
        f"  |  {cs.n_rejected} rejeitado(s)"
        f"  |  {cs.n_pending} pendente(s)"
    )
    print(f"{'='*65}")
    hdr = f"  {'ID':<6} {'Tabela SUPRA':<44} {'Contratos':>9}  Status"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for t in cs.tables:
        print(f"  {t.id:<6} {t.table_supra:<44} {len(t.contracts):>9}  {t.status_label}")


_PAGE_SIZE = 30


def _print_table_contracts(t: TableChange) -> None:
    print(f"\n[{t.id}] {t.table_supra}  ({t.table_simdnit})")
    hdr = f"  {'ID':<7} {'Contrato':<35} {'Ação':<12} {'SIM':>7} {'SUPRA':>7}  Status"
    sep = "  " + "-" * (len(hdr) - 2)

    contracts = t.contracts
    total = len(contracts)
    pages = (total + _PAGE_SIZE - 1) // _PAGE_SIZE

    for page in range(pages):
        print(hdr)
        print(sep)
        chunk = contracts[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
        for c in chunk:
            print(
                f"  {c.id:<7} {c.contract:<35} {c.action:<12}"
                f" {c.simdnit_count:>7,} {c.supra_count:>7,}  {c.status_label}"
            )
        if pages > 1:
            shown = min((page + 1) * _PAGE_SIZE, total)
            prompt = f"  [{shown}/{total}]"
            if page < pages - 1:
                prompt += "  Enter=próxima  q=sair"
            print(prompt)
            if page < pages - 1:
                try:
                    key = input().strip().lower()
                except (EOFError, KeyboardInterrupt):
                    key = "q"
                if key == "q":
                    break
                print()  # linha em branco entre páginas


def _apply_review_decision(cs: ChangeSet, arg: str, accept: bool) -> None:
    arg_upper = arg.upper().strip()

    if arg_upper == "ALL":
        for t in cs.tables:
            t.accepted = accept
        word = "aceito(s)" if accept else "rejeitado(s)"
        n = sum(len(t.contracts) for t in cs.tables)
        print(f"  {len(cs.tables)} tabela(s), {n} contrato(s) {word}.")
        return

    ids = [i.strip() for i in arg_upper.split(",") if i.strip()]
    for id_ in ids:
        obj = cs.get_by_id(id_)
        if obj is None:
            print(f"  ID não encontrado: {id_!r}")
            continue
        if hasattr(obj, "contracts"):  # TableChange
            obj.accepted = accept
            word = "aceita" if accept else "rejeitada"
            print(f"  Tabela {obj.id} {word} ({len(obj.contracts)} contrato(s)).")
        else:  # ContractChange
            obj.accepted = accept
            # limpa override de tabela para que decisões individuais passem a valer
            parent = cs.get_table(obj.table_id)
            if parent and parent.accepted is not None:
                parent.accepted = None
                print(f"  Tabela {obj.table_id}: voltou a usar decisões individuais.")
            word = "aceito" if accept else "rejeitado"
            print(f"  Contrato {obj.id} ({obj.contract}) {word}.")


_REVIEW_HELP = """\
Comandos disponíveis:
  aceitar T01            aceita todos os contratos da tabela T01
  aceitar C0001,C0002    aceita contratos específicos por ID
  aceitar all            aceita todos os contratos de todas as tabelas
  rejeitar T01           rejeita tabela T01
  rejeitar C0001         rejeita contratos específicos por ID
  rejeitar all           rejeita tudo
  ver T01                mostra contratos e status da tabela T01
  status                 mostra resumo atual de aceitos/rejeitados
  aplicar                salva e aplica os contratos aceitos agora
  ajuda                  mostra esta mensagem
  sair                   salva e sai sem aplicar"""


def cmd_review(
    path: Path,
    mapping: Path | None = None,
    batch_size: int = 500,
) -> int:
    if not path.exists():
        print(f"Arquivo não encontrado: {path}")
        print("Execute 'compare' primeiro para gerar o arquivo de pendências.")
        return 1

    cs = load_changeset(path)

    if cs.status == "applied":
        print(f"Este changeset já foi aplicado ({cs.generated_at}).")
        return 0

    _print_review_status(cs)
    print(_REVIEW_HELP)

    while True:
        try:
            raw = input("\nreview> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        parts = raw.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("sair", "exit", "quit"):
            break

        if cmd in ("ajuda", "help", "?"):
            print(_REVIEW_HELP)
            continue

        if cmd == "status":
            _print_review_status(cs)
            continue

        if cmd == "ver":
            t = cs.get_table(arg.upper())
            if not t:
                print(f"  Tabela {arg!r} não encontrada. Use 'status' para ver os IDs.")
                continue
            _print_table_contracts(t)
            continue

        if cmd in ("aceitar", "rejeitar"):
            if not arg:
                print(f"  Uso: {cmd} T01  |  {cmd} C0001,C0002  |  {cmd} all")
                continue
            _apply_review_decision(cs, arg, accept=(cmd == "aceitar"))
            save_changeset(cs, path)
            print(f"  Salvo em {path}")
            continue

        if cmd == "aplicar":
            if cs.n_accepted == 0:
                print("  Nenhum contrato aceito. Use 'aceitar' antes de aplicar.")
                continue
            save_changeset(cs, path)
            return cmd_apply(path, mapping, batch_size)

        print(f"  Comando desconhecido: {raw!r}  (use 'ajuda' para ver os comandos)")

    save_changeset(cs, path)
    return 0


# ---------------------------------------------------------------------------
# cmd_apply — aplica os contratos aceitos no changeset
# ---------------------------------------------------------------------------

def cmd_apply(
    path: Path,
    mapping: Path | None,
    batch_size: int,
    force: bool = False,
) -> int:
    if not path.exists():
        print(f"Arquivo não encontrado: {path}")
        print("Execute 'compare' e 'review' primeiro.")
        return 1

    cs = load_changeset(path)

    if cs.status == "applied":
        print(f"Este changeset já foi aplicado ({cs.generated_at}). Nada a fazer.")
        return 0

    if not cs.ready_to_apply:
        print("Nenhum contrato aceito para aplicar.")
        print(
            f"  Aceitos: {cs.n_accepted}"
            f"  |  Pendentes: {cs.n_pending}"
            f"  |  Rejeitados: {cs.n_rejected}"
        )
        print("Execute 'review' para aceitar contratos antes de aplicar.")
        return 1

    tables_with_work = [t for t in cs.tables if t.effective_contract_numbers]
    total_contracts = sum(len(t.effective_contract_numbers) for t in tables_with_work)

    print(f"\nChangeset: {cs.generated_at}  —  {cs.target_label}")
    print(f"Aplicando {len(tables_with_work)} tabela(s) / {total_contracts} contrato(s) aceito(s)...")

    load_env()
    sg = cs.sg_und_gestora
    all_pairs = load_table_map(mapping)
    pair_map = {p.supra_table: p for p in all_pairs}

    sim_ep = simdnit_endpoint()
    targets = supra_targets_for_mode(pick_supra_mode())

    # localiza o destino pelo label salvo no changeset
    target = next((t for t in targets if t.label == cs.target_label), None)
    if not target:
        if not targets:
            print("Nenhum destino SUPRA configurado.")
            return 1
        target = targets[0]
        print(f"  Aviso: destino '{cs.target_label}' não encontrado; usando '{target.label}'")

    exit_code = 0
    results: list[SyncResult] = []

    with connect_endpoint(sim_ep) as src_conn:
        with connect_endpoint(target) as dst_conn:
            # ── alertas de segurança antes de qualquer alteração ──────────
            src_cur_alerts = src_conn.cursor()
            try:
                alerts_ok = run_pre_migration_alerts(src_cur_alerts, dst_conn, target.label, sg=sg)
                if not alerts_ok:
                    if not force:
                        return 1
                    print("\n[AVISO] --force ativo: prosseguindo apesar dos alertas acima.")
            finally:
                src_cur_alerts.close()

            # ── pré-dedup: remove cópias exatas antes do D/I ─────────────
            active_pairs = [
                pair_map[tc.table_supra]
                for tc in tables_with_work
                if tc.table_supra in pair_map
            ]
            if active_pairs:
                print("\nRemovendo duplicatas exatas das tabelas SUPRA...")
                dedup_tables(dst_conn, active_pairs)

            for tc in tables_with_work:
                contracts = tc.effective_contract_numbers
                pair = pair_map.get(tc.table_supra)
                if not pair:
                    print(f"\n  [AVISO] {tc.table_supra} não encontrada no mapeamento; ignorando.")
                    continue

                n_ignored = sum(1 for c in tc.contracts if c.is_ignored)
                print(f"\n[{tc.id}] {tc.table_supra}  —  {len(contracts)} contrato(s)" +
                      (f"  ({n_ignored} ignorado(s) — SUPRA já correto)" if n_ignored else ""))

                def _progress(done: int, total: int) -> None:
                    pct = int(done / total * 100) if total else 100
                    bar = "#" * (pct // 4) + "." * (25 - pct // 4)
                    print(f"  [{bar}] {done:,}/{total:,} ({pct}%)", end="\r", flush=True)

                res = sync_table(
                    src_conn=src_conn,
                    dst_conn=dst_conn,
                    pair=pair,
                    contracts=contracts,
                    sg=sg,
                    batch_size=batch_size,
                    progress_cb=_progress,
                )
                results.append(res)
                print(" " * 70)

                if res.ok:
                    print(f"  Removidas: {res.deleted:,}  |  Inseridas: {res.inserted:,}  ✓")
                else:
                    print(f"  FALHA: {res.errors[0]}")
                    exit_code = 1

    # resumo final
    total_ins = sum(r.inserted for r in results)
    total_del = sum(r.deleted for r in results)
    failed = [r for r in results if not r.ok]
    print(f"\nResumo: {len(results) - len(failed)} tabela(s) ok, {len(failed)} com erro")
    print(f"  Total removidas: {total_del:,}  |  Total inseridas: {total_ins:,}")

    if exit_code == 0:
        cs.status = "applied"
        save_changeset(cs, path)
        print(f"\nChangeset marcado como aplicado: {path}")
    else:
        print(f"\nAlgumas operações falharam — changeset NÃO marcado como aplicado.")

    return exit_code


# ---------------------------------------------------------------------------
# cmd_navigate — revisar contrato por contrato com diff de linhas
# ---------------------------------------------------------------------------

_NAV_ROW_LIMIT = 20
_COL_MAX_W = 35


def _print_row_diff(rd: RowDiff, limit: int = _NAV_ROW_LIMIT) -> None:
    if rd.error:
        print(f"  Erro ao carregar linhas: {rd.error}")
        return
    if rd.warning:
        print(f"  ⚠  {rd.warning}")
        return
    if not rd.added and not rd.removed:
        print(f"  Conteúdo idêntico ({rd.common} linha(s) iguais — diff de checksum pode ter falso positivo).")
        return

    col_w = [max(8, len(h)) for h in rd.cols]
    sample = (rd.removed + rd.added)[:limit * 2]
    for row in sample:
        for i, v in enumerate(row):
            col_w[i] = min(_COL_MAX_W, max(col_w[i], len(str(v) if v is not None else "")))

    sep = "  +" + "+".join("-" * (w + 2) for w in col_w) + "+"
    hdr = "  |" + "|".join(f" {h[:w].ljust(w)} " for h, w in zip(rd.cols, col_w)) + "|"

    def _row_line(prefix: str, row: tuple) -> str:
        cells = "|".join(
            f" {str(v if v is not None else '')[:w].ljust(w)} "
            for v, w in zip(row, col_w)
        )
        return f"{prefix}|{cells}|"

    print(sep)
    print(hdr)
    print(sep)
    for row in rd.removed[:limit]:
        print(_row_line("  [-]", row))
    if len(rd.removed) > limit:
        print(f"  [-] ... e mais {len(rd.removed) - limit} linha(s) a remover")
    for row in rd.added[:limit]:
        print(_row_line("  [+]", row))
    if len(rd.added) > limit:
        print(f"  [+] ... e mais {len(rd.added) - limit} linha(s) a inserir")
    print(sep)

    parts = []
    if rd.removed:
        parts.append(f"[-] {len(rd.removed)} a remover")
    if rd.added:
        parts.append(f"[+] {len(rd.added)} a inserir")
    if rd.common:
        parts.append(f"[=] {rd.common} sem alteração")
    print("  " + "  ".join(parts))


_NAV_HELP = """\
Comandos:
  a / aceitar    aceita este contrato (será sincronizado no apply)
  r / rejeitar   rejeita este contrato (não será tocado)
  p / próximo    pula sem decidir (mantém como pendente)
  s / sair       salva e encerra a navegação"""


def cmd_navigate(
    path: Path,
    mapping: Path | None,
    filter_table: str | None,
    filter_contracts: list[str] | None,
    limit: int,
) -> int:
    if not path.exists():
        print(f"Arquivo não encontrado: {path}")
        print("Execute 'compare' primeiro para gerar o arquivo de pendências.")
        return 1

    cs = load_changeset(path)
    if cs.status == "applied":
        print(f"Este changeset já foi aplicado ({cs.generated_at}). Nada a fazer.")
        return 0

    all_pairs = load_table_map(mapping)
    pair_map = {p.supra_table: p for p in all_pairs}

    load_env()
    sg = cs.sg_und_gestora
    sim_ep = simdnit_endpoint()
    targets = supra_targets_for_mode(pick_supra_mode())
    target = next((t for t in targets if t.label == cs.target_label), None)
    if not target:
        if not targets:
            print("Nenhum destino SUPRA configurado.")
            return 1
        target = targets[0]
        print(f"  Aviso: destino '{cs.target_label}' não encontrado; usando '{target.label}'")

    # Montar lista de (TableChange, ContractChange) a navegar
    tables = cs.tables
    if filter_table:
        ft = filter_table.upper()
        tables = [
            t for t in tables
            if t.id.upper() == ft or ft in t.table_supra.upper()
        ]

    fc = {c.upper() for c in filter_contracts} if filter_contracts else None

    to_nav: list[tuple] = [
        (tc, cc)
        for tc in tables
        for cc in tc.contracts
        if cc.accepted is None and (fc is None or cc.id.upper() in fc)
    ]

    if not to_nav:
        print("Nenhum contrato pendente para navegar.")
        if filter_contracts:
            print(f"  (filtro de contrato(s): {', '.join(filter_contracts)})")
        if filter_table:
            print(f"  (filtro aplicado: {filter_table!r})")
        return 0

    total = len(to_nav)
    print(f"\nNavigando {total} contrato(s) pendente(s).")
    if filter_table:
        print(f"Filtro: {filter_table!r}")
    print(_NAV_HELP)

    with connect_endpoint(sim_ep) as src_conn:
        src_cur = src_conn.cursor()
        with connect_endpoint(target) as dst_conn:
            dst_cur = dst_conn.cursor()

            for idx, (tc, cc) in enumerate(to_nav, 1):
                pair = pair_map.get(tc.table_supra)

                print(f"\n{'='*70}")
                print(
                    f"[{idx}/{total}] {cc.id}  —  {cc.contract}"
                    f"  ({cc.action}: SIM={cc.simdnit_count:,}  SUPRA={cc.supra_count:,}  Δ={cc.delta:+,})"
                )
                print(f"Tabela : {tc.table_supra}")

                if pair:
                    print("Carregando diff...", end="\r", flush=True)
                    rd = diff_rows_for_contract(src_cur, dst_cur, pair, cc.contract, sg)
                    print(" " * 25, end="\r")
                    _print_row_diff(rd, limit)
                else:
                    print("  (tabela não encontrada no mapeamento — use 'compare' para atualizar)")

                while True:
                    try:
                        resp = input("\n[a]ceitar / [r]ejeitar / [p]róximo / [s]air > ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        resp = "s"

                    if resp in ("a", "aceitar", "r", "rejeitar", "p", "próximo", "proximo", "s", "sair"):
                        break
                    print("  Comando inválido. Use: a / r / p / s")

                if resp in ("s", "sair"):
                    save_changeset(cs, path)
                    print(f"\nInterrompido. Progresso salvo em {path}.")
                    break

                parent = cs.get_table(cc.table_id)

                if resp in ("a", "aceitar"):
                    cc.accepted = True
                    if parent and parent.accepted is not None:
                        parent.accepted = None
                    print(f"  ✓ Aceito.")
                elif resp in ("r", "rejeitar"):
                    cc.accepted = False
                    if parent and parent.accepted is not None:
                        parent.accepted = None
                    print(f"  ✗ Rejeitado.")
                # "p" = pular, accepted permanece None

                save_changeset(cs, path)

    # Resumo final
    print(f"\n{'='*70}")
    a, r, p = cs.n_accepted, cs.n_rejected, cs.n_pending
    print(f"Navegação concluída: {a} aceito(s)  {r} rejeitado(s)  {p} pendente(s)")
    if a > 0:
        print(f"Execute 'apply' para sincronizar os {a} contrato(s) aceito(s).")
    if p > 0:
        print(f"Execute 'navigate' novamente para continuar os {p} pendente(s).")

    return 0


# ---------------------------------------------------------------------------
# Subcomandos web (substituem scripts/web_*.py — rodados via binário)
# ---------------------------------------------------------------------------

def _web_safe(v) -> "str | None":
    if v is None:
        return None
    try:
        return str(v)
    except Exception:
        return repr(v)


def _web_row_sort_key(row) -> list:
    result = []
    for v in row:
        s = str(v) if v is not None else ""
        try:
            result.append((0, float(s), ""))
        except (ValueError, TypeError):
            result.append((1, 0.0, s))
    return result


def _web_mapping_meta(table_supra: str) -> dict:
    from supra_db_update._paths import runtime_root
    mapping_path = runtime_root() / "column_mapping.json"
    if not mapping_path.exists():
        return {"join_col": None, "disabled_mapped": [], "sub_key_supra": None}
    try:
        raw = _json.loads(mapping_path.read_text(encoding="utf-8"))
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


def cmd_web_connection_info() -> int:
    load_env()
    try:
        src = simdnit_endpoint()
        tgt = supra_targets_for_mode(pick_supra_mode())[0]
        print(_json.dumps({
            "simdnit": {"label": src.label, "host": src.host, "port": src.port, "database": src.database},
            "supra":   {"label": tgt.label, "host": tgt.host, "port": tgt.port, "database": tgt.database},
        }))
    except Exception as e:
        print(_json.dumps({"erro": str(e)}))
    return 0


def cmd_web_diff_contrato(contract_id: str, limit: int, json_path_str: str | None) -> int:
    from supra_db_update._paths import runtime_root
    load_env()

    if json_path_str:
        json_path = Path(json_path_str).resolve()
    else:
        json_path = runtime_root() / "pending_changes.json"
    if not json_path.exists():
        print(_json.dumps({"erro": "pending_changes.json não encontrado"}, ensure_ascii=False))
        return 1

    cs = load_changeset(json_path)
    contract_change = cs.get_contract(contract_id)
    if not contract_change:
        print(_json.dumps({"erro": f"Contrato {contract_id!r} não encontrado"}, ensure_ascii=False))
        return 1

    table_change = cs.get_table(contract_change.table_id)
    if not table_change:
        print(_json.dumps({"erro": f"Tabela {contract_change.table_id!r} não encontrada"}, ensure_ascii=False))
        return 1

    pairs = load_table_map()
    pair = next((p for p in pairs if p.supra_table.lower() == table_change.table_supra.lower()), None)
    if not pair:
        print(_json.dumps({
            "erro": f"Mapeamento de colunas não encontrado para {table_change.table_supra}",
            "contract_id": contract_id,
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
                src_conn.cursor(), dst_conn.cursor(), pair, contract_change.contract, sg,
            )
    except Exception as exc:
        print(_json.dumps({
            "contract_id": contract_id,
            "contract":    contract_change.contract,
            "table_supra": table_change.table_supra,
            "action":      contract_change.action,
            "cols": [], "added": [], "added_total": 0,
            "removed": [], "removed_total": 0, "common": 0,
            "warning": "", "error": str(exc),
        }, ensure_ascii=False))
        return 1

    count_mismatch = (
        len(rd.added) == 0 and len(rd.removed) == 0
        and contract_change.simdnit_count != contract_change.supra_count
    )
    raw_limit = min(limit, 50)
    full_meta = _web_mapping_meta(table_change.table_supra)
    meta = full_meta if count_mismatch else {"join_col": None, "disabled_mapped": [], "sub_key_supra": full_meta["sub_key_supra"]}

    mismatch_analysis: dict = {}
    if count_mismatch and rd.sim_raw:
        sim_set   = set(rd.sim_raw)
        supra_set = set(rd.supra_raw)
        sim_dupes     = len(rd.sim_raw) - len(sim_set)
        genuinely_new = sorted(sim_set - supra_set, key=_web_row_sort_key)
        supra_only    = sorted(supra_set - sim_set, key=_web_row_sort_key)
        dupe_sample   = sorted(sim_set,             key=_web_row_sort_key)
        sim_raw_sorted   = sorted(rd.sim_raw,   key=_web_row_sort_key)
        supra_raw_sorted = sorted(rd.supra_raw, key=_web_row_sort_key)
        mismatch_analysis = {
            "simdnit_total":     len(rd.sim_raw),
            "simdnit_unique":    len(sim_set),
            "simdnit_dupes":     sim_dupes,
            "supra_total":       len(rd.supra_raw),
            "supra_unique":      len(supra_set),
            "genuinely_new":     len(genuinely_new),
            "supra_only":        len(supra_only),
            "new_rows":          [[_web_safe(v) for v in r] for r in genuinely_new[:raw_limit]],
            "old_rows":          [[_web_safe(v) for v in r] for r in supra_only[:raw_limit]],
            "dupe_sample":       [[_web_safe(v) for v in r] for r in dupe_sample[:raw_limit]],
            "sim_raw_sorted":    [[_web_safe(v) for v in r] for r in sim_raw_sorted[:raw_limit]],
            "supra_raw_sorted":  [[_web_safe(v) for v in r] for r in supra_raw_sorted[:raw_limit]],
        }

    _NULL_WARN: dict[str, list[str]] = {
        "dbo.tb_siac_reajuste":               ["data_da_assinatura_do_reajuste"],
        "dbo.tb_siac_empenho_conta_corrente":  ["Nota_de_Empenho"],
    }
    table_key = table_change.table_supra.lower()
    critical_cols = _NULL_WARN.get(table_key, [])
    cols_lower = [c.lower() for c in rd.cols]
    warn_null_col_indices = [
        cols_lower.index(cc.lower())
        for cc in critical_cols
        if cc.lower() in cols_lower
    ]
    null_warn_rows = []
    if warn_null_col_indices and rd.added:
        for ri, row in enumerate(rd.added[:limit]):
            for ci in warn_null_col_indices:
                if ci < len(row) and row[ci] is None:
                    null_warn_rows.append(ri)
                    break

    result = {
        "contract_id":        contract_id,
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
        "added":              [[_web_safe(v) for v in row] for row in rd.added[:limit]],
        "added_total":        len(rd.added),
        "removed":            [[_web_safe(v) for v in row] for row in rd.removed[:limit]],
        "removed_total":      len(rd.removed),
        "common":             rd.common,
        "warning":            rd.warning,
        "error":              rd.error,
        "truncated":          len(rd.added) > limit or len(rd.removed) > limit,
        "sim_raw":   ([[_web_safe(v) for v in r] for r in sorted(rd.sim_raw,   key=_web_row_sort_key)[:raw_limit]] if count_mismatch else []),
        "supra_raw": ([[_web_safe(v) for v in r] for r in sorted(rd.supra_raw, key=_web_row_sort_key)[:raw_limit]] if count_mismatch else []),
        "warn_null_col_idx":  warn_null_col_indices,
        "null_warn_rows":     null_warn_rows,
        "mapping_join_col":   meta["join_col"],
        "mapping_disabled":   meta["disabled_mapped"],
        "sub_key_supra":      meta["sub_key_supra"],
        "mismatch_analysis":  mismatch_analysis,
    }
    print(_json.dumps(result, ensure_ascii=False, default=str))
    return 0


def cmd_web_analise_duplicatas(table_id: str, limit: int, json_path_str: str | None) -> int:
    from supra_db_update._paths import runtime_root
    load_env()

    if json_path_str:
        json_path = Path(json_path_str).resolve()
    else:
        json_path = runtime_root() / "pending_changes.json"

    if not json_path.exists():
        print(_json.dumps({"erro": "pending_changes.json não encontrado"}, ensure_ascii=False))
        return 1

    cs = load_changeset(json_path)
    table_change = cs.get_table(table_id)
    if not table_change:
        print(_json.dumps({"erro": f"Tabela {table_id!r} não encontrada"}, ensure_ascii=False))
        return 1

    candidates = [
        c for c in table_change.contracts
        if c.action == "D/I" and c.simdnit_count != c.supra_count
    ]

    pairs = load_table_map()
    pair = next((p for p in pairs if p.supra_table.lower() == table_change.table_supra.lower()), None)
    if not pair:
        print(_json.dumps({"erro": f"Mapeamento não encontrado para {table_change.table_supra}"}, ensure_ascii=False))
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
                    dupe_sample    = sorted(sim_set,    key=_web_row_sort_key)
                    sim_raw_sorted = sorted(rd.sim_raw, key=_web_row_sort_key)
                    results.append({
                        "contract":       cc.contract,
                        "simdnit_total":  len(rd.sim_raw),
                        "simdnit_unique": len(sim_set),
                        "simdnit_dupes":  sim_dupes,
                        "cols":           rd.cols,
                        "sim_raw":        [[_web_safe(v) for v in r] for r in sim_raw_sorted[:limit]],
                        "dupe_sample":    [[_web_safe(v) for v in r] for r in dupe_sample[:limit]],
                    })

    print(_json.dumps({
        "table_id":          table_id,
        "table_supra":       table_change.table_supra,
        "table_simdnit":     pair.simdnit_table,
        "simdnit_host":      sim_ep.host,
        "simdnit_port":      sim_ep.port,
        "simdnit_database":  sim_ep.database,
        "simdnit_label":     sim_ep.label,
        "total_candidates":  len(candidates),
        "results":           results,
    }, ensure_ascii=False, default=str))
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SUPRA_DB_UPDATE — SIMDNIT → SUPRA")
    sub = p.add_subparsers(dest="command", required=True)

    # test-connections
    t = sub.add_parser("test-connections", help="Testa conexão com todas as bases configuradas")
    t.set_defaults(_run=lambda _: cmd_test_connections())

    # validate
    v = sub.add_parser("validate", help="Validações pré-migração (contratos) para tabelas")
    v.add_argument("tables", nargs="+", help="Ex.: dbo.Dados_Medicao")
    v.add_argument("--contracts", type=Path, default=None)
    v.set_defaults(_run=lambda a: cmd_validate_tables(a.tables, a.contracts))

    # alerts
    al = sub.add_parser(
        "alerts",
        help="Executa todos os alertas de segurança pré-migração (não altera dados)",
    )
    al.add_argument(
        "--contracts",
        type=Path,
        default=None,
        metavar="ARQUIVO",
        help="Caminho alternativo para import_rules.json",
    )
    al.set_defaults(_run=lambda a: cmd_alerts(a.contracts))

    # check-counts
    cc = sub.add_parser(
        "check-counts",
        help="Verifica contagem de linhas SIMDNIT >= SUPRA para uma tabela específica",
    )
    cc.add_argument(
        "--table",
        required=True,
        metavar="SUPRA_TABLE",
        help="Nome da tabela SUPRA (ex.: dbo.TB_SIAC_MEDICAO_MAIOR)",
    )
    cc.set_defaults(_run=lambda a: cmd_check_counts(a.table))

    # check-date-regression
    cdr = sub.add_parser(
        "check-date-regression",
        help="Verifica regressão de datas (1900) em todas as tabelas configuradas",
    )
    cdr.set_defaults(_run=lambda _: cmd_check_date_regression())

    # check-contract-values
    ccv = sub.add_parser(
        "check-contract-values",
        help="Verifica aritmética de valores em dbo.Dados_Contrato (SIMDNIT)",
    )
    ccv.set_defaults(_run=lambda _: cmd_check_contract_values())

    # check-data-integrity
    cdi = sub.add_parser(
        "check-data-integrity",
        help="Verifica integridade por contrato: SIMDNIT não perdeu linhas que SUPRA já tem",
    )
    cdi.set_defaults(_run=lambda _: cmd_check_data_integrity())

    # check-reajuste-null-date
    crnd = sub.add_parser(
        "check-reajuste-null-date",
        help="Alerta quando dbo.Dados_Reajuste tem linhas com Data_da_Assinatura_do_Reajuste IS NULL",
    )
    crnd.set_defaults(_run=lambda _: cmd_check_reajuste_null_date())

    # check-empenho-null-nota
    cenn = sub.add_parser(
        "check-empenho-null-nota",
        help="Alerta quando dbo.Dados_Empenho tem linhas com NU_EMPENHO IS NULL",
    )
    cenn.set_defaults(_run=lambda _: cmd_check_empenho_null_nota())

    # run-all
    ra = sub.add_parser(
        "run-all",
        help="Executa todas as regras de import_rules.json e em seguida o compare --detail",
    )
    ra.set_defaults(_run=lambda _: cmd_run_all())

    # compare
    cmp = sub.add_parser(
        "compare",
        help="Compara SIMDNIT↔SUPRA e gera pending_changes.json",
    )
    cmp.add_argument(
        "tables",
        nargs="*",
        help="Tabelas SUPRA a comparar (omitir = todas sincronizáveis)",
    )
    cmp.add_argument(
        "--mapping",
        type=Path,
        default=None,
        help="Caminho alternativo para o JSON de inferência de colunas",
    )
    cmp.add_argument(
        "--deep",
        action="store_true",
        help="Verifica também checksum de linhas (detecta alterações sem mudança de contagem)",
    )
    cmp.add_argument(
        "--detail",
        action="store_true",
        help="Mostra por contrato o que será inserido / reposto / apagado",
    )
    cmp.set_defaults(_run=lambda a: cmd_compare(a.tables, a.mapping, a.deep, a.detail))

    # review
    rev = sub.add_parser(
        "review",
        help="Aceitar / rejeitar contratos no pending_changes.json interativamente",
    )
    rev.add_argument(
        "--path",
        type=Path,
        default=Path("pending_changes.json"),
        metavar="ARQUIVO",
        help="Arquivo de pendências (padrão: pending_changes.json)",
    )
    rev.add_argument(
        "--mapping",
        type=Path,
        default=None,
    )
    rev.add_argument(
        "--batch-size",
        type=int,
        default=500,
        metavar="N",
        help="Linhas por lote para 'aplicar' dentro do review (padrão: 500)",
    )
    rev.set_defaults(_run=lambda a: cmd_review(a.path, a.mapping, a.batch_size))

    # apply
    apl = sub.add_parser(
        "apply",
        help="Aplica os contratos aceitos no pending_changes.json",
    )
    apl.add_argument(
        "--path",
        type=Path,
        default=Path("pending_changes.json"),
        metavar="ARQUIVO",
        help="Arquivo de pendências (padrão: pending_changes.json)",
    )
    apl.add_argument(
        "--mapping",
        type=Path,
        default=None,
    )
    apl.add_argument(
        "--batch-size",
        type=int,
        default=500,
        metavar="N",
        help="Linhas por lote no INSERT (padrão: 500)",
    )
    apl.add_argument(
        "--force",
        action="store_true",
        default=False,
        help="Aplica mesmo que alertas pré-migração falhem",
    )
    apl.set_defaults(_run=lambda a: cmd_apply(a.path, a.mapping, a.batch_size, a.force))

    # sync
    syn = sub.add_parser(
        "sync",
        help="Sincroniza SIMDNIT→SUPRA: compara, seleciona e atualiza em lotes",
    )
    syn.add_argument(
        "tables",
        nargs="*",
        help="Tabelas SUPRA a sincronizar (omitir = todas com diferença)",
    )
    syn.add_argument(
        "--mapping",
        type=Path,
        default=None,
        help="Caminho alternativo para o JSON de inferência de colunas",
    )
    syn.add_argument(
        "--force",
        action="store_true",
        help="Ignora comparação e sincroniza todos os contratos CGCONT",
    )
    syn.add_argument(
        "--yes",
        action="store_true",
        help="Confirma automaticamente sem prompt interativo",
    )
    syn.add_argument(
        "--deep",
        action="store_true",
        help="Usa checksum para detectar alterações de valor (mais lento)",
    )
    syn.add_argument(
        "--batch-size",
        type=int,
        default=500,
        metavar="N",
        help="Linhas por lote no INSERT (padrão: 500)",
    )
    syn.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra o que seria feito sem executar nenhuma alteração",
    )
    syn.set_defaults(
        _run=lambda a: cmd_sync(
            a.tables, a.mapping, a.force, a.yes, a.deep, a.batch_size, a.dry_run
        )
    )

    # navigate
    nav = sub.add_parser(
        "navigate",
        help="Revisa contrato por contrato com diff de linhas: aceitar/rejeitar interativamente",
    )
    nav.add_argument(
        "--path",
        type=Path,
        default=Path("pending_changes.json"),
        metavar="ARQUIVO",
        help="Arquivo de pendências (padrão: pending_changes.json)",
    )
    nav.add_argument(
        "--table",
        dest="filter_table",
        default=None,
        metavar="ID_OU_NOME",
        help="Filtrar por tabela — ID (ex.: T01) ou parte do nome (ex.: MEDICAO)",
    )
    nav.add_argument(
        "--contract",
        dest="filter_contracts",
        nargs="+",
        default=None,
        metavar="ID",
        help="Filtrar por ID(s) de contrato — ex.: C0006 C0010",
    )
    nav.add_argument(
        "--mapping",
        type=Path,
        default=None,
    )
    nav.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="Máximo de linhas exibidas por seção [+]/[-] (padrão: 20)",
    )
    nav.set_defaults(
        _run=lambda a: cmd_navigate(a.path, a.mapping, a.filter_table, a.filter_contracts, a.limit)
    )

    # web-connection-info
    wci = sub.add_parser("web-connection-info", help="Retorna info de conexão como JSON (uso interno web)")
    wci.set_defaults(_run=lambda _: cmd_web_connection_info())

    # web-diff-contrato
    wdc = sub.add_parser("web-diff-contrato", help="Diff JSON de um contrato para a interface web")
    wdc.add_argument("--contract-id", required=True, help="ID do contrato (ex: C0001)")
    wdc.add_argument("--limit", type=int, default=200, help="Máximo de linhas por seção")
    wdc.add_argument("--json-path", default=None, help="Caminho para pending_changes.json")
    wdc.set_defaults(_run=lambda a: cmd_web_diff_contrato(a.contract_id, a.limit, a.json_path))

    # web-analise-duplicatas
    wad = sub.add_parser("web-analise-duplicatas", help="Analisa duplicatas SIMDNIT para uma tabela (uso interno web)")
    wad.add_argument("--table-id", required=True, help="ID da tabela (ex: T03)")
    wad.add_argument("--limit", type=int, default=50, help="Máximo de linhas brutas por contrato")
    wad.add_argument("--json-path", default=None, help="Caminho para pending_changes.json")
    wad.set_defaults(_run=lambda a: cmd_web_analise_duplicatas(a.table_id, a.limit, a.json_path))

    # inspect
    ins = sub.add_parser(
        "inspect",
        help="Mostra linhas de um contrato específico no SIMDNIT e no SUPRA",
    )
    ins.add_argument(
        "contract",
        help="Número do contrato (ex.: '00 00799/2025')",
    )
    ins.add_argument(
        "--table",
        dest="tables",
        nargs="+",
        default=[],
        metavar="TABELA",
        help="Filtrar por tabela(s) SUPRA (omitir = todas)",
    )
    ins.add_argument(
        "--mapping",
        type=Path,
        default=None,
    )
    ins.add_argument(
        "--rows",
        action="store_true",
        help="Exibir as linhas das colunas mapeadas (SIMDNIT e SUPRA)",
    )
    ins.add_argument(
        "--limit",
        type=int,
        default=20,
        metavar="N",
        help="Máximo de linhas a exibir por tabela (padrão: 20)",
    )
    ins.set_defaults(
        _run=lambda a: cmd_inspect(a.contract, a.tables, a.mapping, a.rows, a.limit)
    )

    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args()
    raise SystemExit(args._run(args))


if __name__ == "__main__":
    main()
