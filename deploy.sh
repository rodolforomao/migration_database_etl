#!/usr/bin/env bash
# deploy.sh — Compila o binário e envia para homolog ou produção via SSH.
#
# Uso:
#   ./deploy.sh hom [--build]    # envia para homolog (lê .env_hom)
#   ./deploy.sh prod [--build]   # envia para produção (lê .env_prod)
#
# Flags:
#   --build   Executa build_dist.sh antes de enviar (requer Docker)
#
# Arquivos enviados:
#   dist/supra_db_update   — binário compilado
#   column_mapping.json    — mapeamento de colunas
#   import_rules.json      — regras de validação
#   .env_hom / .env_prod   — credenciais → salvo como .env no servidor
#
# Arquivos preservados no servidor (não sobrescritos):
#   pending_changes.json, import_history.json, jobs/

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── 1. Argumentos ─────────────────────────────────────────────────────────────

AMBIENTE=""
DO_BUILD=false

for arg in "$@"; do
  case "$arg" in
    hom|prod) AMBIENTE="$arg" ;;
    --build)  DO_BUILD=true ;;
    *)
      echo "Uso: $0 [hom|prod] [--build]"
      exit 1
      ;;
  esac
done

if [[ -z "$AMBIENTE" ]]; then
  echo "Erro: informe o ambiente — hom ou prod."
  echo "Uso: $0 [hom|prod] [--build]"
  exit 1
fi

ENV_FILE="$SCRIPT_DIR/.env_${AMBIENTE}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Erro: arquivo '$ENV_FILE' não encontrado."
  echo "Crie-o a partir de .env.example e preencha as credenciais."
  exit 1
fi

# ── 2. Carrega variáveis DEPLOY_* do env file ─────────────────────────────────

while IFS= read -r line; do
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${line// }" ]] && continue
  if [[ "$line" =~ ^DEPLOY_ ]]; then
    # export seguro: mantém caracteres especiais nas aspas
    key="${line%%=*}"
    val="${line#*=}"
    export "$key"="$val"
  fi
done < "$ENV_FILE"

SSH_USER="${DEPLOY_SSH_USER:-}"
SSH_HOST="${DEPLOY_SSH_HOST:-}"
SSH_PORT="${DEPLOY_SSH_PORT:-22}"
SSH_DEST="${DEPLOY_SSH_DEST:-}"
SSH_PASS="${DEPLOY_SSH_PASS:-}"
APP_USER="${DEPLOY_APP_USER:-www-data}"

if [[ -z "$SSH_USER" || -z "$SSH_HOST" || -z "$SSH_DEST" ]]; then
  echo "Erro: DEPLOY_SSH_USER, DEPLOY_SSH_HOST e DEPLOY_SSH_DEST devem estar preenchidos em '$ENV_FILE'."
  exit 1
fi

if [[ -z "$SSH_PASS" ]]; then
  echo "Erro: DEPLOY_SSH_PASS não definido em '$ENV_FILE'."
  exit 1
fi

# Verifica sshpass
if ! command -v sshpass &>/dev/null; then
  echo "Erro: sshpass não encontrado. Instale com:"
  echo "  sudo apt-get install sshpass"
  exit 1
fi

# Prefixo sshpass para todos os comandos SSH/SCP
SSHPASS_CMD=(sshpass -p "$SSH_PASS")

# Opções comuns SSH (sem -p, pois sshpass já lida com a senha)
SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -p "$SSH_PORT")
# scp usa -P (maiúsculo) para porta
SCP_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -P "$SSH_PORT")

TARGET="${SSH_USER}@${SSH_HOST}"

echo ""
echo "====================================================="
echo "  Ambiente : $AMBIENTE"
echo "  Destino  : ${TARGET}:${SSH_DEST}"
echo "====================================================="
echo ""

# ── 3. Build (opcional) ───────────────────────────────────────────────────────

if [[ "$DO_BUILD" == true ]]; then
  echo "==> Compilando binário..."
  "$SCRIPT_DIR/build_dist.sh"
  echo ""
fi

# ── 4. Verifica binário ───────────────────────────────────────────────────────

BINARY="$SCRIPT_DIR/dist/supra_db_update"

if [[ ! -f "$BINARY" ]]; then
  echo "Erro: binário não encontrado em dist/supra_db_update."
  echo "Execute com --build ou rode ./build_dist.sh manualmente."
  exit 1
