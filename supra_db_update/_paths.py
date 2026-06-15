"""Resolução de caminhos compatível com execução normal e PyInstaller --onefile."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def bundle_root() -> Path:
    """Raiz dos arquivos de dados empacotados (column_mapping.json).

    Dentro de um executável PyInstaller aponta para sys._MEIPASS (pasta temp
    onde os dados são extraídos). Em execução normal aponta para a raiz do projeto.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def _linux_ppid(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def _is_staticx_bundle_path(path: Path) -> bool:
    parts = path.parts
    return len(parts) >= 3 and parts[1] == "tmp" and parts[2].startswith("staticx-")


def _frozen_executable_dir() -> Path:
    """Diretório onde o binário empacotado está no disco.

    Com staticx, sys.executable e /proc/self/exe apontam para /tmp/staticx-*.
    O bootloader original fica na cadeia de processos pai ou em STATICX_PROG_PATH.
    """
    prog = os.environ.get("STATICX_PROG_PATH")
    if prog:
        return Path(prog).resolve().parent

    pid = os.getpid()
    seen: set[int] = set()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            exe = Path(os.readlink(f"/proc/{pid}/exe")).resolve()
        except OSError:
            pid = _linux_ppid(pid)
            continue
        if not _is_staticx_bundle_path(exe):
            return exe.parent
        pid = _linux_ppid(pid)

    argv0 = sys.argv[0] if sys.argv else ""
    if argv0 and os.path.isabs(argv0):
        return Path(argv0).resolve().parent

    return Path(sys.executable).resolve().parent


def runtime_root() -> Path:
    """Raiz dos arquivos de configuração do usuário (.env, *.json, etc.).

    No binário empacotado: mesmo diretório do executável.
    Em execução normal: raiz do projeto.
    """
    if getattr(sys, "frozen", False):
        return _frozen_executable_dir()
    return Path(__file__).resolve().parent.parent
