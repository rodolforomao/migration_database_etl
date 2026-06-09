"""Comparação SIMDNIT↔SUPRA por contagem e checksum de linhas."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

import pymssql

from supra_db_update.table_map import TablePair, _br, _sql_expr

log = logging.getLogger(__name__)

_CHUNK = 900
_DADOS_CONTRATO = "dbo.dados_contrato"


def _norm_val(v):
    """
    Normaliza valores numéricos para comparação de sets entre bases.

    SIMDNIT usa FLOAT  → pymssql retorna Python float  (ex.: 1831200.2)
    SUPRA usa MONEY    → pymssql retorna Python Decimal (ex.: Decimal('1831200.2000'))

    Sem normalização, float(1831200.2) != Decimal('1831200.2000') em set comparison,
    fazendo linhas idênticas aparecerem como "diferentes".
    """
    if v is None:
        return v
    if isinstance(v, Decimal):
        try:
            return v.normalize()          # Decimal('1831200.2000') → Decimal('1.8312002E+6')
        except InvalidOperation:
            return v
    if isinstance(v, float):
        try:
            return Decimal(str(v)).normalize()   # float → mesmo canonical que Decimal acima
        except InvalidOperation:
            return v
    return v


def _norm_row(row: tuple) -> tuple:
    return tuple(_norm_val(v) for v in row)


def _build_scope_where(pair: TablePair, sg: str, table_prefix: str = "") -> tuple[str, tuple]:
    """
    Retorna (fragmento WHERE, params) para escopar queries SIMDNIT ao SG_UND_GESTORA correto.

    Dados_Contrato é a tabela mãe e possui SG_UND_GESTORA diretamente.
    Usar subquery IN nela mesma não isola o SG correto quando o mesmo
    NU_CON_FORMATADO existe para múltiplos SGs — então filtramos na própria coluna.

    Tabelas filhas (Dados_Medicao, Dados_Empenho, etc.) não possuem SG_UND_GESTORA,
    por isso usam subquery para buscar os contratos do escopo em Dados_Contrato.

    table_prefix: alias da tabela principal (ex.: "_m") — usado quando há extra_joins.
    """
    p = f"{table_prefix}." if table_prefix else ""
    jcol = f"{p}{_br(pair.join_simdnit)}"
    if pair.simdnit_table.lower() == _DADOS_CONTRATO:
        return f"{p}[SG_UND_GESTORA] = %s", (sg,)
    return (
        f"{jcol} IN (SELECT NU_CON_FORMATADO FROM dbo.Dados_Contrato WHERE SG_UND_GESTORA = %s)",
        (sg,),
    )


# ---------------------------------------------------------------------------
# RowDiff — diff linha a linha entre SIMDNIT e SUPRA para um contrato
# ---------------------------------------------------------------------------

@dataclass
class RowDiff:
    contract: str
    pair: TablePair
    cols: list[str]        # nomes das colunas (ordem de pair.pairs, lado SUPRA)
    added: list[tuple]     # linhas só no SIMDNIT → serão inseridas no SUPRA
    removed: list[tuple]   # linhas só no SUPRA   → serão removidas
    common: int            # linhas idênticas nos dois lados
    error: str = ""
    warning: str = ""      # mapeamento insuficiente para diff preciso


def diff_rows_for_contract(
    src_cur: pymssql.Cursor,
    dst_cur: pymssql.Cursor,
    pair: TablePair,
    contract: str,
    sg: str,
) -> RowDiff:
    """
    Busca todas as linhas do contrato nos dois bancos e retorna a diferença simétrica.
    added  = linhas em SIMDNIT que não estão no SUPRA  → serão inseridas
    removed = linhas no SUPRA que não estão no SIMDNIT → serão removidas
    """
    try:
        jcol_supra = _br(pair.join_supra)

        # colunas não-join do mapeamento principal, excluindo injetadas (=GETDATE() etc.)
        data_pairs = [p for p in pair.pairs if p.supra != pair.join_supra and not p.is_injected]

        # colunas vindas de extra_joins (todas são "dados", nunca join key)
        ej_cols: list[tuple[str, str, int]] = []  # (supra_col, simdnit_col, join_idx)
        for i, ej in enumerate(pair.extra_joins):
            for ec in ej.columns:
                ej_cols.append((ec.supra, ec.simdnit, i))

        if not data_pairs and not ej_cols:
            # sem colunas além da join key — retorna contagens reais
            where_scope, scope_params = _build_scope_where(pair, sg)
            jcol_sim = _br(pair.join_simdnit)
            src_cur.execute(
                f"SELECT COUNT(*) FROM {pair.simdnit_table} WHERE {jcol_sim} = %s AND {where_scope}",
                (contract, *scope_params),
            )
            sim_count = src_cur.fetchone()[0]
            dst_cur.execute(
                f"SELECT COUNT(*) FROM {pair.supra_table} WHERE {jcol_supra} = %s",
                (contract,),
            )
            supra_count = dst_cur.fetchone()[0]
            n_mapped = len(pair.pairs)
            return RowDiff(
                contract=contract,
                pair=pair,
                cols=[],
                added=[],
                removed=[],
                common=0,
                warning=(
                    f"Diff indisponível — {n_mapped} coluna(s) mapeada(s), todas são join key. "
                    f"SIMDNIT={sim_count} linha(s)  SUPRA={supra_count} linha(s). "
                    f"Complete o mapeamento em column_mapping.json para ver o diff."
                ),
            )

        # ── lado SIMDNIT ─────────────────────────────────────────────────────
        if pair.extra_joins:
            where_scope, scope_params = _build_scope_where(pair, sg, table_prefix="_m")
            m_cols = ", ".join(p.simdnit_sql_expr("_m") for p in data_pairs)
            j_col_exprs = [f"_j{idx}.{_br(sc)}" for (_, sc, idx) in ej_cols]
            all_sim_cols = ", ".join(filter(None, [m_cols, ", ".join(j_col_exprs)]))
            join_clauses = "\n".join(
                f"LEFT JOIN {ej.simdnit_table} AS _j{i} "
                f"ON _m.{_br(ej.main_col)} = _j{i}.{_br(ej.join_col)}"
                for i, ej in enumerate(pair.extra_joins)
            )
            sim_jcol = f"_m.{_br(pair.join_simdnit)}"
            from_sim = f"{pair.simdnit_table} AS _m\n{join_clauses}"
        elif pair.needs_main_alias:
            where_scope, scope_params = _build_scope_where(pair, sg, table_prefix="_m")
            all_sim_cols = ", ".join(p.simdnit_sql_expr("_m") for p in data_pairs)
            sim_jcol = f"_m.{_br(pair.join_simdnit)}"
            from_sim = f"{pair.simdnit_table} AS _m"
        else:
            where_scope, scope_params = _build_scope_where(pair, sg)
            all_sim_cols = ", ".join(p.simdnit_sql_expr() for p in data_pairs)
            sim_jcol = _br(pair.join_simdnit)
            from_sim = pair.simdnit_table

        src_cur.execute(
            f"SELECT {all_sim_cols} FROM {from_sim} WHERE {sim_jcol} = %s AND {where_scope}",
            (contract, *scope_params),
        )
        sim_rows: set[tuple] = {tuple(r) for r in src_cur.fetchall()}

        # ── lado SUPRA ───────────────────────────────────────────────────────
        supra_col_exprs = [_br(p.supra) for p in data_pairs] + [_br(sc) for (sc, _, _) in ej_cols]
        col_list_supra = ", ".join(supra_col_exprs)
        dst_cur.execute(
            f"SELECT {col_list_supra} FROM {pair.supra_table} WHERE {jcol_supra} = %s",
            (contract,),
        )
        supra_rows: set[tuple] = {tuple(r) for r in dst_cur.fetchall()}

        all_col_names = [p.supra for p in data_pairs] + [sc for (sc, _, _) in ej_cols]

        def _sort_key(r: tuple) -> list:
            return [str(v) if v is not None else "" for v in r]

        # ── 1ª passagem: comparação exata (comportamento original) ───────────
        added_raw  = sim_rows  - supra_rows   # apenas em SIMDNIT
        removed_raw = supra_rows - sim_rows   # apenas em SUPRA
        common_exact = len(sim_rows & supra_rows)

        # ── 2ª passagem: filtra "fantasmas" de precisão numérica ─────────────
        # SIMDNIT usa FLOAT, SUPRA usa MONEY/DECIMAL → mesmo número, tipos Python diferentes.
        # Antes de marcar como mudança, re-verifica com valores normalizados.
        if added_raw or removed_raw:
            supra_norm = {_norm_row(r) for r in supra_rows}
            sim_norm   = {_norm_row(r) for r in sim_rows}

            # mantém só os que continuam diferentes após normalização
            added_real   = [r for r in added_raw   if _norm_row(r) not in supra_norm]
            removed_real = [r for r in removed_raw if _norm_row(r) not in sim_norm]

            # pares que eram "diferentes" só por precisão numérica → viram common
            phantom = len(added_raw) - len(added_real)
        else:
            added_real   = []
            removed_real = []
            phantom      = 0

        return RowDiff(
            contract=contract,
            pair=pair,
            cols=all_col_names,
            added=sorted(added_real,   key=_sort_key),
            removed=sorted(removed_real, key=_sort_key),
            common=common_exact + phantom,
        )
    except Exception as exc:
        return RowDiff(
            contract=contract,
            pair=pair,
            cols=[p.supra for p in pair.pairs],
            added=[],
            removed=[],
            common=0,
            error=str(exc),
        )


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


# ---------------------------------------------------------------------------
# Ação determinada por contrato
# ---------------------------------------------------------------------------

def _action(sim: int, supra: int, chk: bool | None) -> str:
    if sim > 0 and supra == 0:
        return "INSERT"
    if sim == 0 and supra > 0:
        return "DELETE"
    if sim != supra:
        return "D/I"    # DELETE + INSERT (contagens diferentes)
    if chk is False:
        return "UPDATE"  # mesma contagem mas valores divergem
    return "OK"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ContractDiff:
    contract: str
    simdnit_count: int
    supra_count: int
    checksum_match: bool | None  # None = deep-check não solicitado
    excluded: bool = False       # contrato protegido — não será tocado

    @property
    def action(self) -> str:
        return _action(self.simdnit_count, self.supra_count, self.checksum_match)

    @property
    def needs_sync(self) -> bool:
        return not self.excluded and self.action != "OK"


@dataclass
class TableDiff:
    pair: TablePair
    simdnit_total: int
    supra_total: int
    by_contract: list[ContractDiff] = field(default_factory=list)
    error: str = ""
    warning: str = ""  # escopo vazio — comparação não realizada

    # ── totais por ação (apenas contratos activos, não protegidos) ──────────

    @property
    def active_changed(self) -> list[ContractDiff]:
        return [c for c in self.by_contract if c.needs_sync]

    @property
    def protected_changed(self) -> list[ContractDiff]:
        """Contratos que mudariam mas estão protegidos."""
        return [
            c for c in self.by_contract
            if c.excluded and c.action != "OK"
        ]

    @property
    def needs_sync(self) -> bool:
        if self.error:
            return False
        return bool(self.active_changed)

    @property
    def delta(self) -> int:
        return self.simdnit_total - self.supra_total

    @property
    def changed_contracts(self) -> list[str]:
        """Contratos activos com diferença (usados pelo migrator)."""
        return [c.contract for c in self.active_changed]

    @property
    def status_label(self) -> str:
        if self.error:
            return f"ERRO: {self.error[:60]}"
        if self.warning:
            return f"AVISO: {self.warning[:80]}"
        if not self.needs_sync:
            prot = len(self.protected_changed)
            suffix = f"  [{prot} protegido(s) ignorado(s)]" if prot else ""
            return f"OK{suffix}"
        d = self.delta
        sign = "+" if d >= 0 else ""
        prot = len(self.protected_changed)
        suffix = f"  [{prot} protegido(s)]" if prot else ""
        return f"DIFF ({sign}{d:,} linhas){suffix}"


# ---------------------------------------------------------------------------
# Queries auxiliares
# ---------------------------------------------------------------------------

def _count_per_contract_simdnit(
    cur: pymssql.Cursor, pair: TablePair, sg: str
) -> dict[str, int]:
    # quando há extra_joins, o COUNT precisa usar o mesmo JOIN que o sync usa,
    # pois o JOIN pode multiplicar linhas (ex.: Dados_Oficio_Pagamento)
    if pair.extra_joins:
        where, params = _build_scope_where(pair, sg, table_prefix="_m")
        jcol = f"_m.{_br(pair.join_simdnit)}"
        join_clauses = "\n".join(
            f"LEFT JOIN {ej.simdnit_table} AS _j{i} "
            f"ON _m.{_br(ej.main_col)} = _j{i}.{_br(ej.join_col)}"
            for i, ej in enumerate(pair.extra_joins)
        )
        from_clause = f"{pair.simdnit_table} AS _m\n{join_clauses}"
    elif pair.needs_main_alias:
        where, params = _build_scope_where(pair, sg, table_prefix="_m")
        jcol = f"_m.{_br(pair.join_simdnit)}"
        from_clause = f"{pair.simdnit_table} AS _m"
    else:
        jcol = _br(pair.join_simdnit)
        where, params = _build_scope_where(pair, sg)
        from_clause = pair.simdnit_table

    sql = f"""
        SELECT {jcol}, COUNT(*) AS cnt
        FROM {from_clause}
        WHERE {where}
        GROUP BY {jcol}
    """
    cur.execute(sql, params)
    return {str(r[0]): int(r[1]) for r in cur.fetchall()}


def _count_per_contract_supra(
    cur: pymssql.Cursor, pair: TablePair, contracts: list[str]
) -> dict[str, int]:
    jcol = _br(pair.join_supra)
    result: dict[str, int] = {}
    for chunk in _chunks(contracts, _CHUNK):
        ph = ", ".join(["%s"] * len(chunk))
        sql = f"""
            SELECT {jcol}, COUNT(*) AS cnt
            FROM {pair.supra_table}
            WHERE {jcol} IN ({ph})
            GROUP BY {jcol}
        """
        cur.execute(sql, chunk)
        for r in cur.fetchall():
            result[str(r[0])] = int(r[1])
    return result


def _checksum_per_contract_simdnit(
    cur: pymssql.Cursor, pair: TablePair, sg: str
) -> dict[str, int]:
    real_pairs = [p for p in pair.pairs if not p.is_injected]
    if pair.extra_joins:
        where, params = _build_scope_where(pair, sg, table_prefix="_m")
        jcol = f"_m.{_br(pair.join_simdnit)}"
        m_chk = ", ".join(p.simdnit_sql_expr("_m") for p in real_pairs)
        ej_chk = ", ".join(
            f"_j{i}.{_br(ec.simdnit)}"
            for i, ej in enumerate(pair.extra_joins)
            for ec in ej.columns
        )
        chk_cols = ", ".join(filter(None, [m_chk, ej_chk]))
        join_clauses = "\n".join(
            f"LEFT JOIN {ej.simdnit_table} AS _j{i} "
            f"ON _m.{_br(ej.main_col)} = _j{i}.{_br(ej.join_col)}"
            for i, ej in enumerate(pair.extra_joins)
        )
        from_clause = f"{pair.simdnit_table} AS _m\n{join_clauses}"
    elif pair.needs_main_alias:
        jcol = f"_m.{_br(pair.join_simdnit)}"
        chk_cols = ", ".join(p.simdnit_sql_expr("_m") for p in real_pairs)
        where, params = _build_scope_where(pair, sg, table_prefix="_m")
        from_clause = f"{pair.simdnit_table} AS _m"
    else:
        jcol = _br(pair.join_simdnit)
        chk_cols = ", ".join(p.simdnit_sql_expr() for p in real_pairs)
        where, params = _build_scope_where(pair, sg)
        from_clause = pair.simdnit_table

    sql = f"""
        SELECT {jcol},
               CHECKSUM_AGG(BINARY_CHECKSUM({chk_cols})) AS chk
        FROM {from_clause}
        WHERE {where}
        GROUP BY {jcol}
    """
    cur.execute(sql, params)
    return {str(r[0]): r[1] for r in cur.fetchall()}


def _checksum_per_contract_supra(
    cur: pymssql.Cursor, pair: TablePair, contracts: list[str]
) -> dict[str, int]:
    jcol = _br(pair.join_supra)
    main_chk = [_br(p.supra) for p in pair.pairs if not p.is_injected]
    ej_chk = [_br(ec.supra) for ej in pair.extra_joins for ec in ej.columns]
    chk_cols = ", ".join(main_chk + ej_chk)
    result: dict[str, int] = {}
    for chunk in _chunks(contracts, _CHUNK):
        ph = ", ".join(["%s"] * len(chunk))
        sql = f"""
            SELECT {jcol},
                   CHECKSUM_AGG(BINARY_CHECKSUM({chk_cols})) AS chk
            FROM {pair.supra_table}
            WHERE {jcol} IN ({ph})
            GROUP BY {jcol}
        """
        cur.execute(sql, chunk)
        for r in cur.fetchall():
            result[str(r[0])] = r[1]
    return result


# ---------------------------------------------------------------------------
# compare_table — ponto de entrada público
# ---------------------------------------------------------------------------

def compare_table(
    src_cur: pymssql.Cursor,
    dst_cur: pymssql.Cursor,
    pair: TablePair,
    sg: str = "CGCONT",
    deep: bool = False,
) -> TableDiff:
    try:
        sim_cnt = _count_per_contract_simdnit(src_cur, pair, sg)
        managed = list(sim_cnt.keys())

        if not managed:
            if pair.simdnit_table.lower() == _DADOS_CONTRATO:
                # tabela raiz: podemos contar o SUPRA diretamente
                dst_cur.execute(f"SELECT COUNT(*) FROM {pair.supra_table}")
                supra_total = int(dst_cur.fetchone()[0])
                msg = (
                    f"Dados_Contrato vazia (SG={sg!r}). "
                    f"SUPRA tem {supra_total:,} linha(s) — escopo SIMDNIT inexistente."
                )
            else:
                # tabela filha: escopo depende de Dados_Contrato; sem contratos não
                # podemos afirmar nada sobre o SUPRA — não executamos COUNT sem filtro
                supra_total = 0
                msg = (
                    f"Escopo vazio (SG={sg!r}) — depende de Dados_Contrato. "
                    "Estado do SUPRA não verificado."
                )
            return TableDiff(
                pair=pair,
                simdnit_total=0,
                supra_total=supra_total,
                warning=msg,
            )

        supra_cnt = _count_per_contract_supra(dst_cur, pair, managed)

        all_contracts = sorted(set(sim_cnt) | set(supra_cnt))

        sim_chk = supra_chk = {}
        if deep:
            sim_chk = _checksum_per_contract_simdnit(src_cur, pair, sg)
            supra_chk = _checksum_per_contract_supra(dst_cur, pair, managed)

        protected = set(pair.protected_contracts)

        by_contract: list[ContractDiff] = []
        for c in all_contracts:
            s_cnt = sim_cnt.get(c, 0)
            d_cnt = supra_cnt.get(c, 0)
            chk_match: bool | None = None
            if deep and s_cnt == d_cnt:
                chk_match = sim_chk.get(c) == supra_chk.get(c)
            by_contract.append(
                ContractDiff(
                    contract=c,
                    simdnit_count=s_cnt,
                    supra_count=d_cnt,
                    checksum_match=chk_match,
                    excluded=(c in protected),
                )
            )

        return TableDiff(
            pair=pair,
            simdnit_total=sum(sim_cnt.values()),
            supra_total=sum(supra_cnt.values()),
            by_contract=by_contract,
        )

    except Exception as exc:
        log.debug("Erro comparando %s: %s", pair.supra_table, exc)
        return TableDiff(pair=pair, simdnit_total=0, supra_total=0, error=str(exc))
