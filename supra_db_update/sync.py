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
from supra_db_update.migrator import SyncResult, stamp_injected_cols, sync_table
from supra_db_update.table_map import TablePair, _br, load_table_map
from supra_db_update.validators import (
    TableContract,
    load_table_contracts,
    rules_for_table,
    validate_before_migration,
    validate_table_contract,
)

log = logging.getLogger(__name__)

_COL_W = 46  # largura da coluna de nome de tabela na saída


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
                log.info("%s — %s sem regras em table_contracts.yaml; a ignorar.", dest_label, qualified)
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
) -> list[dict]:
    """Coleta resultados de alertas como lista de dicts (para persistir no JSON)."""
    contracts = load_table_contracts(contracts_path)
    if not contracts:
        return []
    results: list[dict] = []
    dst_cur = dst_conn.cursor()
    try:
        for contract in contracts.values():
            for res in validate_table_contract(src_cur, dst_cur, contract):
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
) -> bool:
    """Executa e imprime todos os alertas. Retorna True se nenhum falhou."""
    results = collect_alerts(src_cur, dst_conn, contracts_path)
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
                    if not run_pre_migration_alerts(src_cur, dst_conn, ep.label, contracts_path):
                        exit_code = 1
        finally:
            src_cur.close()

    if exit_code == 0:
        print("\nTodos os alertas passaram. Migração liberada.")
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
                alert_results = collect_alerts(src_cur, dst_conn)
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
                if not run_pre_migration_alerts(src_cur, dst_conn, ep.label):
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
        "SELECT DISTINCT NU_CON_FORMATADO FROM dbo.Dados_Contrato WHERE SG_UND_GESTORA = ?",
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
                            WHERE {jcol_sim} = ? AND {where_scope}""",
                        (contract, *scope_params),
                    )
                    sim_n: int = src_cur.fetchone()[0]

                    dst_cur.execute(
                        f"SELECT COUNT(*) FROM {pair.supra_table} WHERE {jcol_supra} = ?",
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
                                WHERE {jcol_sim} = ? AND {where_scope}""",
                            (contract, *scope_params),
                        )
                        _print_rows(sim_cols, src_cur.fetchall(),
                                    f"SIMDNIT ({pair.simdnit_table})", sim_n, limit)

                    if supra_n > 0:
                        col_list = ", ".join(_br(c) for c in supra_cols)
                        dst_cur.execute(
                            f"SELECT TOP {limit} {col_list} "
                            f"FROM {pair.supra_table} WHERE {jcol_supra} = ?",
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
                if not run_pre_migration_alerts(src_cur_alerts, dst_conn, target.label):
                    return 1
            finally:
                src_cur_alerts.close()

            for tc in tables_with_work:
                contracts = tc.effective_contract_numbers
                pair = pair_map.get(tc.table_supra)
                if not pair:
                    print(f"\n  [AVISO] {tc.table_supra} não encontrada no mapeamento; ignorando.")
                    continue

                print(f"\n[{tc.id}] {tc.table_supra}  —  {len(contracts)} contrato(s)")

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
        help="Caminho alternativo para table_contracts.yaml",
    )
    al.set_defaults(_run=lambda a: cmd_alerts(a.contracts))

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
    apl.set_defaults(_run=lambda a: cmd_apply(a.path, a.mapping, a.batch_size))

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
