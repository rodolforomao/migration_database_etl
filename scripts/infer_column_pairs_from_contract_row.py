#!/usr/bin/env python3
"""
Âmbito SIMDNIT: só contratos presentes em dbo.Dados_Contrato com SG_UND_GESTORA = valor de
SIMDNIT_SCOPE_SG_UND_GESTORA no .env (predefinição CGCONT). --nu tem de existir nesse conjunto.

1) Pares homónimos (já no JSON).
2) Pares semânticos fixos (âncora + domínio típico de Dados_Contrato / tabelas base SIMDNIT): ver SEMANTIC_SUPRA_TO_SIM.
   Não se usam tabelas SIMDNIT `SUPRA_CGCONT_*`: escolhe-se o primeiro alvo entre `primary_simdnit` e `simdnit_candidates` que não seja desse prefixo.
   Ex.: situacao_contrato ↔ DS_FAS_CONTRATO mesmo quando o texto difere na linha; unidade_gestora ↔ SG_UND_GESTORA vs ambiguidade por valor.
3) Pares por valor na tabela SIMDNIT mapeada (candidato único).
4) Colunas ainda sem par: procura o valor na linha noutras tabelas SIMDNIT (âmbito acima), exc. tabela primária do par.
5) TB_SIAC_CONTRATO.trecho: concatenação dbo.Dados_Segmento.DS_TRECHO (ORDER BY SK_SEGMENTO, sep \" - \").

Gera:
  reports/inferencia_colunas_por_contrato_<sanitized>.json
  reports/tb_siac_colunas_sem_par.txt  (recalculado: exclui homónimo + semântico + inferido)

Uso:
  python scripts/infer_column_pairs_from_contract_row.py --nu "00 00799/2025"

Tabelas SUPRA excluídas (cópia/arquivo): ver `supra_db_update/tb_siac_scope.py`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from supra_db_update.config import load_env, simdnit_endpoint, supra_local_endpoint
from supra_db_update.connection import connect_endpoint
from supra_db_update.simdnit_scope import (
    get_sk_contrato_scoped,
    sql_where_scoped_first_predicates,
    validate_nu_in_scoped_dados_contrato,
    scope_sg_und_gestora,
)
from supra_db_update.tb_siac_scope import include_tb_siac_for_analysis

MAPPING_JSON = ROOT / "reports" / "tb_siac_mapping_final.json"

# SUPRA (minúsculo) -> candidatos SIMDNIT em ordem de preferência (minúsculo, como em INFORMATION_SCHEMA).
# Só entra em `merged` se a coluna existir na tabela SIM mapeada. Vários SUP podem partilhar a mesma coluna SIM.
SEMANTIC_SUPRA_TO_SIM: dict[str, tuple[str, ...]] = {
    "contrato": ("nu_con_formatado", "contrato"),
    "id_siac_contrato": ("sk_contrato",),
    # Situação / fase (textos podem não coincidir na mesma linha; o vínculo é de negócio)
    "situacao_contrato": ("ds_fas_contrato", "sk_situacao_contrato"),
    "unidade_gestora": ("sg_und_gestora", "nm_und_gestora"),
    "dt_termino_atualizada": ("dt_ter_atz",),
    "lote_contrato": ("nu_lote_licitacao",),
    "vr_inicial": ("valor_inicial",),
    "vr_reajuste": ("valor_total_de_reajuste",),
    "vr_empenho": ("valor_empenhado",),
    "saldo_empenho": ("valor_saldo",),
    "uf_unidade_local": ("sg_uf_unidade_local", "sg_uf"),
    "unidade_local": ("sg_und_local", "nm_und_local", "sg_und_fiscal", "nm_und_fiscal"),
    "qtd_dias_paralisados": ("nu_dia_paralisacao",),
    "qtd_dias_prorrogacao": ("nu_dia_prorrogacao",),
    "vr_total_aditivo": ("valor_total_de_aditivos",),
    "vr_total_reajuste": ("valor_total_de_reajuste",),
    "vr_inicial_aditivo_reajuste": ("valor_inicial_adit_reajustes",),
    "vr_saldo": ("valor_saldo",),
    "vr_medicao_pi_mais_r": (
        "valor_medicao_pi_r",
        "valor_medicao_pi_r_ajuste_acumulado",
    ),
    "vr_pi_medicao": ("valor_pi_medicao",),
    "vr_reajuste_medicao": ("valor_reajuste_medicao",),
}


def table_columns(conn, sch: str, tbl: str) -> dict[str, str]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        (sch, tbl),
    )
    return {r[0].lower(): r[0] for r in cur.fetchall()}


def is_supra_cgcont_simdnit_table(_schema: str, table: str) -> bool:
    """Views/tabelas `SUPRA_CGCONT_*` excluídas da análise por pedido do projeto."""
    return table.upper().startswith("SUPRA_CGCONT_")


def resolve_simdnit_table(meta: dict) -> str | None:
    """
    Primeira tabela entre primary + candidates que não seja SUPRA_CGCONT_*.
    Se só existirem CGCONT, devolve None (bloco SUPRA não entra no relatório).
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for q in [meta.get("primary_simdnit")] + (meta.get("simdnit_candidates") or []):
        if not q or q in seen:
            continue
        seen.add(q)
        ordered.append(q)
    for q in ordered:
        sch, tbl = parse_q(q)
        if is_supra_cgcont_simdnit_table(sch, tbl):
            continue
        return q
    return None


