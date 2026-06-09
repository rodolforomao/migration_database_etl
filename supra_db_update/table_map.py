"""
Carrega o mapeamento SUPRA↔SIMDNIT.

Fonte primária: column_mapping.json (raiz do projeto) — editável manualmente.
Fallback: reports/inferencia_colunas_por_contrato_*.json — gerado automaticamente.

Para regenerar o column_mapping.json:
    python scripts/generate_column_mapping.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from supra_db_update._paths import runtime_root as _runtime_root
_ROOT = _runtime_root()
_COLUMN_MAPPING = _ROOT / "column_mapping.json"
_INFERENCE_GLOB = "inferencia_colunas_por_contrato_*.json"

_UNSCOPEABLE = "sem NU_CON_FORMATADO nem SK_CONTRATO"
_CONTRACT_REF_COLS = {"NU_CON_FORMATADO", "CONTRATO", "NU_CONTRATO"}


@dataclass(frozen=True)
class ColumnPair:
    supra: str
    simdnit: str
    source: str  # "homonym" | "semantic" | "inferred" | "manual" | "none"
    null_if: str | None = None  # valor sentinel → converte para NULL (ex.: "1900-01-01")

    @property
    def is_injected(self) -> bool:
        """Coluna cujo valor vem de uma expressão SQL (ex.: '=GETDATE()'), não do SIMDNIT."""
        return bool(self.simdnit) and self.simdnit.startswith("=")

    @property
    def inject_expr(self) -> str:
        """Expressão SQL a injetar (sem o '=' inicial). Ex.: 'GETDATE()'."""
        return self.simdnit[1:] if self.is_injected else ""

    def simdnit_sql_expr(self, table_prefix: str = "") -> str:
        """
        Expressão SQL completa para SELECT do lado SIMDNIT.

        table_prefix: alias da tabela (ex. '_m') — usado quando há extra_joins.
        Retorna exemplos:
          - coluna simples:   [DT_BASE]            ou  _m.[DT_BASE]
          - com null_if:      NULLIF([DT_BASE], '1900-01-01')
          - injetada:         GETDATE()            (nunca leva prefixo)
          - injetada com {M}: subquery com alias resolvido para table_prefix ou '_m'
        """
        if self.is_injected:
            expr = self.inject_expr
            if "{M}" in expr:
                alias = table_prefix if table_prefix else "_m"
                expr = expr.replace("{M}", alias)
            return expr

        p = f"{table_prefix}." if table_prefix else ""
        base = f"{p}{_sql_expr(self.simdnit)}"

        if self.null_if is not None:
            try:
                float(self.null_if)
                quoted = self.null_if          # número: sem aspas
            except ValueError:
                quoted = f"'{self.null_if}'"   # string/data: com aspas simples
            return f"NULLIF({base}, {quoted})"

        return base


@dataclass(frozen=True)
class ExtraJoinColumn:
    supra: str
    simdnit: str  # coluna na tabela extra do SIMDNIT


@dataclass
class ExtraJoin:
    simdnit_table: str
    main_col: str   # coluna em _m (tabela principal) para o ON
    join_col: str   # coluna em _jN (tabela extra) para o ON
    columns: list[ExtraJoinColumn]


@dataclass
class TablePair:
    supra_table: str
    simdnit_table: str
    pairs: list[ColumnPair]
    join_supra: str    # coluna de contrato no SUPRA  (ex.: "contrato")
    join_simdnit: str  # coluna de contrato no SIMDNIT (ex.: "NU_CON_FORMATADO")
    protected_contracts: list[str] = field(default_factory=list)
    extra_joins: list[ExtraJoin] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.supra_table

    @property
    def all_pairs(self) -> list[ColumnPair]:
        return self.pairs

    @property
    def needs_main_alias(self) -> bool:
        """True se alguma coluna injetada usa {M} — força alias _m na tabela principal."""
        return any("{M}" in p.simdnit for p in self.pairs if p.is_injected)


def _br(name: str) -> str:
    return f"[{name.replace(']', ']]')}]"


def _sql_expr(name: str) -> str:
    """Retorna a expressão SQL sem colchetes se for uma fórmula (contém operador ou espaço)."""
    if any(op in name for op in (" ", "+", "-", "*", "/")):
        return name
    return _br(name)


# ---------------------------------------------------------------------------
# Loader: column_mapping.json (formato editável)
# ---------------------------------------------------------------------------

def _load_from_column_mapping(path: Path) -> list[TablePair]:
    data: dict = json.loads(path.read_text(encoding="utf-8"))

    scope = data.get("scope", {})

    # contratos globalmente excluídos (valem para todas as tabelas)
    global_protected: set[str] = set(scope.get("excluded_contracts", []))

    # tabelas ignoradas globalmente (ex.: vazias ou fora do escopo)
    ignored_tables: set[str] = {t.lower() for t in scope.get("ignored_tables", [])}

    result: list[TablePair] = []
    for tbl in data.get("tables", []):
        if not tbl.get("enabled", True):
            continue
        if tbl.get("supra", "").lower() in ignored_tables:
            continue

        pairs: list[ColumnPair] = []
        join_supra = join_simdnit = None

        for col in tbl.get("columns", []):
            if not col.get("enabled", True):
                continue
            simdnit_col: str | None = col.get("simdnit_col")
            if not simdnit_col:
                continue

            pair = ColumnPair(
                supra=col["supra_col"],
                simdnit=simdnit_col,
                source=col.get("source", "manual"),
                null_if=col.get("null_if") or None,
            )
            pairs.append(pair)

            if col.get("is_join_key"):
                join_supra = col["supra_col"]
                join_simdnit = simdnit_col

        if not pairs or not join_supra:
            continue

        # parse extra_joins
        extra_joins: list[ExtraJoin] = []
        for ej_raw in tbl.get("extra_joins", []):
            ej_cols: list[ExtraJoinColumn] = []
            for ec in ej_raw.get("columns", []):
                if not ec.get("enabled", True):
                    continue
                if not ec.get("simdnit_col") or not ec.get("supra_col"):
                    continue
                ej_cols.append(ExtraJoinColumn(supra=ec["supra_col"], simdnit=ec["simdnit_col"]))
            if not ej_cols:
                continue
            on = ej_raw.get("on", {})
            extra_joins.append(ExtraJoin(
                simdnit_table=ej_raw["simdnit_table"],
                main_col=on.get("main_col", ""),
                join_col=on.get("join_col", ""),
                columns=ej_cols,
            ))

        # merge: exclusões globais + por tabela
        protected = sorted(
            global_protected | set(tbl.get("protected_contracts", []))
        )

        result.append(
            TablePair(
                supra_table=tbl["supra"],
                simdnit_table=tbl["simdnit"],
                pairs=pairs,
                join_supra=join_supra,
                join_simdnit=join_simdnit,
                protected_contracts=protected,
                extra_joins=extra_joins,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Loader: inferencia_*.json (fallback / legado)
# ---------------------------------------------------------------------------

def _find_inference() -> Path | None:
    candidates = sorted((_ROOT / "reports").glob(_INFERENCE_GLOB))
    return candidates[-1] if candidates else None


def _load_from_inference(path: Path) -> list[TablePair]:
    data: list[dict] = json.loads(path.read_text(encoding="utf-8"))
    result: list[TablePair] = []

    for entry in data:
        fs = entry.get("fetch_supra", "")
        fd = entry.get("fetch_simdnit", "")
        if _UNSCOPEABLE in fs or _UNSCOPEABLE in fd:
            continue

        col_pairs: list[ColumnPair] = []
        for src, key in (
            ("homonym", "pairs_homonym"),
            ("semantic", "pairs_semantic"),
            ("inferred", "pairs_inferidos_valor"),
        ):
            for p in entry.get(key, []):
                col_pairs.append(ColumnPair(p["supra"], p["simdnit"], src))

        if not col_pairs:
            continue

        join_supra = join_simdnit = None
        for p in col_pairs:
            if p.simdnit.upper() in _CONTRACT_REF_COLS:
                join_supra = p.supra
                join_simdnit = p.simdnit
                break

        if not join_supra:
            continue

        result.append(
            TablePair(
                supra_table=entry["supra_table"],
                simdnit_table=entry["simdnit_table"],
                pairs=col_pairs,
                join_supra=join_supra,
                join_simdnit=join_simdnit,
            )
        )

    return result


# ---------------------------------------------------------------------------
# Ponto de entrada público
# ---------------------------------------------------------------------------

def load_table_map(path: Path | None = None) -> list[TablePair]:
    """
    Carrega o mapeamento de tabelas sincronizáveis.

    Prioridade:
      1. path explícito (se fornecido)
      2. column_mapping.json na raiz do projeto
      3. inferencia_colunas_por_contrato_*.json em reports/ (fallback)
    """
    if path is not None:
        # detecta o formato pelo nome
        if path.name == "column_mapping.json":
            return _load_from_column_mapping(path)
        return _load_from_inference(path)

    if _COLUMN_MAPPING.is_file():
        return _load_from_column_mapping(_COLUMN_MAPPING)

    inf = _find_inference()
    if inf:
        return _load_from_inference(inf)

    return []
