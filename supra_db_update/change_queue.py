"""
Fila de alterações pendentes: SIMDNIT → SUPRA.

Fluxo:
  1. compare  → build_changeset() + save_changeset()  → pending_changes.json
  2. review   → load_changeset() + aceitar/rejeitar    → pending_changes.json (atualizado)
  3. apply    → load_changeset() + sync_table()        → banco atualizado
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from supra_db_update.differ import TableDiff

DEFAULT_PATH = Path("pending_changes.json")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ContractChange:
    id: str              # "C0001"
    table_id: str        # "T01" — tabela pai
    contract: str        # número do contrato
    action: str          # INSERT | DELETE | D/I | UPDATE
    simdnit_count: int
    supra_count: int
    accepted: bool | None = None  # None=pendente  True=aceito  False=rejeitado
    note: str = ""

    @property
    def delta(self) -> int:
        return self.simdnit_count - self.supra_count

    @property
    def status_label(self) -> str:
        if self.accepted is True:
            return "ACEITO"
        if self.accepted is False:
            return "REJEITADO"
        return "pendente"


@dataclass
class TableChange:
    id: str              # "T01"
    table_supra: str
    table_simdnit: str
    simdnit_total: int
    supra_total: int
    accepted: bool | None = None   # None=individual  True=todos  False=nenhum
    contracts: list[ContractChange] = field(default_factory=list)

    @property
    def delta(self) -> int:
        return self.simdnit_total - self.supra_total

    @property
    def effective_contracts(self) -> list[ContractChange]:
        """Contratos efectivamente aceitos (respeitando override de tabela)."""
        if self.accepted is True:
            return self.contracts
        if self.accepted is False:
            return []
        return [c for c in self.contracts if c.accepted is True]

    @property
    def effective_contract_numbers(self) -> list[str]:
        return [c.contract for c in self.effective_contracts]

    @property
    def n_accepted(self) -> int:
        if self.accepted is True:
            return len(self.contracts)
        if self.accepted is False:
            return 0
        return sum(1 for c in self.contracts if c.accepted is True)

    @property
    def n_rejected(self) -> int:
        if self.accepted is False:
            return len(self.contracts)
        if self.accepted is True:
            return 0
        return sum(1 for c in self.contracts if c.accepted is False)

    @property
    def n_pending(self) -> int:
        if self.accepted is not None:
            return 0
        return sum(1 for c in self.contracts if c.accepted is None)

    @property
    def status_label(self) -> str:
        if self.accepted is True:
            return f"ACEITO ({len(self.contracts)})"
        if self.accepted is False:
            return "REJEITADO"
        parts = []
        a, r, p = self.n_accepted, self.n_rejected, self.n_pending
        if a:
            parts.append(f"{a} aceito(s)")
        if r:
            parts.append(f"{r} rejeitado(s)")
        if p:
            parts.append(f"{p} pendente(s)")
        return " / ".join(parts) if parts else "vazio"


@dataclass
class ChangeSet:
    generated_at: str
    target_label: str
    sg_und_gestora: str
    status: str = "pending"   # "pending" | "applied"
    tables: list[TableChange] = field(default_factory=list)

    # ── lookups ─────────────────────────────────────────────────────────────

    def get_table(self, tid: str) -> TableChange | None:
        for t in self.tables:
            if t.id.upper() == tid.upper():
                return t
        return None

    def get_contract(self, cid: str) -> ContractChange | None:
        for t in self.tables:
            for c in t.contracts:
                if c.id.upper() == cid.upper():
                    return c
        return None

    def get_by_id(self, id_: str) -> TableChange | ContractChange | None:
        return self.get_table(id_) or self.get_contract(id_)

    # ── totais ──────────────────────────────────────────────────────────────

    @property
    def total_contracts(self) -> int:
        return sum(len(t.contracts) for t in self.tables)

    @property
    def n_accepted(self) -> int:
        return sum(t.n_accepted for t in self.tables)

    @property
    def n_rejected(self) -> int:
        return sum(t.n_rejected for t in self.tables)

    @property
    def n_pending(self) -> int:
        return sum(t.n_pending for t in self.tables)

    @property
    def ready_to_apply(self) -> bool:
        return self.n_accepted > 0 and self.status == "pending"


# ---------------------------------------------------------------------------
# Serialização
# ---------------------------------------------------------------------------

def _contract_to_dict(c: ContractChange) -> dict:
    return {
        "id": c.id,
        "table_id": c.table_id,
        "contract": c.contract,
        "action": c.action,
        "simdnit_count": c.simdnit_count,
        "supra_count": c.supra_count,
        "accepted": c.accepted,
        "note": c.note,
    }


def _table_to_dict(t: TableChange) -> dict:
    return {
        "id": t.id,
        "table_supra": t.table_supra,
        "table_simdnit": t.table_simdnit,
        "simdnit_total": t.simdnit_total,
        "supra_total": t.supra_total,
        "accepted": t.accepted,
        "contracts": [_contract_to_dict(c) for c in t.contracts],
    }


def save_changeset(cs: ChangeSet, path: Path = DEFAULT_PATH) -> None:
    data = {
        "_doc": (
            "Arquivo de alterações pendentes. "
            "Edite 'accepted': true/false nos contratos ou tabelas para aceitar/rejeitar. "
            "Execute 'python -m supra_db_update apply' para aplicar os aceitos."
        ),
        "generated_at": cs.generated_at,
        "target_label": cs.target_label,
        "sg_und_gestora": cs.sg_und_gestora,
        "status": cs.status,
        "tables": [_table_to_dict(t) for t in cs.tables],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_changeset(path: Path = DEFAULT_PATH) -> ChangeSet:
    raw = json.loads(path.read_text(encoding="utf-8"))
    tables: list[TableChange] = []
    for t in raw.get("tables", []):
        contracts = [
            ContractChange(
                id=c["id"],
                table_id=c.get("table_id", t["id"]),
                contract=c["contract"],
                action=c["action"],
                simdnit_count=c["simdnit_count"],
                supra_count=c["supra_count"],
                accepted=c.get("accepted"),
                note=c.get("note", ""),
            )
            for c in t.get("contracts", [])
        ]
        tables.append(
            TableChange(
                id=t["id"],
                table_supra=t["table_supra"],
                table_simdnit=t["table_simdnit"],
                simdnit_total=t["simdnit_total"],
                supra_total=t["supra_total"],
                accepted=t.get("accepted"),
                contracts=contracts,
            )
        )
    return ChangeSet(
        generated_at=raw["generated_at"],
        target_label=raw["target_label"],
        sg_und_gestora=raw["sg_und_gestora"],
        status=raw.get("status", "pending"),
        tables=tables,
    )


# ---------------------------------------------------------------------------
# build_changeset — constrói a partir dos TableDiffs
# ---------------------------------------------------------------------------

def build_changeset(
    diffs: list[TableDiff],
    target_label: str,
    sg: str,
) -> ChangeSet:
    tables: list[TableChange] = []
    contract_seq = 0

    for t_idx, diff in enumerate(diffs):
        active = diff.active_changed
        if not active:
            continue

        t_id = f"T{t_idx + 1:02d}"
        contracts: list[ContractChange] = []

        for cd in active:
            contract_seq += 1
            contracts.append(
                ContractChange(
                    id=f"C{contract_seq:04d}",
                    table_id=t_id,
                    contract=cd.contract,
                    action=cd.action,
                    simdnit_count=cd.simdnit_count,
                    supra_count=cd.supra_count,
                )
            )

        tables.append(
            TableChange(
                id=t_id,
                table_supra=diff.pair.supra_table,
                table_simdnit=diff.pair.simdnit_table,
                simdnit_total=diff.simdnit_total,
                supra_total=diff.supra_total,
                contracts=contracts,
            )
        )

    return ChangeSet(
        generated_at=datetime.now().isoformat(timespec="seconds"),
        target_label=target_label,
        sg_und_gestora=sg,
        tables=tables,
    )