def br(s: str) -> str:
    return "[" + s.replace("]", "]]") + "]"


def safe_ident(s: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_.]+$", s))


def fetch_supra_anchor(
    cs, sch: str, tbl: str, nu: str, cid: int | None
) -> tuple[dict[str, Any], str]:
    if not (safe_ident(sch) and safe_ident(tbl)):
        return {}, "identificador inválido"
    cols = table_columns(cs, sch, tbl)
    cur = cs.cursor()
    try:
        if "contrato" in cols:
            c = cols["contrato"]
            cur.execute(
                f"SELECT TOP 1 * FROM {br(sch)}.{br(tbl)} WHERE {br(c)} = ?",
                (nu,),
            )
        elif "co_contrato" in cols and cid is not None:
            c = cols["co_contrato"]
            cur.execute(
                f"SELECT TOP 1 * FROM {br(sch)}.{br(tbl)} WHERE {br(c)} = ?",
                (cid,),
            )
        else:
            return {}, "sem coluna contrato nem CO_CONTRATO na tabela SUPRA"
        row = cur.fetchone()
        return row_to_dict(cur, row), "ok" if row else "sem linha supra"
    except Exception as e:
        return {}, str(e)


def parse_q(q: str) -> tuple[str, str]:
    a, b = q.split(".", 1)
    return a, b


def row_to_dict(cur, row) -> dict[str, Any]:
    if row is None:
        return {}
    cols = [d[0] for d in cur.description]
    return {cols[i]: row[i] for i in range(len(cols))}