fi

echo "==> Binário: $(du -sh "$BINARY" | cut -f1)  ($BINARY)"
echo ""

# ── 5. Cria staging no servidor ───────────────────────────────────────────────

STAGING="/tmp/supra_deploy_$(date +%s)"

echo "==> Criando staging no servidor: $STAGING ..."
"${SSHPASS_CMD[@]}" ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p '$STAGING'"

# ── 6. Envia arquivos (scp) ───────────────────────────────────────────────────

echo "==> Enviando arquivos..."
"${SSHPASS_CMD[@]}" scp "${SCP_OPTS[@]}" \
  "$BINARY" \
  "$SCRIPT_DIR/column_mapping.json" \
  "$SCRIPT_DIR/import_rules.json" \
  "$TARGET:$STAGING/"

# Envia o arquivo de ambiente como .env
"${SSHPASS_CMD[@]}" scp "${SCP_OPTS[@]}" \
  "$ENV_FILE" \
  "$TARGET:$STAGING/.env"

# ── 7. Instala no destino final (sudo via sshpass -e) ─────────────────────────

echo ""
echo "==> Instalando em $SSH_DEST ..."

# Escapa a senha para embedding seguro no heredoc (evita expansão de $VAR no remoto)
ESCAPED_PASS=$(printf '%q' "$SSH_PASS")

# Passa a senha também para o sudo remoto via stdin
"${SSHPASS_CMD[@]}" ssh "${SSH_OPTS[@]}" "$TARGET" bash -s <<REMOTE
set -euo pipefail

STAGING="$STAGING"
DEST="$SSH_DEST"
PASS=$ESCAPED_PASS
APP_USER="$APP_USER"

_sudo() { printf '%s\n' "\$PASS" | sudo -S "\$@" 2>/dev/null; }

echo "--- Criando diretório destino ---"
_sudo mkdir -p "\$DEST"
_sudo mkdir -p "\$DEST/jobs"

echo "--- Copiando arquivos ---"
_sudo cp "\$STAGING/supra_db_update"      "\$DEST/supra_db_update"
_sudo cp "\$STAGING/column_mapping.json"  "\$DEST/column_mapping.json"
_sudo cp "\$STAGING/import_rules.json"    "\$DEST/import_rules.json"
_sudo cp "\$STAGING/.env"                 "\$DEST/.env"

# Cria arquivos de runtime se não existirem (Apache precisa escrever neles)
_sudo touch "\$DEST/pending_changes.json"
_sudo touch "\$DEST/import_history.json"

echo "--- Ajustando dono: \$APP_USER ---"
_sudo chown -R "\$APP_USER:\$APP_USER" "\$DEST"

echo "--- Ajustando permissões ---"
# Diretório raiz: Apache lê e entra
_sudo chmod 750 "\$DEST"
# Binário: Apache executa (PHP shell_exec)
_sudo chmod 750 "\$DEST/supra_db_update"
# .env: apenas dono lê (senhas)
_sudo chmod 600 "\$DEST/.env"
# Configs: dono lê/escreve, grupo lê
_sudo chmod 640 "\$DEST/column_mapping.json"
_sudo chmod 640 "\$DEST/import_rules.json"
# Runtime (Apache escreve):
_sudo chmod 660 "\$DEST/pending_changes.json"
_sudo chmod 660 "\$DEST/import_history.json"
# jobs/: Apache cria arquivos de saída
_sudo chmod 770 "\$DEST/jobs"
# Escrita pública nos diretórios (necessário para Apache/PHP sem ser dono)
_sudo chmod o+w "\$DEST"
_sudo chmod o+w "\$DEST/jobs"

echo "--- Permissões finais ---"
_sudo ls -la "\$DEST/"

echo "--- Limpando staging ---"
rm -rf "\$STAGING"

echo ""
echo "Instalação concluída em \$DEST  (dono: \$APP_USER)"
REMOTE

echo ""
echo "====================================================="
echo "  Deploy $AMBIENTE concluído!"
echo ""
echo "  Para executar no servidor:"
echo "    ssh ${TARGET}"
echo "    cd $SSH_DEST"
echo "    ./supra_db_update test-connections"
echo "====================================================="
echo ""
