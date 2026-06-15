#!/usr/bin/env bash
# Gera executável Linux x86_64 portável para Debian 8+ (kernel 3.16 / glibc 2.19)
#
# Estratégia:
#   1. python:3.12-slim  — Python com shared lib (requerido pelo PyInstaller)
#   2. PyInstaller       — empacota Python 3.12 + pymssql (FreeTDS embutido) num único binário
#   3. staticx           — substitui todas as .so dinâmicas (incluindo libc) por
#                          versões estáticas → binário independente de glibc e sem ODBC no server
#
# Pré-requisito: Docker rodando nesta máquina.
#
# Uso:
#   ./build_dist.sh
#
# Resultado:
#   dist/supra_db_update   — binário Linux x86_64 estático, roda sem Python instalado
#                            (ainda requer os drivers MSSQL ODBC no servidor destino)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
IMAGE="python:3.12-slim"

echo "==> Imagem de build: $IMAGE"
docker pull --quiet "$IMAGE"

PIP_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/pip"
mkdir -p "$PIP_CACHE"

echo "==> Compilando dentro do container..."
docker run --rm \
  -v "$SCRIPT_DIR:/project" \
  -v "$PIP_CACHE:/root/.cache/pip" \
  "$IMAGE" \
  bash -c "
    set -euo pipefail

    echo '--- [1/5] Sistema: dependências de compilação ---'
    apt-get update -qq
    apt-get install -y --no-install-recommends \
      freetds-dev patchelf binutils > /dev/null
    echo 'OK'

    echo '--- [2/5] Dependências Python ---'
    pip install --quiet --timeout 120 --retries 5 \
      pymssql python-dotenv pyinstaller staticx

    echo '--- [3/5] Expondo o pacote do projeto ---'
    export PYTHONPATH=/project

    echo '--- [4/5] PyInstaller ---'
    pyinstaller \
      --onefile \
      --name supra_db_update \
      --hidden-import supra_db_update._paths \
      --hidden-import pymssql \
      --hidden-import dotenv \
      --distpath /project/dist \
      --workpath /tmp/pyinstaller_build \
      --specpath /tmp \
      --log-level WARN \
      /project/supra_db_update/__main__.py

    echo '--- [5/5] staticx — tornando o binário independente de glibc ---'
    staticx /project/dist/supra_db_update /project/dist/supra_db_update_static
    mv /project/dist/supra_db_update_static /project/dist/supra_db_update
    chmod 755 /project/dist/supra_db_update
    echo 'OK'

    echo '--- glibc requerida (deve ser vazio ou apenas GLIBC_2.0) ---'
    objdump -p /project/dist/supra_db_update \
      | grep -o 'GLIBC_[0-9.]*' \
      | sort -t_ -k2 -V | uniq || echo '(nenhuma dependência glibc dinâmica)'
  "

echo ""
echo "====================================================="
echo "Binário: dist/supra_db_update"
echo "Tamanho: $(du -sh "$SCRIPT_DIR/dist/supra_db_update" | cut -f1)"
echo "====================================================="
echo ""
echo "Para instalar no servidor:"
echo "  ssh admin.rodolfo@DNIT-SIGACONT 'mkdir -p ~/supra_db_update'"
echo "  scp dist/supra_db_update .env column_mapping.json import_rules.json admin.rodolfo@DNIT-SIGACONT:~/supra_db_update/"
echo "  ssh admin.rodolfo@DNIT-SIGACONT 'chmod +x ~/supra_db_update/supra_db_update'"
echo ""
echo "Para executar no servidor:"
echo "  cd ~/supra_db_update && ./supra_db_update --help"