def norm_val(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-8", errors="replace")
        except Exception:
            return None
    if isinstance(v, str):
        s = " ".join(v.split())
        return s.casefold() if s else None
    if isinstance(v, Decimal):
        try:
            return str(round(float(v), 8))
        except Exception:
            return str(v)
    if isinstance(v, float):
        return str(round(v, 8))
    if isinstance(v, int):
        return str(v)
    dv = getattr(v, "isoformat", None)
    if callable(dv):
        try:
            s = v.isoformat(sep=" ")
            if len(s) > 19:
                s = s[:19]
            return s.casefold()
        except Exception:
            return str(v).casefold()
    return str(v).strip().casefold() or None


def collect_for_match(d: dict[str, Any]) -> dict[str, str | None]:
    return {k: norm_val(v) for k, v in d.items()}


def homonym_pairs(meta: dict) -> set[tuple[str, str]]:
    out = set()
    for p in meta.get("column_pairs_exact_name") or []:
        out.add((p["supra"].lower(), p["simdnit"].lower()))
    return out


def schema_semantic_pairs(
    sup_schema_lower: dict[str, str], sim_schema_lower: dict[str, str]
) -> tuple[set[tuple[str, str]], list[dict[str, str]]]:
    """Pares semânticos fixos quando a coluna existe em ambos (esquema), sem precisar da linha."""
    sup_l = set(sup_schema_lower.keys())
    sim_l = set(sim_schema_lower.keys())
    merged: set[tuple[str, str]] = set()
    display: list[dict[str, str]] = []
    for sup_key, sim_options in SEMANTIC_SUPRA_TO_SIM.items():
        if sup_key not in sup_l:
            continue
        for opt in sim_options:
            if opt in sim_l:
                merged.add((sup_key, opt))
                display.append(
                    {
                        "supra": sup_schema_lower[sup_key],
                        "simdnit": sim_schema_lower[opt],
                    }
                )
                break
    return merged, display


def invert_index(norm_map: dict[str, str | None]) -> dict[str, list[str]]:
    inv: dict[str, list[str]] = {}
    for col, nv in norm_map.items():
        if nv is None:
            continue
        inv.setdefault(nv, []).append(col)
    return inv


def infer_value_pairs(
    sup_norm: dict[str, str | None],
    sim_norm: dict[str, str | None],
    already: set[tuple[str, str]],
) -> list[tuple[str, str, str]]:
    used_sim = {b for a, b in already}
    used_sup = {a for a, b in already}
    inv_sim = invert_index(sim_norm)
    inferred = []
    for sup_col, nv in sup_norm.items():
        sl = sup_col.lower()
        if sl in used_sup or nv is None:
            continue
        candidates = [c for c in inv_sim.get(nv, []) if c.lower() not in used_sim]
        if len(candidates) == 1:
            sim_c = candidates[0]
            inferred.append((sup_col, sim_c, "valor_igual"))
            used_sup.add(sl)
            used_sim.add(sim_c.lower())
    return inferred


def fetch_simdnit_row(
    cm, sch: str, tbl: str, nu: str, sk: int | None
) -> tuple[dict[str, Any], str]:
    if not (safe_ident(sch) and safe_ident(tbl)):
        return {}, "identificador inválido"
    cur = cm.cursor()
    cur.execute(
        """
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        """,
        (sch, tbl),
    )
    sim_cols = {r[0].lower(): r[0] for r in cur.fetchall()}
    al = "t"
    try:
        if "nu_con_formatado" in sim_cols:
            c = sim_cols["nu_con_formatado"]
            pred = f"{br(al)}.{br(c)} = ?"
            wh, prm = sql_where_scoped_first_predicates(
                pred, (nu,), sch, tbl, al, sim_cols
            )
            cur.execute(
                f"SELECT TOP 1 * FROM {br(sch)}.{br(tbl)} AS {br(al)} {wh}",
                prm,
            )
        elif sk is not None and "sk_contrato" in sim_cols:
            c = sim_cols["sk_contrato"]
            pred = f"{br(al)}.{br(c)} = ?"
            wh, prm = sql_where_scoped_first_predicates(
                pred, (sk,), sch, tbl, al, sim_cols
            )
            cur.execute(
                f"SELECT TOP 1 * FROM {br(sch)}.{br(tbl)} AS {br(al)} {wh}",
                prm,
            )
        else:
            return {}, "sem NU_CON_FORMATADO nem SK_CONTRATO na tabela SIMDNIT"
        row = cur.fetchone()
        return row_to_dict(cur, row), "ok" if row else "sem linha simdnit"
    except Exception as e:
        return {}, str(e)


def anchorable_simdnit_tables(cm) -> list[tuple[str, str]]:
    cur = cm.cursor()
    cur.execute(
        """
        SELECT DISTINCT c.TABLE_SCHEMA, c.TABLE_NAME
        FROM INFORMATION_SCHEMA.COLUMNS c
        INNER JOIN INFORMATION_SCHEMA.TABLES t
          ON c.TABLE_SCHEMA = t.TABLE_SCHEMA AND c.TABLE_NAME = t.TABLE_NAME
        WHERE t.TABLE_SCHEMA = N'dbo' AND t.TABLE_TYPE = N'BASE TABLE'
          AND c.COLUMN_NAME IN (N'NU_CON_FORMATADO', N'SK_CONTRATO')
        ORDER BY 1, 2
        """
    )
    out: list[tuple[str, str]] = []
    for sch, tbl in cur.fetchall():
        if is_supra_cgcont_simdnit_table(sch, tbl):
            continue
        out.append((sch, tbl))
    return out


def cache_simdnit_rows_by_contract(
    cm, nu: str, sk: int | None
) -> dict[str, dict[str, Any]]:
    cache: dict[str, dict[str, Any]] = {}
    for sch, tbl in anchorable_simdnit_tables(cm):
        qual = f"{sch}.{tbl}"
        row, msg = fetch_simdnit_row(cm, sch, tbl, nu, sk)
        if msg == "ok" and row:
            cache[qual] = row
    return cache


def full_norm_index_from_rows(
    rows_by_table: dict[str, dict[str, Any]],
    contract_nu: str,
) -> dict[str, list[tuple[str, str]]]:
    """Índice valor_normalizado -> [(tabela qualificada, coluna)]."""
    nu_n = norm_val(contract_nu.strip())
    # Evita centenas de hits com o mesmo NU em todas as tabelas (não é sugestão útil).
    skip_if_val_is_contract = frozenset({"nu_con_formatado", "contrato"})
    inv: dict[str, list[tuple[str, str]]] = {}
    for qual, d in rows_by_table.items():
        for col, val in d.items():
            nv = norm_val(val)
            if nv is None:
                continue
            if (
                nu_n is not None
                and nv == nu_n
                and col.lower() in skip_if_val_is_contract
            ):
                continue
            inv.setdefault(nv, []).append((qual, col))
    return inv


def cross_table_value_hints(
    orphans: list[str],
    sup_d: dict[str, Any],
    primary_qual: str,
    full_inv: dict[str, list[tuple[str, str]]],
    preview_max: int = 120,
) -> list[dict[str, Any]]:
    pq = primary_qual.strip().lower()
    out: list[dict[str, Any]] = []
    for col in orphans:
        nv = norm_val(sup_d.get(col))
        if nv is None:
            out.append(
                {
                    "coluna_supra": col,
                    "valor_normalizado_prev": None,
                    "ocorrencias_noutras_tabelas": 0,
                    "nota": "sem_valor_na_linha_supra",
                }
            )
            continue
        hits = [
            (t, c)
            for t, c in full_inv.get(nv, [])
            if t.lower() != pq
        ]
        preview = nv if len(nv) <= preview_max else nv[: preview_max - 3] + "..."
        rec: dict[str, Any] = {
            "coluna_supra": col,
            "valor_normalizado_prev": preview,
            "ocorrencias_noutras_tabelas": len(hits),
        }
        if len(hits) == 1:
            rec["sugestao_unica"] = {"tabela": hits[0][0], "coluna": hits[0][1]}
        elif len(hits) > 1:
            rec["candidatos"] = [
                {"tabela": t, "coluna": c} for t, c in hits[:50]
            ]
            if len(hits) > 50:
                rec["candidatos_truncados"] = True
        else:
            rec["sugestao_unica"] = None
        out.append(rec)
    return out


def match_trecho_supra_to_dados_segmento(
    cm, nu: str, sk: int | None, sup_trecho: Any
) -> dict[str, Any] | None:
    """
    TB_SIAC_CONTRATO.trecho = DS_TRECHO(linha 1) + \" - \" + ... + DS_TRECHO(linha N)
    em dbo.Dados_Segmento para o mesmo contrato, ordenado por SK_SEGMENTO.
    """
    if sup_trecho is None:
        return None
    sup_s = " ".join(str(sup_trecho).split()).strip()
    if not sup_s:
        return None
    cur = cm.cursor()
    cur.execute(
        """
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = N'dbo' AND TABLE_NAME = N'Dados_Segmento'
        """
    )
    cols = {r[0].lower(): r[0] for r in cur.fetchall()}
    needed = ("ds_trecho", "sk_segmento")
    if not all(k in cols for k in needed):
        return None
    if "nu_con_formatado" in cols:
        wc, wp = cols["nu_con_formatado"], nu
    elif sk is not None and "sk_contrato" in cols:
        wc, wp = cols["sk_contrato"], sk
    else:
        return None
    c_trecho = br(cols["ds_trecho"])
    c_seg = br(cols["sk_segmento"])
    c_w = br(wc)
    al = "seg"
    pred = f"{br(al)}.{br(wc)} = ?"
    wh, prm = sql_where_scoped_first_predicates(
        pred, (wp,), "dbo", "Dados_Segmento", al, cols
    )
    try:
        cur.execute(
            f"""
            SELECT {br(al)}.{c_trecho} FROM dbo.Dados_Segmento AS {br(al)}
            {wh}
            ORDER BY {br(al)}.{c_seg}
            """,
            prm,
        )
    except Exception:
        return None
    parts: list[str] = []
    for (cell,) in cur.fetchall():
        if cell is not None and str(cell).strip():
            parts.append(str(cell).strip())
    if not parts:
        return None
    cat = " - ".join(parts)
    if " ".join(cat.split()) != sup_s:
        return None
    return {
        "tabela_simdnit": "dbo.Dados_Segmento",
        "coluna_fonte": "DS_TRECHO",
        "ordenacao": "SK_SEGMENTO",
        "separador": " - ",
        "num_segmentos": len(parts),
    }


def apply_trecho_dados_segmento_hint(
    cm,
    nu: str,
    sk: int | None,
    sup_table: str,
    orphans: list[str],
    sup_d: dict[str, Any],
    hints: list[dict[str, Any]],
) -> None:
    if sup_table.upper() != "TB_SIAC_CONTRATO" or "trecho" not in orphans:
        return
    comp = match_trecho_supra_to_dados_segmento(cm, nu, sk, sup_d.get("trecho"))
    if not comp:
        return
    for h in hints:
        if h.get("coluna_supra") != "trecho":
            continue
        h["composicao_dados_segmento"] = comp
        h["nota"] = "concatenacao_ds_trecho_varias_linhas"
        h["ocorrencias_noutras_tabelas"] = 1
        h["sugestao_unica"] = {
            "tabela": comp["tabela_simdnit"],
            "coluna": comp["coluna_fonte"],
            "regra": (
                f"ORDER BY {comp['ordenacao']}; juntar com {comp['separador']!r}; "
                f"{comp['num_segmentos']} segmento(s)"
            ),
        }
        h.pop("candidatos", None)
        h.pop("candidatos_truncados", None)
        break


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nu", default="00 00799/2025")
    args = ap.parse_args()
    nu = args.nu.strip()
    load_env()

    mapping = json.loads(MAPPING_JSON.read_text(encoding="utf-8"))

    full_report: list[dict[str, Any]] = []

    with connect_endpoint(supra_local_endpoint()) as cs, connect_endpoint(
        simdnit_endpoint()
    ) as cm:
        ok_nu, err_scope = validate_nu_in_scoped_dados_contrato(cm, nu)
        if not ok_nu:
            raise SystemExit(err_scope)
        sk_hold = get_sk_contrato_scoped(cm, nu)
        cur = cs.cursor()
        cur.execute(
            "SELECT id_siac_contrato FROM dbo.TB_SIAC_CONTRATO WHERE contrato = ?",
            (nu,),
        )
        r_id = cur.fetchone()
        cid_hold = int(r_id[0]) if r_id and r_id[0] is not None else None

        sim_rows_cache = cache_simdnit_rows_by_contract(cm, nu, sk_hold)
        full_norm_index = full_norm_index_from_rows(sim_rows_cache, nu)

        for entry in mapping:
            sup_q = entry["supra_table"]
            sch_s, tbl_s = parse_q(sup_q)
            if not include_tb_siac_for_analysis(sch_s, tbl_s):
                continue
            sim_q = resolve_simdnit_table(entry)
            if not sim_q:
                continue
            sch_m, tbl_m = parse_q(sim_q)

            meta_hom = homonym_pairs(entry)
            sup_schema = table_columns(cs, sch_s, tbl_s)
            sim_schema = table_columns(cm, sch_m, tbl_m)
            sem_merged, sem_display = schema_semantic_pairs(sup_schema, sim_schema)
            merged: set[tuple[str, str]] = set(meta_hom) | sem_merged

            sup_d, msg_s = fetch_supra_anchor(cs, sch_s, tbl_s, nu, cid_hold)
            sim_d, msg_m = fetch_simdnit_row(cm, sch_m, tbl_m, nu, sk_hold)

            sup_norm = collect_for_match(sup_d)
            sim_norm = collect_for_match(sim_d)
            inferred = infer_value_pairs(sup_norm, sim_norm, merged)
            for a, b, _ in inferred:
                merged.add((a.lower(), b.lower()))

            paired_sup = {a for a, b in merged}
            all_sup_cols = [c["name"] for c in entry.get("supra_columns") or []]
            orphans = [c for c in all_sup_cols if c.lower() not in paired_sup]

            xhints = cross_table_value_hints(
                orphans, sup_d, sim_q, full_norm_index
            )
            apply_trecho_dados_segmento_hint(
                cm, nu, sk_hold, tbl_s, orphans, sup_d, xhints
            )

            full_report.append(
                {
                    "supra_table": sup_q,
                    "simdnit_table": sim_q,
                    "contrato": nu,
                    "sk_contrato_simdnit": sk_hold,
                    "fetch_supra": msg_s,
                    "fetch_simdnit": msg_m,
                    "pairs_homonym": [
                        {"supra": p["supra"], "simdnit": p["simdnit"]}
                        for p in entry.get("column_pairs_exact_name") or []
                    ],
                    "pairs_semantic": sem_display,
                    "pairs_inferidos_valor": [
                        {"supra": a, "simdnit": b, "motivo": r}
                        for a, b, r in inferred
                    ],
                    "colunas_supra_sem_par_depois_inferencia": orphans,
                    "cruzamento_valor_outras_tabelas_simdnit": xhints,
                }
            )

    safe_nu = re.sub(r"[^\w]+", "_", nu).strip("_")
    out_infer = ROOT / "reports" / f"inferencia_colunas_por_contrato_{safe_nu}.json"
    out_infer.write_text(json.dumps(full_report, ensure_ascii=False, indent=2), encoding="utf-8")

    sg = scope_sg_und_gestora()
    orphan_lines = [
        "# Colunas SUPRA sem par após: homónimo (JSON) + semânticos (SEMANTIC_SUPRA_TO_SIM) + inferência por valor na tabela mapeada (match único).",
        "# Sugestão extra: valor noutras tabelas ou regra conhecida (ex.: trecho = concat Dados_Segmento.DS_TRECHO por SK_SEGMENTO).",
        f"# Contrato: {nu}",
        f"# SIMDNIT: só linhas cujo contrato existe em Dados_Contrato com SG_UND_GESTORA = {sg!r}",
        "",
    ]
    for rec in full_report:
        o = rec.get("colunas_supra_sem_par_depois_inferencia") or []
        if not o:
            continue
        orphan_lines.append(f"## {rec['supra_table']}")
        orphan_lines.append(
            f"   SIMDNIT: {rec['simdnit_table']} | SUPRA: {rec['fetch_supra']} | SIMDNIT: {rec['fetch_simdnit']}"
        )
        for c in o:
            orphan_lines.append(f"   - {c}")
            hint = None
            for h in rec.get("cruzamento_valor_outras_tabelas_simdnit") or []:
                if h.get("coluna_supra") == c:
                    hint = h
                    break
            if not hint:
                continue
            if hint.get("nota") == "sem_valor_na_linha_supra":
                orphan_lines.append("     → sem valor na linha SUPRA para cruzar")
                continue
            co = hint.get("composicao_dados_segmento")
            if co:
                orphan_lines.append(
                    f"     → trecho = concatenação {co['tabela_simdnit']}.{co['coluna_fonte']} "
                    f"({co['num_segmentos']} linhas, ORDER BY {co['ordenacao']}, sep {co['separador']!r})"
                )
                continue
            su = hint.get("sugestao_unica")
            oc = hint.get("ocorrencias_noutras_tabelas", 0)
            if su and isinstance(su, dict) and su.get("regra"):
                orphan_lines.append(
                    f"     → sugestão: {su.get('tabela')}.{su.get('coluna')} — {su.get('regra')}"
                )
                continue
            if su:
                orphan_lines.append(
                    f"     → valor coincide (noutras tab.): {su['tabela']}.{su['coluna']}"
                )
            elif oc > 1:
                cand = hint.get("candidatos") or []
                parts = [f"{x['tabela']}.{x['coluna']}" for x in cand[:8]]
                tail = " …" if hint.get("candidatos_truncados") or oc > 8 else ""
                orphan_lines.append(
                    f"     → mesmo valor em {oc} locais (ambiguo): {', '.join(parts)}{tail}"
                )
            elif oc == 0:
                orphan_lines.append(
                    "     → valor não encontrado noutras tabelas SIMDNIT (âncora)"
                )
        orphan_lines.append("")

    (ROOT / "reports" / "tb_siac_colunas_sem_par.txt").write_text(
        "\n".join(orphan_lines), encoding="utf-8"
    )
    print(out_infer)
    print(ROOT / "reports" / "tb_siac_colunas_sem_par.txt")


if __name__ == "__main__":
    main()
