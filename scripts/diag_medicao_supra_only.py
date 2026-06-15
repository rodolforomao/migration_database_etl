"""
Diagnóstico: medições presentes no SUPRA (TB_SIAC_MEDICAO_MAIOR) mas ausentes no SIMDNIT
(Dados_Medicao) para contratos do escopo CGCONT.

Uso:
    python scripts/diag_medicao_supra_only.py [--sg CGCONT] [--limit 500]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supra_db_update.config import (
    get_setting,
    load_env,
    pick_supra_mode,
    simdnit_endpoint,
    supra_targets_for_mode,
)
from supra_db_update.connection import connect_endpoint

_CHUNK = 900


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def _fetch_scope_contracts(cur, sg: str) -> list[str]:
    cur.execute(
        "SELECT [NU_CON_FORMATADO] FROM [dbo].[Dados_Contrato] "
        "WHERE [SG_UND_GESTORA] = %s",
        (sg,),
    )
    return [str(r[0]) for r in cur.fetchall() if r[0] is not None]


def _fetch_supra_keys(cur, contracts: list[str]) -> set[tuple]:
    """Retorna set de (contrato, str(nume_medicao)) do SUPRA, no escopo dado."""
    keys: set[tuple] = set()
    for chunk in _chunks(contracts, _CHUNK):
        ph = ",".join(["%s"] * len(chunk))
        cur.execute(
            f"SELECT [contrato], [nume_medicao] "
            f"FROM [dbo].[TB_SIAC_MEDICAO_MAIOR] "
            f"WHERE [contrato] IN ({ph})",
            chunk,
        )
        for row in cur.fetchall():
            keys.add((str(row[0]) if row[0] is not None else "", str(row[1]) if row[1] is not None else ""))
    return keys


def _fetch_simdnit_keys(cur, contracts: list[str]) -> set[tuple]:
    """Retorna set de (NU_CON_FORMATADO, str(NU_MEDICAO)) do SIMDNIT, no escopo dado."""
    keys: set[tuple] = set()
    for chunk in _chunks(contracts, _CHUNK):
        ph = ",".join(["%s"] * len(chunk))
        cur.execute(
            f"SELECT [NU_CON_FORMATADO], [NU_MEDICAO] "
            f"FROM [dbo].[Dados_Medicao] "
            f"WHERE [NU_CON_FORMATADO] IN ({ph})",
            chunk,
        )
        for row in cur.fetchall():
            keys.add((str(row[0]) if row[0] is not None else "", str(row[1]) if row[1] is not None else ""))
    return keys


def _fetch_supra_rows_for_missing(cur, missing: list[tuple]) -> list[dict]:
    """Busca detalhes do SUPRA para as medições ausentes no SIMDNIT."""
    if not missing:
        return []
    rows = []
    # busca em chunks de contrato+medicao via OR seria lento; usa temp set approach
    # agrupa por contrato para minimizar queries
    by_contract: dict[str, list[str]] = {}
    for contrato, medicao in missing:
        by_contract.setdefault(contrato, []).append(medicao)

    for contrato, medicoes in by_contract.items():
        ph = ",".join(["%s"] * len(medicoes))
        try:
            cur.execute(
                f"SELECT [contrato], [nume_medicao], [data_inicio_medicao], "
                f"[data_termino_medicao], [valor_medicao_acumulada], [percentual_medicao_acumulada] "
                f"FROM [dbo].[TB_SIAC_MEDICAO_MAIOR] "
                f"WHERE [contrato] = %s AND [nume_medicao] IN ({ph})",
                [contrato] + medicoes,
            )
            for row in cur.fetchall():
                rows.append({
                    "contrato":                    row[0],
                    "nume_medicao":                row[1],
                    "data_inicio_medicao":         row[2],
                    "data_termino_medicao":        row[3],
                    "valor_medicao_acumulada":     row[4],
                    "percentual_medicao_acumulada": row[5],
                })
        except Exception as exc:
            rows.append({"contrato": contrato, "erro": str(exc)})
    return rows


def _fmt(v) -> str:
    if v is None:
        return "NULL"
    return str(v)


def main() -> int:
    parser = argparse.ArgumentParser(description="Medições no SUPRA ausentes no SIMDNIT.")
    parser.add_argument("--sg",    default="CGCONT", help="Escopo SG_UND_GESTORA (padrão: CGCONT)")
    parser.add_argument("--limit", type=int, default=500, help="Máx linhas a detalhar (padrão: 500)")
    args = parser.parse_args()

    load_env()
    sg      = args.sg or get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
    sim_ep  = simdnit_endpoint()
    mode    = pick_supra_mode()
    targets = supra_targets_for_mode(mode)

    print(f"SIMDNIT : {sim_ep.host}:{sim_ep.port}  db={sim_ep.database}")
    print(f"SUPRA   : {targets[0].host}:{targets[0].port}  db={targets[0].database}")
    print(f"Escopo  : SG_UND_GESTORA = '{sg}'\n")

    with connect_endpoint(sim_ep) as sim_conn, connect_endpoint(targets[0]) as sup_conn:
        sim_cur = sim_conn.cursor()
        sup_cur = sup_conn.cursor()

        print("1/4  Buscando contratos do escopo no SIMDNIT...")
        contracts = _fetch_scope_contracts(sim_cur, sg)
        print(f"     {len(contracts):,} contratos no escopo '{sg}'\n")

        if not contracts:
            print("Nenhum contrato encontrado no escopo. Verifique o parâmetro --sg.")
            return 1

        print("2/4  Buscando chaves SUPRA (contrato, nume_medicao)...")
        supra_keys = _fetch_supra_keys(sup_cur, contracts)
        print(f"     {len(supra_keys):,} medições no SUPRA\n")

        print("3/4  Buscando chaves SIMDNIT (NU_CON_FORMATADO, NU_MEDICAO)...")
        simdnit_keys = _fetch_simdnit_keys(sim_cur, contracts)
        print(f"     {len(simdnit_keys):,} medições no SIMDNIT\n")

        # diferença: está no SUPRA mas não no SIMDNIT
        missing = sorted(supra_keys - simdnit_keys, key=lambda t: (t[0], float(t[1]) if t[1].replace('.','',1).isdigit() else t[1]))
        print(f"4/4  Medições no SUPRA sem par no SIMDNIT: {len(missing):,}\n")

        if not missing:
            print("Nenhuma divergência encontrada.")
            return 0

        # agrupa por contrato
        by_contract: dict[str, list[str]] = {}
        for contrato, medicao in missing:
            by_contract.setdefault(contrato, []).append(medicao)

        print(f"Contratos afetados: {len(by_contract)}")
        print("-" * 60)
        for contrato, medicoes in sorted(by_contract.items()):
            nums = ", ".join(medicoes[:30])
            suffix = f" ... (+{len(medicoes)-30} mais)" if len(medicoes) > 30 else ""
            print(f"  {contrato:<25}  {len(medicoes):>3} medição(ões): [{nums}{suffix}]")

        if len(missing) > args.limit:
            print(f"\n(limitando detalhes às primeiras {args.limit} de {len(missing)} linhas)")
            missing_detail = missing[:args.limit]
        else:
            missing_detail = missing

        print(f"\n{'─'*110}")
        print(f"{'CONTRATO':<25}  {'MEDICAO':>8}  {'INÍCIO':<12}  {'TÉRMINO':<12}  {'VALOR ACUMULADO':>18}  {'% ACUM':>8}")
        print(f"{'─'*110}")

        detail_rows = _fetch_supra_rows_for_missing(sup_cur, missing_detail)
        detail_rows.sort(key=lambda r: (str(r.get("contrato","") or ""), float(str(r.get("nume_medicao","0") or "0")) if str(r.get("nume_medicao","0") or "0").replace('.','',1).isdigit() else 0))

        for r in detail_rows:
            if "erro" in r:
                print(f"  {r.get('contrato','?'):<25}  ERRO: {r['erro']}")
                continue
            print(
                f"  {_fmt(r['contrato']):<25}  "
                f"{_fmt(r['nume_medicao']):>8}  "
                f"{_fmt(r['data_inicio_medicao']):<12}  "
                f"{_fmt(r['data_termino_medicao']):<12}  "
                f"{_fmt(r['valor_medicao_acumulada']):>18}  "
                f"{_fmt(r['percentual_medicao_acumulada']):>8}"
            )

        print(f"{'─'*110}")
        print(f"\nTotal: {len(missing):,} medição(ões) no SUPRA sem correspondente no SIMDNIT.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
# quick inline check
