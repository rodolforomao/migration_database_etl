#!/usr/bin/env bash
# Gera o executável e instala na pasta de produção local.
#
# Uso:
#   ./build_and_deploy_local.sh
#
# Destino:
#   /home/black/enviroment/code/DNIT/production/supra/application/supra_db_update/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="/home/black/enviroment/code/DNIT/production/supra/application/supra_db_update"

echo "==> [1/2] Build..."
bash "$SCRIPT_DIR/build_dist.sh"

echo ""
echo "==> [2/2] Deploy → $DEST"
mkdir -p "$DEST"

# O Docker cria o binário como root — usa sudo para sobrescrever e corrige a propriedade.
sudo cp -v "$SCRIPT_DIR/dist/supra_db_update" "$DEST/supra_db_update"
sudo chown "$USER:$USER" "$DEST/supra_db_update"
chmod 755 "$DEST/supra_db_update"

for f in column_mapping.json import_rules.json .env; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        cp -v "$SCRIPT_DIR/$f" "$DEST/$f"
    else
        echo "  AVISO: $f não encontrado em $SCRIPT_DIR — ignorado"
    fi
done

echo ""
echo "====================================================="
echo "Deploy concluído: $DEST"
ls -lh "$DEST"
echo "====================================================="
