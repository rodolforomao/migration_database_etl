#!/usr/bin/env bash
# Referência rápida de comandos — SUPRA_DB_UPDATE
# Diretório: /home/black/enviroment/code/DNIT/SUPRA_DB_UPDATE

# ── MODOS (SUPRA_UPDATE_MODE no .env) ───────────────────────────────────────
#
#   local      → Docker local   (SIMDNIT_LOCAL_* + SUPRA_LOCAL_*)
#   homolog    → homologação    (SIMDNIT_HOM_*   + SUPRA_HOM_*)
#   production → produção       (SIMDNIT_PROD_*  + SUPRA_PROD_*)


# ── CONECTIVIDADE ────────────────────────────────────────────────────────────

python -m supra_db_update test-connections


# ── VALIDAÇÃO PRÉ-MIGRAÇÃO ───────────────────────────────────────────────────

python -m supra_db_update validate dbo.Dados_Medicao


# ── COMPARE ─────────────────────────────────────────────────────────────────

python -m supra_db_update compare                            # diff rápido (contagem)
python -m supra_db_update compare --detail                   # lista contratos a inserir/repor/apagar
python -m supra_db_update compare --deep                     # checksum por linha (detecta mudança de valor)
python -m supra_db_update compare dbo.TB_SIAC_CONTRATO       # filtra uma tabela específica


# ── WORKFLOW RECOMENDADO: compare → review → apply ───────────────────────────

python -m supra_db_update compare --detail                   # 1. gera pending_changes.json
python -m supra_db_update review                             # 2. aceitar/rejeitar interativamente
python -m supra_db_update apply                              # 3. aplica apenas os aceitos
python -m supra_db_update apply --batch-size 1000            # 3. com lote maior


# ── COMANDOS DENTRO DO REVIEW ────────────────────────────────────────────────
#
#   aceitar T01           aceita todos os contratos da tabela T01
#   aceitar C0001,C0002   aceita contratos específicos por ID
#   aceitar all           aceita tudo
#   rejeitar T01          rejeita tabela inteira
#   rejeitar C0001        rejeita contrato específico
#   rejeitar all          rejeita tudo
#   ver T01               lista contratos e status da tabela T01
#   status                resumo aceitos/rejeitados/pendentes
#   aplicar               salva + executa apply direto (atalho)
#   sair                  salva e sai sem aplicar


# ── NAVEGAR CONTRATO A CONTRATO (diff de linhas + aceitar/rejeitar) ──────────
#
#   Mostra para cada contrato pendente:
#     [N/TOTAL] ID  —  número_contrato  (ação: SIM=X  SUPRA=Y  Δ=±Z)
#     Tabela : dbo.TB_SIAC_...
#     [+] linhas que entrarão no SUPRA (só no SIMDNIT)
#     [-] linhas que sairão do SUPRA   (só no SUPRA)
#     ⚠  aviso quando o mapeamento de colunas é insuficiente para diff preciso
#
#   Ações: INSERT=novo  DELETE=removido  D/I=contagens divergem  UPDATE=valor alterado

python -m supra_db_update navigate                               # navega todos os pendentes
python -m supra_db_update navigate --table T01                   # filtra por tabela (ID)
python -m supra_db_update navigate --table TB_SIAC_EMPENHO               # filtra por parte do nome
python -m supra_db_update navigate --contract C0006              # vai direto ao contrato C0006
python -m supra_db_update navigate --contract C0006 C0010        # múltiplos contratos
python -m supra_db_update navigate --contract C0006 --limit 50   # até 50 linhas por seção [+]/[-]

#   Dentro do navigate:
#     a / aceitar    aceita e avança (será sincronizado no apply)
#     r / rejeitar   rejeita e avança (não será tocado)
#     p / próximo    pula sem decidir (mantém pendente)
#     s / sair       salva progresso e encerra
#
#   Ao sair mostra resumo: N aceito(s)  N rejeitado(s)  N pendente(s)
#   Execute 'navigate' novamente para continuar os pendentes.


# ── INSPECIONAR UM CONTRATO ──────────────────────────────────────────────────

python -m supra_db_update inspect "00 00799/2025"                        # contagens em todas as tabelas
python -m supra_db_update inspect "00 00799/2025" --table dbo.TB_SIAC_CONTRATO
python -m supra_db_update inspect "00 00799/2025" --rows                 # mostra linhas reais
python -m supra_db_update inspect "00 00799/2025" --rows --limit 5


# ── SYNC DIRETO (sem review) ─────────────────────────────────────────────────

python -m supra_db_update sync                               # interativo: compara → escolhe → executa
python -m supra_db_update sync dbo.TB_SIAC_CONTRATO          # tabela específica
python -m supra_db_update sync --dry-run                     # simulação segura (não altera nada)
python -m supra_db_update sync --force --yes                 # todos os contratos CGCONT sem prompt
python -m supra_db_update sync --force --yes --batch-size 5000
python -m supra_db_update sync --deep                        # usa checksum


# ── SCRIPTS DE ANÁLISE (requerem conexão com os bancos) ─────────────────────

python scripts/build_tb_siac_mapping_from_db.py              # reconstrói reports/tb_siac_mapping_final.json
python scripts/infer_column_pairs_from_contract_row.py --nu "00 00799/2025"
python scripts/analyze_supra_simdnit_column_gaps.py          # gera relatórios de lacunas
python scripts/generate_column_mapping.py                    # atualiza column_mapping.json
