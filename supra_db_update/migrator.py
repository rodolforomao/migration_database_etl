"""Executa a migração SIMDNIT→SUPRA em lotes (DELETE + INSERT por contrato)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

import pyodbc

from supra_db_update.differ import _build_scope_where
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
    cur: pyodbc.Cursor,
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
        ph = ", ".join(["?"] * len(chunk))
        sql = f"""
            SELECT {all_cols}
            FROM {from_clause}
            WHERE {where_scope}
            AND {jcol} IN ({ph})
        """
        cur.execute(sql, [*scope_params, *chunk])
        rows.extend(cur.fetchall())
    return rows


def _delete_supra(
    cur: pyodbc.Cursor,
    pair: TablePair,
    contracts: list[str],
) -> int:
    jcol = _br(pair.join_supra)
    deleted = 0
    for chunk in _chunks(contracts, _CHUNK):
        ph = ", ".join(["?"] * len(chunk))
        sql = f"DELETE FROM {pair.supra_table} WHERE {jcol} IN ({ph})"
        cur.execute(sql, chunk)
        deleted += cur.rowcount
    return deleted


def _build_insert_sql(pair: TablePair) -> str:
    all_supra = [p.supra for p in pair.pairs]
    for ej in pair.extra_joins:
        all_supra.extend(ec.supra for ec in ej.columns)
    cols = ", ".join(_br(c) for c in all_supra)
    ph = ", ".join(["?"] * len(all_supra))
    return f"INSERT INTO {pair.supra_table} ({cols}) VALUES ({ph})"


def stamp_injected_cols(
    dst_conn: pyodbc.Connection,
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
    dst_conn.autocommit = False
    stamped = 0
    try:
        for chunk in _chunks(all_contracts, _CHUNK):
            ph = ", ".join(["?"] * len(chunk))
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
        dst_conn.autocommit = True
        dst_cur.close()

    return stamped


def sync_table(
    src_conn: pyodbc.Connection,
    dst_conn: pyodbc.Connection,
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
        rows = _fetch_simdnit(src_cur, pair, contracts, sg)

        if not rows:
            log.info("  Nenhuma linha no SIMDNIT para esses contratos.")
            return result

        # 2. DELETE no SUPRA (dentro de transação)
        dst_conn.autocommit = False
        try:
            log.info("  Deletando linhas SUPRA: %s...", pair.supra_table)
            result.deleted = _delete_supra(dst_cur, pair, contracts)
            log.info("  %d linhas removidas.", result.deleted)

            # 3. INSERT em lotes
            ins_sql = _build_insert_sql(pair)
            total = len(rows)
            for i, batch in enumerate(_chunks(rows, batch_size)):
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
            dst_conn.autocommit = True

    except Exception as exc:
        msg = str(exc)
        log.error("  FALHA em %s: %s", pair.supra_table, msg)
        result.errors.append(msg)
    finally:
        src_cur.close()
        dst_cur.close()

    return result
