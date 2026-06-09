"""Retorna info de conexão (label, host, database) para SIMDNIT e SUPRA como JSON."""
import json, sys
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))

from supra_db_update.config import (
    load_env, simdnit_endpoint, supra_targets_for_mode, pick_supra_mode,
)

load_env()
try:
    src = simdnit_endpoint()
    tgt = supra_targets_for_mode(pick_supra_mode())[0]
    print(json.dumps({
        "simdnit": {"label": src.label, "host": src.host, "port": src.port, "database": src.database},
        "supra":   {"label": tgt.label, "host": tgt.host, "port": tgt.port, "database": tgt.database},
    }))
except Exception as e:
    print(json.dumps({"erro": str(e)}))
