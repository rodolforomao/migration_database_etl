"""Carrega .env e define o modo local / homolog / produção."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError as e:
    raise SystemExit("Instale dependências: pip install -r requirements.txt") from e


class UpdateMode(str, Enum):
    LOCAL      = "local"
    HOMOLOG    = "homolog"
    PRODUCTION = "production"


@dataclass(frozen=True)
class SqlServerEndpoint:
    host: str
    port: str
    user: str
    password: str
    database: str
    label: str


def load_env() -> None:
    from supra_db_update._paths import runtime_root
    dotenv_path = os.environ.get("DOTENV_PATH")
    if dotenv_path:
        load_dotenv(dotenv_path)
    else:
        load_dotenv(runtime_root() / ".env")


def _strip_quotes(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def get_setting(*keys: str, default: str | None = None) -> str | None:
    for k in keys:
        v = os.environ.get(k)
        if v is not None and str(v).strip() != "":
            return _strip_quotes(str(v))
    return default


def _endpoint(prefix: str, label: str, required: bool = True) -> SqlServerEndpoint:
    """Constrói um endpoint lendo variáveis {prefix}_HOST, _PORT, _USER, _PASS, _DATABASE."""
    host = get_setting(f"{prefix}_HOST")
    if not host:
        if required:
            raise SystemExit(f"Defina {prefix}_HOST no .env")
        host = "localhost"
    return SqlServerEndpoint(
        host=host,
        port=get_setting(f"{prefix}_PORT", default="1433") or "1433",
        user=get_setting(f"{prefix}_USER") or "",
        password=get_setting(f"{prefix}_PASS") or "",
        database=get_setting(f"{prefix}_DATABASE", default="SUPRA") or "SUPRA",
        label=label,
    )


def _parse_mode(raw: str, var: str) -> UpdateMode:
    r = raw.lower()
    if r == "production":
        return UpdateMode.PRODUCTION
    if r == "homolog":
        return UpdateMode.HOMOLOG
    if r == "local":
        return UpdateMode.LOCAL
    raise SystemExit(f"{var} inválido: {raw!r} (use local, homolog ou production)")


def pick_supra_mode() -> UpdateMode:
    """Modo do destino SUPRA — lido de SUPRA_UPDATE_MODE."""
    return _parse_mode(
        get_setting("SUPRA_UPDATE_MODE", default="local") or "local",
        "SUPRA_UPDATE_MODE",
    )


def pick_simdnit_mode() -> UpdateMode:
    """Modo da origem SIMDNIT — lido de SIMDNIT_UPDATE_MODE (padrão = local)."""
    return _parse_mode(
        get_setting("SIMDNIT_UPDATE_MODE", default="local") or "local",
        "SIMDNIT_UPDATE_MODE",
    )


# mantido para compatibilidade com chamadas existentes
def pick_update_mode() -> UpdateMode:
    return pick_supra_mode()


# ── Endpoints SIMDNIT por modo ───────────────────────────────────────────────

def simdnit_for_mode(mode: UpdateMode) -> SqlServerEndpoint:
    if mode == UpdateMode.HOMOLOG:
        return _endpoint("SIMDNIT_HOM", "SIMDNIT_HOM")
    if mode == UpdateMode.PRODUCTION:
        return _endpoint("SIMDNIT_PROD", "SIMDNIT_PROD")
    return _endpoint("SIMDNIT_LOCAL", "SIMDNIT_LOCAL", required=False)


def simdnit_endpoint() -> SqlServerEndpoint:
    """Endpoint SIMDNIT conforme SIMDNIT_UPDATE_MODE."""
    return simdnit_for_mode(pick_simdnit_mode())


# ── Endpoints SUPRA por modo ─────────────────────────────────────────────────

def supra_targets_for_mode(mode: UpdateMode) -> list[SqlServerEndpoint]:
    """Destinos SUPRA conforme SUPRA_UPDATE_MODE."""
    if mode == UpdateMode.LOCAL:
        return [_endpoint("SUPRA_LOCAL", "SUPRA_LOCAL", required=False)]
    if mode == UpdateMode.HOMOLOG:
        return [_endpoint("SUPRA_HOM", "SUPRA_HOM")]
    return [_endpoint("SUPRA_PROD", "SUPRA_PROD")]


def simdnit_scope() -> str:
    """Retorna o valor de SG_UND_GESTORA para filtrar contratos no SIMDNIT."""
    return get_setting("SIMDNIT_SCOPE_SG_UND_GESTORA", default="CGCONT") or "CGCONT"
