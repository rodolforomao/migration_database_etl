"""Executa a migração SIMDNIT→SUPRA em lotes (DELETE + INSERT por contrato)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import pymssql

from supra_db_update.differ import _build_scope_where, _norm_row
from supra_db_update.table_map import TablePair, _br, _sql_expr

log = logging.getLogger(__name__)

_CHUNK = 900


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]




@dataclass
class SyncResult:
    supra_table: str
    deleted: int = 0
    inserted: int = 0
    contracts_synced: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _fetch_simdnit(
    cur: pymssql.Cursor,
    pair: TablePair,
    contracts: list[str],
    sg: str,
) -> list[tuple]:
    """Busca linhas do SIMDNIT pelas colunas mapeadas, sempre dentro do escopo correto."""
    contracts = list(dict.fromkeys(contracts))  # remove duplicatas preservando ordem
    if pair.extra_joins:
        # usa aliases para evitar ambiguidade; LEFT JOIN nas tabelas extras
        m_cols = ", ".join(p.simdnit_sql_expr("_m") for p in pair.pairs)
        j_col_parts: list[str] = []
        join_clauses: list[str] = []
        for i, ej in enumerate(pair.extra_joins):
            alias = f"_j{i}"
            for ec in ej.columns:
                j_col_parts.append(f"{alias}.{_br(ec.simdnit)}")
            join_clauses.append(
                f"LEFT JOIN {ej.simdnit_table} AS {alias} "
                f"ON _m.{_br(ej.main_col)} = {alias}.{_br(ej.join_col)}"
            )
        all_cols = ", ".join(filter(None, [m_cols, ", ".join(j_col_parts)]))
        joins_sql = "\n".join(join_clauses)
        where_scope, scope_params = _build_scope_where(pair, sg, table_prefix="_m")
        jcol = f"_m.{_br(pair.join_simdnit)}"
        from_clause = f"{pair.simdnit_table} AS _m\n{joins_sql}"
    elif pair.needs_main_alias:
        where_scope, scope_params = _build_scope_where(pair, sg, table_prefix="_m")
        all_cols = ", ".join(p.simdnit_sql_expr("_m") for p in pair.pairs)
        jcol = f"_m.{_br(pair.join_simdnit)}"
        from_clause = f"{pair.simdnit_table} AS _m"
    else:
        all_cols = ", ".join(p.simdnit_sql_expr() for p in pair.pairs)
        where_scope, scope_params = _build_scope_where(pair, sg)
        jcol = _br(pair.join_simdnit)
        from_clause = pair.simdnit_table

    rows: list[tuple] = []
    for chunk in _chunks(contracts, _CHUNK):
        ph = ", ".join(["%s"] * len(chunk))
        sql = f"""
            SELECT {all_cols}
            FROM {from_clause}
            WHERE {where_scope}
            AND {jcol} IN ({ph})
        """
        cur.execute(sql, [*scope_params, *chunk])
        rows.extend(cur.fetchall())
    return rows


def _fetch_supra(
    cur: pymssql.Cursor,
    pair: TablePair,
    contracts: list[str],
) -> list[tuple]:
    """Busca linhas atuais do SUPRA nas mesmas colunas que o INSERT usa."""
    jcol = _br(pair.join_supra)
    all_supra = [p.supra for p in pair.pairs]
    for ej in pair.extra_joins:
        all_supra.extend(ec.supra for ec in ej.columns)
    col_list = ", ".join(_br(c) for c in all_supra)
    rows: list[tuple] = []
    for chunk in _chunks(contracts, _CHUNK):
        ph = ", ".join(["%s"] * len(chunk))
        cur.execute(
            f"SELECT {col_list} FROM {pair.supra_table} WHERE {jcol} IN ({ph})",
            chunk,
        )
        rows.extend(cur.fetchall())
    return rows


def _delete_supra(
    cur: pymssql.Cursor,
    pair: TablePair,
    contracts: list[str],
) -> int:
    jcol = _br(pair.join_supra)
    deleted = 0
    for chunk in _chunks(contracts, _CHUNK):
        ph = ", ".join(["%s"] * len(chunk))
        sql = f"DELETE FROM {pair.supra_table} WHERE {jcol} IN ({ph})"
        cur.execute(sql, chunk)
        deleted += cur.rowcount
    return deleted


def _build_insert_sql(pair: TablePair) -> str:
    all_supra = [p.supra for p in pair.pairs]
    for ej in pair.extra_joins:
        all_supra.extend(ec.supra for ec in ej.columns)
    cols = ", ".join(_br(c) for c in all_supra)
    ph = ", ".join(["%s"] * len(all_supra))
    return f"INSERT INTO {pair.supra_table} ({cols}) VALUES ({ph})"


def stamp_injected_cols(
    dst_conn: pymssql.Connection,
    pair: TablePair,
    all_contracts: list[str],
) -> int:
    """
    UPDATE nas colunas injetadas (ex.: dt_atualizacao = GETDATE()) para TODOS os
    contratos gerenciados — inclusive os que não foram alterados no sync.
    Retorna o número de linhas carimbadas.
    """
    # apenas expressões simples (sem {M}) podem ser carimbadas no SUPRA;
    # subqueries com {M} referenciam tabelas do SIMDNIT e já chegam corretas via INSERT
    stamp_pairs = [p for p in pair.pairs if p.is_injected and "{M}" not in p.simdnit]
    if not stamp_pairs or not all_contracts:
        return 0

    set_clause = ", ".join(f"{_br(p.supra)} = {p.inject_expr}" for p in stamp_pairs)
    jcol = _br(pair.join_supra)
    dst_cur = dst_conn.cursor()
    dst_conn.autocommit(False)
    stamped = 0
    try:
        for chunk in _chunks(all_contracts, _CHUNK):
            ph = ", ".join(["%s"] * len(chunk))
            dst_cur.execute(
                f"UPDATE {pair.supra_table} SET {set_clause} WHERE {jcol} IN ({ph})",
                chunk,
            )
            stamped += dst_cur.rowcount
        dst_conn.commit()
    except Exception:
        dst_conn.rollback()
        raise
    finally:
        dst_conn.autocommit(True)
        dst_cur.close()

    return stamped


def dedup_tables(
    dst_conn: pymssql.Connection,
    pairs: "list[TablePair]",
) -> None:
    """Remove linhas exatamente duplicadas (todos os campos iguais) das tabelas antes do sync.

    Usa CTE + ROW_NUMBER particionado por TODAS as colunas — só apaga cópias 100% idênticas.
    Tabelas sem duplicatas passam sem custo (rowcount = 0).
    """
    dst_cur = dst_conn.cursor()
    dst_conn.autocommit(False)
    try:
        for pair in pairs:
            all_supra = [p.supra for p in pair.pairs]
            for ej in pair.extra_joins:
                all_supra.extend(ec.supra for ec in ej.columns)
            if not all_supra:
                continue
            partition_cols = ", ".join(_br(c) for c in all_supra)
            sql = (
                f"WITH _cte AS ("
                f"SELECT ROW_NUMBER() OVER ("
                f"PARTITION BY {partition_cols} "
                f"ORDER BY (SELECT NULL)) AS _rn "
                f"FROM {pair.supra_table}"
                f") DELETE FROM _cte WHERE _rn > 1"
            )
            dst_cur.execute(sql)
            n = dst_cur.rowcount
            if n > 0:
                print(
                    f"  [dedup] {pair.supra_table}: {n:,} linha(s) duplicada(s) removida(s).",
                    flush=True,
                )
        dst_conn.commit()
    except Exception as exc:
        dst_conn.rollback()
        print(f"  [dedup] Aviso: erro ao deduplicar — {exc}", flush=True)
    finally:
        dst_conn.autocommit(True)
        dst_cur.close()


def sync_table(
    src_conn: pymssql.Connection,
    dst_conn: pymssql.Connection,
    pair: TablePair,
    contracts: list[str],
    sg: str = "CGCONT",
    batch_size: int = 500,
    progress_cb: Callable[[int, int], None] | None = None,
) -> SyncResult:
    """
    Sincroniza uma tabela via DELETE + INSERT em lote para os contratos indicados.
    Executa dentro de uma transação por lote; faz rollback total se falhar.
    """
    result = SyncResult(supra_table=pair.supra_table)
    src_cur = src_conn.cursor()
    dst_cur = dst_conn.cursor()

    try:
        # remove contratos protegidos antes de qualquer operação
        protected = set(pair.protected_contracts)
        contracts = [c for c in contracts if c not in protected]
        if not contracts:
            log.info("  Todos os contratos estão protegidos. Nada a sincronizar.")
            return result

        # 1. busca dados do SIMDNIT
        log.info("  Buscando dados SIMDNIT: %s (%d contratos)...", pair.simdnit_table, len(contracts))
        sim_rows = _fetch_simdnit(src_cur, pair, contracts, sg)

        if not sim_rows:
            log.info("  Nenhuma linha no SIMDNIT para esses contratos.")
            return result

        # 2. busca estado atual do SUPRA e decide a estratégia
        supra_rows = _fetch_supra(dst_cur, pair, contracts)

        sim_norm = {_norm_row(tuple(r)) for r in sim_rows}
        sup_norm = {_norm_row(tuple(r)) for r in supra_rows}

        supra_only = sup_norm - sim_norm    # linhas no SUPRA que NÃO estão no SIMDNIT
        simdnit_only_norm = sim_norm - sup_norm  # linhas no SIMDNIT que não estão no SUPRA

        # duplicatas no SUPRA: o set collapsa cópias idênticas — se há mais linhas
        # brutas do que entradas únicas, o SUPRA tem duplicatas para esses contratos
        has_supra_duplicates = len(supra_rows) > len(sup_norm)

        if not supra_only and not simdnit_only_norm and not has_supra_duplicates:
            # tudo idêntico após normalização — nada a fazer
            log.info("  Sem alterações reais após verificação numérica. Nenhuma ação necessária.")
            result.contracts_synced = contracts[:]
            return result

        if not supra_only and not has_supra_duplicates:
            # todas as linhas do SUPRA já existem no SIMDNIT — apenas INSERTs necessários
            # (evita DELETE+reinsert de linhas que já estão corretas)
            rows_to_insert = [r for r in sim_rows if _norm_row(tuple(r)) not in sup_norm]
            contracts_to_delete: list[str] = []
            log.info(
                "  SUPRA é subconjunto do SIMDNIT — apenas %d INSERT(s), sem DELETE.",
                len(rows_to_insert),
            )
        else:
            # há linhas divergentes OU duplicatas no SUPRA → refresh completo por segurança
            rows_to_insert = sim_rows
            contracts_to_delete = contracts
            if has_supra_duplicates:
                log.info(
                    "  %d linha(s) duplicadas no SUPRA → DELETE + INSERT completo para limpar.",
                    len(supra_rows) - len(sup_norm),
                )
            else:
                log.info(
                    "  %d linha(s) do SUPRA divergem → DELETE + INSERT completo.",
                    len(supra_only),
                )

        # 3. executa dentro de transação
        dst_conn.autocommit(False)
        try:
            if contracts_to_delete:
                log.info("  Deletando linhas SUPRA: %s...", pair.supra_table)
                result.deleted = _delete_supra(dst_cur, pair, contracts_to_delete)
                log.info("  %d linhas removidas.", result.deleted)

            ins_sql = _build_insert_sql(pair)
            total = len(rows_to_insert)
            for batch in _chunks(rows_to_insert, batch_size):
                dst_cur.executemany(ins_sql, [tuple(r) for r in batch])
                result.inserted += len(batch)
                if progress_cb:
                    progress_cb(result.inserted, total)

            dst_conn.commit()
            result.contracts_synced = contracts[:]
            log.info("  %d linhas inseridas em %s.", result.inserted, pair.supra_table)

        except Exception as exc:
            dst_conn.rollback()
            raise exc
        finally:
            dst_conn.autocommit(True)

    except Exception as exc:
        msg = str(exc)
        log.error("  FALHA em %s: %s", pair.supra_table, msg)
        result.errors.append(msg)
    finally:
        src_cur.close()
        dst_cur.close()

    return result
