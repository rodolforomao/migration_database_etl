"""Resolução de caminhos compatível com execução normal e PyInstaller --onefile."""

from __future__ import annotations

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


def runtime_root() -> Path:
    """Raiz dos arquivos de configuração do usuário (.env).

    Dentro de um executável aponta para o diretório onde o binário está.
    Em execução normal aponta para a raiz do projeto.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent
