"""
Executa o script de reflexão SIAC → SUPRAVISÃO e devolve os result-sets como JSON.

Uso:
    python scripts/web_supravisao.py [--contrato-obra X] [--contrato-supervisora Y] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal

sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))

from supra_db_update.config import load_env, supra_targets_for_mode, pick_supra_mode
from supra_db_update.connection import connect_endpoint


def _serialize(v):
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace")
    return v


SQL = r"""
USE [SUPRA];

SET NOCOUNT ON;
SET XACT_ABORT ON;

BEGIN TRY
    BEGIN TRAN;

    DECLARE @ContratoObraFiltro varchar(50) = ?;
    DECLARE @ContratoSupervisoraFiltro varchar(50) = ?;
    DECLARE @IdUsuario int = NULL;

    IF OBJECT_ID('tempdb..#base') IS NOT NULL DROP TABLE #base;
    IF OBJECT_ID('tempdb..#base_u') IS NOT NULL DROP TABLE #base_u;
    IF OBJECT_ID('tempdb..#map_co') IS NOT NULL DROP TABLE #map_co;
    IF OBJECT_ID('tempdb..#co_inseridos') IS NOT NULL DROP TABLE #co_inseridos;
    IF OBJECT_ID('tempdb..#pendencias') IS NOT NULL DROP TABLE #pendencias;
    IF OBJECT_ID('tempdb..#log_exec') IS NOT NULL DROP TABLE #log_exec;
    IF OBJECT_ID('tempdb..#historico_hit') IS NOT NULL DROP TABLE #historico_hit;

    CREATE TABLE #log_exec
    (
        id_log          int           IDENTITY(1, 1) NOT NULL,
        tabela          varchar(80)   NOT NULL,
        acao            varchar(10)   NOT NULL,
        contrato_obra   varchar(50)   NULL,
        id_registro     int           NULL,
        detalhe         nvarchar(500) NULL
    );

    CREATE TABLE #base
    (
        contrato_obra           varchar(50)   NOT NULL,
        contrato_supervisao     varchar(50)   NULL,
        nome_supervisora        varchar(255)  NULL,
        id_supervisora          int           NULL,
        uf                      varchar(10)   NULL,
        construtora             varchar(255)  NULL,
        br                      varchar(20)   NOT NULL,
        br_config               nvarchar(500) NOT NULL,
        situacao_contrato       varchar(100)  NULL,
        situacao_supervisao     varchar(100)  NULL,
        num_processo            varchar(80)   NULL,
        unidade_gestora         varchar(120)  NULL,
        tipo                    varchar(120)  NULL,
        programa                varchar(255)  NULL,
        dt_inicio               date          NULL,
        dt_termino              date          NULL,
        ext_obra                decimal(18,2) NULL,
        vr_medicao_pi_mais_r    decimal(18,2) NULL,
        valor_contratado        decimal(18,2) NULL,
        valor_aditivado         decimal(18,2) NULL,
        valor_reajuste          decimal(18,2) NULL,
        total                   decimal(18,2) NULL,
        empenhado               decimal(18,2) NULL,
        supervisora_fmt         varchar(350)  NULL
    );

    CREATE TABLE #pendencias
    (
        contrato_obra       varchar(50)  NOT NULL,
        contrato_supervisao varchar(50)  NULL,
        nome_supervisora    varchar(255) NULL,
        motivo              nvarchar(500) NOT NULL
    );

    CREATE TABLE #historico_hit
    (
        contrato_obra   varchar(50)  NOT NULL,
        grupo           varchar(30)  NOT NULL,
        nome_tabela     sysname      NOT NULL
    );

    ;WITH siac_ranked AS
    (
        SELECT sc.*,
               ROW_NUMBER() OVER (PARTITION BY LTRIM(RTRIM(sc.contrato)) ORDER BY sc.id_siac_contrato DESC) AS rn
        FROM dbo.TB_SIAC_CONTRATO sc
        WHERE NULLIF(LTRIM(RTRIM(sc.contrato)), '') IS NOT NULL
    ),
    siac_sup_ranked AS
    (
        SELECT
            LTRIM(RTRIM(cs.contrato)) AS contrato_obra,
            LTRIM(RTRIM(cs.contrato_supervisora)) AS contrato_supervisao,
            NULLIF(LTRIM(RTRIM(cs.supervisora)), '') AS nome_supervisora,
            NULLIF(LTRIM(RTRIM(cs.situacao_contrato)), '') AS situacao_supervisao,
            ROW_NUMBER() OVER (PARTITION BY LTRIM(RTRIM(cs.contrato)) ORDER BY LTRIM(RTRIM(cs.contrato_supervisora)) DESC) AS rn_vinc
        FROM dbo.TB_SIAC_CONTRATO_SUPERVISORA cs
        WHERE NULLIF(LTRIM(RTRIM(cs.contrato)), '') IS NOT NULL
          AND NULLIF(LTRIM(RTRIM(cs.contrato_supervisora)), '') IS NOT NULL
          AND LTRIM(RTRIM(cs.contrato_supervisora)) NOT IN (N'-1', N'')
          AND (@ContratoObraFiltro IS NULL OR LTRIM(RTRIM(cs.contrato)) = LTRIM(RTRIM(@ContratoObraFiltro)))
          AND (@ContratoSupervisoraFiltro IS NULL OR LTRIM(RTRIM(cs.contrato_supervisora)) = LTRIM(RTRIM(@ContratoSupervisoraFiltro)))
    ),
    vinculos AS (SELECT contrato_obra, contrato_supervisao, nome_supervisora, situacao_supervisao FROM siac_sup_ranked WHERE rn_vinc = 1),
    vinc AS (SELECT v.contrato_obra, v.contrato_supervisao, v.nome_supervisora, v.situacao_supervisao FROM vinculos v),
    siac AS
    (
        SELECT
            LTRIM(RTRIM(sc.contrato)) AS contrato,
            LTRIM(RTRIM(sc.uf_unidade_local)) AS uf,
            LTRIM(RTRIM(sc.empresa)) AS construtora,
            LTRIM(RTRIM(sc.situacao_contrato)) AS situacao_contrato,
            LTRIM(RTRIM(sc.num_processo)) AS num_processo,
            LTRIM(RTRIM(sc.unidade_gestora)) AS unidade_gestora,
            LTRIM(RTRIM(sc.tipo_contrato)) AS tipo,
            LTRIM(RTRIM(sc.programa)) AS programa,
            sc.trecho, sc.objeto_contratacao,
            COALESCE(TRY_CONVERT(date, sc.dt_inicio), CAST(GETDATE() AS date)) AS dt_inicio,
            COALESCE(TRY_CONVERT(date, sc.dt_termino_atualizada), TRY_CONVERT(date, sc.dt_termino_prevista), TRY_CONVERT(date, sc.dt_termino_vigencia), DATEADD(DAY, 365, COALESCE(TRY_CONVERT(date, sc.dt_inicio), CAST(GETDATE() AS date)))) AS dt_termino,
            TRY_CONVERT(decimal(18,2), sc.extensao_total) AS ext_obra,
            TRY_CONVERT(decimal(18,2), sc.vr_medicao_pi_mais_r) AS vr_medicao_pi_mais_r,
            CASE WHEN sc.vr_inicial IS NULL THEN NULL WHEN CHARINDEX(',', LTRIM(RTRIM(CAST(sc.vr_inicial AS varchar(80))))) > 0 THEN TRY_CONVERT(decimal(18,2), REPLACE(REPLACE(LTRIM(RTRIM(CAST(sc.vr_inicial AS varchar(80)))), '.', ''), ',', '.')) ELSE TRY_CONVERT(decimal(18,2), LTRIM(RTRIM(CAST(sc.vr_inicial AS varchar(80))))) END AS valor_contratado,
            CASE WHEN sc.vr_total_aditivo IS NULL THEN NULL WHEN CHARINDEX(',', LTRIM(RTRIM(CAST(sc.vr_total_aditivo AS varchar(80))))) > 0 THEN TRY_CONVERT(decimal(18,2), REPLACE(REPLACE(LTRIM(RTRIM(CAST(sc.vr_total_aditivo AS varchar(80)))), '.', ''), ',', '.')) ELSE TRY_CONVERT(decimal(18,2), LTRIM(RTRIM(CAST(sc.vr_total_aditivo AS varchar(80))))) END AS valor_aditivado,
            CASE WHEN sc.vr_total_reajuste IS NULL THEN NULL WHEN CHARINDEX(',', LTRIM(RTRIM(CAST(sc.vr_total_reajuste AS varchar(80))))) > 0 THEN TRY_CONVERT(decimal(18,2), REPLACE(REPLACE(LTRIM(RTRIM(CAST(sc.vr_total_reajuste AS varchar(80)))), '.', ''), ',', '.')) ELSE TRY_CONVERT(decimal(18,2), LTRIM(RTRIM(CAST(sc.vr_total_reajuste AS varchar(80))))) END AS valor_reajuste,
            CASE WHEN sc.vr_total_empenho IS NULL THEN NULL WHEN CHARINDEX(',', LTRIM(RTRIM(CAST(sc.vr_total_empenho AS varchar(80))))) > 0 THEN TRY_CONVERT(decimal(18,2), REPLACE(REPLACE(LTRIM(RTRIM(CAST(sc.vr_total_empenho AS varchar(80)))), '.', ''), ',', '.')) ELSE TRY_CONVERT(decimal(18,2), LTRIM(RTRIM(CAST(sc.vr_total_empenho AS varchar(80))))) END AS empenhado
        FROM siac_ranked sc WHERE sc.rn = 1
    )
    INSERT INTO #base (contrato_obra, contrato_supervisao, nome_supervisora, id_supervisora, uf, construtora, br, br_config, situacao_contrato, situacao_supervisao, num_processo, unidade_gestora, tipo, programa, dt_inicio, dt_termino, ext_obra, vr_medicao_pi_mais_r, valor_contratado, valor_aditivado, valor_reajuste, total, empenhado, supervisora_fmt)
    SELECT
        v.contrato_obra, v.contrato_supervisao, v.nome_supervisora, sps.id_supervisora,
        NULLIF(LTRIM(RTRIM(s.uf)), ''), NULLIF(LTRIM(RTRIM(s.construtora)), ''),
        COALESCE(seg.br_segmento, NULLIF(LTRIM(RTRIM(s.trecho)), ''), '-'),
        COALESCE(seg_br.br_lista, seg.br_segmento, CASE WHEN PATINDEX('%BR-[0-9]%', ISNULL(s.trecho, '')) > 0 THEN 'BR-' + LEFT(SUBSTRING(s.trecho, PATINDEX('%BR-[0-9]%', s.trecho) + 3, 10), PATINDEX('%[^0-9]%', SUBSTRING(s.trecho, PATINDEX('%BR-[0-9]%', s.trecho) + 3, 10) + 'X') - 1) WHEN PATINDEX('%BR-[0-9]%', ISNULL(s.objeto_contratacao, '')) > 0 THEN 'BR-' + LEFT(SUBSTRING(s.objeto_contratacao, PATINDEX('%BR-[0-9]%', s.objeto_contratacao) + 3, 10), PATINDEX('%[^0-9]%', SUBSTRING(s.objeto_contratacao, PATINDEX('%BR-[0-9]%', s.objeto_contratacao) + 3, 10) + 'X') - 1) ELSE NULL END, '-'),
        s.situacao_contrato, v.situacao_supervisao, s.num_processo, s.unidade_gestora, s.tipo, s.programa,
        s.dt_inicio, s.dt_termino, s.ext_obra, s.vr_medicao_pi_mais_r,
        s.valor_contratado, s.valor_aditivado, s.valor_reajuste,
        ISNULL(s.valor_contratado, 0) + ISNULL(s.valor_aditivado, 0) + ISNULL(s.valor_reajuste, 0) AS total,
        s.empenhado,
        CASE WHEN NULLIF(v.contrato_supervisao, '') IS NULL AND NULLIF(v.nome_supervisora, '') IS NULL THEN '-' WHEN NULLIF(v.contrato_supervisao, '') IS NULL THEN v.nome_supervisora WHEN NULLIF(v.nome_supervisora, '') IS NULL THEN v.contrato_supervisao ELSE v.contrato_supervisao + ' - ' + v.nome_supervisora END AS supervisora_fmt
    FROM vinc v
    INNER JOIN siac s ON s.contrato = v.contrato_obra
    OUTER APPLY (SELECT TOP (1) ts.id_supervisora FROM dbo.TB_SUPERVISORA ts WHERE NULLIF(LTRIM(RTRIM(v.nome_supervisora)), '') IS NOT NULL AND UPPER(LTRIM(RTRIM(ts.supervisora))) COLLATE Latin1_General_CI_AI = UPPER(LTRIM(RTRIM(v.nome_supervisora))) COLLATE Latin1_General_CI_AI ORDER BY ts.id_supervisora DESC) sps
    OUTER APPLY (SELECT TOP (1) COALESCE(NULLIF(LTRIM(RTRIM(seg.rodovia)), ''), CASE WHEN PATINDEX('%BR-[0-9]%', ISNULL(seg.trecho, '')) > 0 THEN 'BR-' + LEFT(SUBSTRING(seg.trecho, PATINDEX('%BR-[0-9]%', seg.trecho) + 3, 10), PATINDEX('%[^0-9]%', SUBSTRING(seg.trecho, PATINDEX('%BR-[0-9]%', seg.trecho) + 3, 10) + 'X') - 1) ELSE NULL END) AS br_segmento FROM dbo.TB_SIAC_SEGMENTO seg WHERE LTRIM(RTRIM(seg.contrato)) = v.contrato_obra ORDER BY seg.id_siac_segmento DESC) seg
    OUTER APPLY (SELECT NULLIF(LTRIM(RTRIM(STRING_AGG(CAST(r.rodovia AS nvarchar(50)), N' ') WITHIN GROUP (ORDER BY r.rodovia))), N'') AS br_lista FROM (SELECT DISTINCT LTRIM(RTRIM(seg.rodovia)) AS rodovia FROM dbo.TB_SIAC_SEGMENTO seg WHERE LTRIM(RTRIM(seg.contrato)) = v.contrato_obra AND NULLIF(LTRIM(RTRIM(seg.rodovia)), '') IS NOT NULL) r) seg_br;

    IF NOT EXISTS (SELECT 1 FROM #base)
        THROW 51003, 'Nenhum vinculo obra/supervisao encontrado em TB_SIAC_CONTRATO_SUPERVISORA para os filtros informados.', 1;

    SELECT d.contrato_obra, d.contrato_supervisao, d.nome_supervisora, d.id_supervisora, d.uf, d.construtora, d.br, d.br_config, d.situacao_contrato, d.situacao_supervisao, d.num_processo, d.unidade_gestora, d.tipo, d.programa, d.dt_inicio, d.dt_termino, d.ext_obra, d.vr_medicao_pi_mais_r, d.valor_contratado, d.valor_aditivado, d.valor_reajuste, d.total, d.empenhado, d.supervisora_fmt
    INTO #base_u
    FROM (SELECT b.*, ROW_NUMBER() OVER (PARTITION BY b.contrato_obra ORDER BY CASE WHEN b.id_supervisora IS NOT NULL THEN 0 ELSE 1 END, b.id_supervisora DESC, b.contrato_supervisao DESC) AS rn_base FROM #base b) d
    WHERE d.rn_base = 1;

    ALTER TABLE #base_u ADD
        existe_tb_contrato_obra        bit NOT NULL DEFAULT (0),
        existe_tb_config_v2            bit NOT NULL DEFAULT (0),
        existe_tb_supervisora_contrato bit NOT NULL DEFAULT (0),
        existe_historico_obra          bit NOT NULL DEFAULT (0),
        existe_historico_config        bit NOT NULL DEFAULT (0),
        existe_historico_supervisora   bit NOT NULL DEFAULT (0),
        tabelas_historico              nvarchar(2000) NULL,
        pode_sincronizar               bit NOT NULL DEFAULT (0);

    IF OBJECT_ID('tempdb..#catalogo_historico') IS NOT NULL DROP TABLE #catalogo_historico;
    CREATE TABLE #catalogo_historico (grupo varchar(30) NOT NULL, nome_tabela sysname NOT NULL, coluna_contrato sysname NOT NULL);

    INSERT INTO #catalogo_historico (grupo, nome_tabela, coluna_contrato)
    SELECT v.grupo, v.nome_tabela, col.coluna_contrato
    FROM (SELECT N'OBRA' AS grupo, N'TB_CONTRATO_OBRA_old' AS nome_tabela UNION ALL SELECT N'OBRA', N'TB_CONTRATO_OBRA_HISTORICO' UNION ALL SELECT N'OBRA', N'TB_CONTRATO_OBRA231220210943' UNION ALL SELECT N'CONFIG', N'TB_CONFIG_SUPERVISORA_old' UNION ALL SELECT N'CONFIG', N'TB_CONFIG_SUPERVISORA' UNION ALL SELECT N'CONFIG', N'TB_CONFIG_SUPERVISORA_V2_HISTORICO' UNION ALL SELECT N'CONFIG', N'TB_CONFIG_SUPERVISORA_V2_old_21092020_1043' UNION ALL SELECT N'CONFIG', N'TB_CONFIG_SUPERVISORA_V2_OLD_30012024_1257' UNION ALL SELECT N'SUPERVISORA', N'TB_CONTRATO_SUPERVISORA_old_04062025_0928') v
    CROSS APPLY (SELECT TOP (1) c.name AS coluna_contrato FROM sys.columns c INNER JOIN sys.tables t ON t.object_id = c.object_id INNER JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = N'dbo' AND t.name = v.nome_tabela AND c.name IN (N'contrato_obra', N'contrato') ORDER BY CASE c.name WHEN N'contrato_obra' THEN 0 WHEN N'contrato' THEN 1 ELSE 9 END) col
    WHERE OBJECT_ID(QUOTENAME(N'dbo') + N'.' + QUOTENAME(v.nome_tabela), N'U') IS NOT NULL AND col.coluna_contrato IS NOT NULL;

    DECLARE @grupo_hist varchar(30), @tabela_hist sysname, @coluna_hist sysname, @sql_historico nvarchar(max);
    DECLARE cur_hist CURSOR LOCAL FAST_FORWARD FOR SELECT grupo, nome_tabela, coluna_contrato FROM #catalogo_historico ORDER BY grupo, nome_tabela;
    OPEN cur_hist; FETCH NEXT FROM cur_hist INTO @grupo_hist, @tabela_hist, @coluna_hist;
    WHILE @@FETCH_STATUS = 0
    BEGIN
        BEGIN TRY
            SET @sql_historico = N'INSERT INTO #historico_hit (contrato_obra, grupo, nome_tabela) SELECT b.contrato_obra, @grupo_in, @tabela_in FROM #base_u b WHERE EXISTS (SELECT 1 FROM dbo.' + QUOTENAME(@tabela_hist) + N' x WHERE LTRIM(RTRIM(CONVERT(nvarchar(50), x.' + QUOTENAME(@coluna_hist) + N'))) = b.contrato_obra);';
            EXEC sys.sp_executesql @sql_historico, N'@grupo_in varchar(30), @tabela_in sysname', @grupo_in = @grupo_hist, @tabela_in = @tabela_hist;
        END TRY
        BEGIN CATCH
            INSERT INTO #pendencias (contrato_obra, contrato_supervisao, nome_supervisora, motivo) SELECT N'-', NULL, NULL, N'Historico: falha ao consultar ' + @tabela_hist + N' (coluna ' + @coluna_hist + N') - ' + ERROR_MESSAGE();
        END CATCH;
        FETCH NEXT FROM cur_hist INTO @grupo_hist, @tabela_hist, @coluna_hist;
    END;
    CLOSE cur_hist; DEALLOCATE cur_hist;

    UPDATE b SET
        b.existe_tb_contrato_obra = CASE WHEN EXISTS (SELECT 1 FROM dbo.TB_CONTRATO_OBRA co WHERE LTRIM(RTRIM(co.contrato)) = b.contrato_obra) THEN 1 ELSE 0 END,
        b.existe_tb_config_v2 = CASE WHEN EXISTS (SELECT 1 FROM dbo.TB_CONFIG_SUPERVISORA_V2 cfg WHERE LTRIM(RTRIM(cfg.contrato_obra)) = b.contrato_obra) THEN 1 ELSE 0 END,
        b.existe_tb_supervisora_contrato = CASE WHEN b.id_supervisora IS NULL THEN 0 WHEN EXISTS (SELECT 1 FROM dbo.TB_SUPERVISORA_CONTRATO sc WHERE sc.id_supervisora = b.id_supervisora AND LTRIM(RTRIM(sc.contrato_obra)) = b.contrato_obra) THEN 1 ELSE 0 END,
        b.existe_historico_obra = CASE WHEN ho.cnt > 0 THEN 1 ELSE 0 END,
        b.existe_historico_config = CASE WHEN hc.cnt > 0 THEN 1 ELSE 0 END,
        b.existe_historico_supervisora = CASE WHEN hs.cnt > 0 THEN 1 ELSE 0 END,
        b.tabelas_historico = NULLIF(LTRIM(RTRIM(COALESCE(ho.lista, N'') + CASE WHEN ho.lista IS NOT NULL AND hc.lista IS NOT NULL THEN N'; ' ELSE N'' END + COALESCE(hc.lista, N'') + CASE WHEN (ho.lista IS NOT NULL OR hc.lista IS NOT NULL) AND hs.lista IS NOT NULL THEN N'; ' ELSE N'' END + COALESCE(hs.lista, N''))), N'')
    FROM #base_u b
    OUTER APPLY (SELECT COUNT(*) AS cnt, STRING_AGG(h.nome_tabela, N', ') WITHIN GROUP (ORDER BY h.nome_tabela) AS lista FROM #historico_hit h WHERE h.contrato_obra = b.contrato_obra AND h.grupo = N'OBRA') ho
    OUTER APPLY (SELECT COUNT(*) AS cnt, STRING_AGG(h.nome_tabela, N', ') WITHIN GROUP (ORDER BY h.nome_tabela) AS lista FROM #historico_hit h WHERE h.contrato_obra = b.contrato_obra AND h.grupo = N'CONFIG') hc
    OUTER APPLY (SELECT COUNT(*) AS cnt, STRING_AGG(h.nome_tabela, N', ') WITHIN GROUP (ORDER BY h.nome_tabela) AS lista FROM #historico_hit h WHERE h.contrato_obra = b.contrato_obra AND h.grupo = N'SUPERVISORA') hs;

    UPDATE b SET b.pode_sincronizar = CASE WHEN b.id_supervisora IS NULL THEN 0 WHEN b.existe_historico_obra = 1 AND b.existe_tb_contrato_obra = 0 THEN 0 WHEN b.existe_historico_config = 1 AND b.existe_tb_config_v2 = 0 THEN 0 WHEN b.existe_historico_supervisora = 1 AND b.existe_tb_supervisora_contrato = 0 THEN 0 ELSE 1 END FROM #base_u b;

    INSERT INTO #pendencias (contrato_obra, contrato_supervisao, nome_supervisora, motivo)
    SELECT b.contrato_obra, b.contrato_supervisao, b.nome_supervisora, CASE WHEN NULLIF(LTRIM(RTRIM(b.nome_supervisora)), '') IS NULL THEN N'Contrato de supervisao sem supervisora em TB_SIAC_CONTRATO_SUPERVISORA' ELSE N'Supervisora nao cadastrada em TB_SUPERVISORA' END
    FROM #base_u b WHERE b.id_supervisora IS NULL;

    INSERT INTO #pendencias (contrato_obra, contrato_supervisao, nome_supervisora, motivo)
    SELECT b.contrato_obra, b.contrato_supervisao, b.nome_supervisora, N'Contrato somente em tabela old/historico (' + b.tabelas_historico + N'); ausente na tabela ativa correspondente - INSERT bloqueado'
    FROM #base_u b WHERE b.pode_sincronizar = 0 AND b.id_supervisora IS NOT NULL AND NULLIF(b.tabelas_historico, N'') IS NOT NULL;

    INSERT INTO #pendencias (contrato_obra, contrato_supervisao, nome_supervisora, motivo)
    SELECT b.contrato_obra, b.contrato_supervisao, b.nome_supervisora, N'TB_CONFIG_SUPERVISORA_V2: status_supervisao fora da regra de INSERT (' + ISNULL(COALESCE(b.situacao_supervisao, b.situacao_contrato), N'NULL') + N')'
    FROM #base_u b WHERE b.id_supervisora IS NOT NULL AND b.pode_sincronizar = 1 AND NOT (COALESCE(b.situacao_supervisao, b.situacao_contrato) IS NULL OR LTRIM(RTRIM(COALESCE(b.situacao_supervisao, b.situacao_contrato))) IN (N'ATIVO', N'ATIVO - AGUARDANDO CONCLUSÃO', N'CADASTRADO', N'CONCLUÍDO', N'Não possui Supervisora', N'PARALISADO'));

    INSERT INTO dbo.TB_CONTRATO_OBRA (id_usuario, contrato, uf, construtora, supervisora, br, situacao_contrato, num_processo, unidade_gestora, observacao, tipo)
    OUTPUT N'TB_CONTRATO_OBRA', N'INSERT', inserted.contrato, inserted.id_contrato_obra, N'supervisora=' + COALESCE(inserted.supervisora, N'') + N' | br=' + COALESCE(inserted.br, N'') INTO #log_exec (tabela, acao, contrato_obra, id_registro, detalhe)
    SELECT NULL, b.contrato_obra, ISNULL(NULLIF(LTRIM(RTRIM(b.uf)), ''), '-'), ISNULL(NULLIF(LTRIM(RTRIM(b.construtora)), ''), '-'), b.supervisora_fmt, ISNULL(NULLIF(LTRIM(RTRIM(b.br)), ''), '-'), b.situacao_contrato, b.num_processo, b.unidade_gestora, NULL, b.tipo
    FROM #base_u b WHERE b.id_supervisora IS NOT NULL AND b.pode_sincronizar = 1 AND NOT EXISTS (SELECT 1 FROM dbo.TB_CONTRATO_OBRA co WHERE LTRIM(RTRIM(co.contrato)) = b.contrato_obra);

    CREATE TABLE #co_inseridos (contrato_obra varchar(50) NOT NULL PRIMARY KEY);
    INSERT INTO #co_inseridos (contrato_obra) SELECT DISTINCT LTRIM(RTRIM(l.contrato_obra)) FROM #log_exec l WHERE l.tabela = N'TB_CONTRATO_OBRA' AND l.acao = N'INSERT' AND NULLIF(LTRIM(RTRIM(l.contrato_obra)), '') IS NOT NULL;

    CREATE TABLE #map_co (contrato_obra varchar(50) NOT NULL, id_contrato_obra int NOT NULL);
    INSERT INTO #map_co (contrato_obra, id_contrato_obra) SELECT m.contrato_obra, m.id_contrato_obra FROM (SELECT b.contrato_obra, co.id_contrato_obra, ROW_NUMBER() OVER (PARTITION BY b.contrato_obra ORDER BY co.id_contrato_obra DESC) AS rn_map FROM #base_u b INNER JOIN #co_inseridos ci ON ci.contrato_obra = LTRIM(RTRIM(b.contrato_obra)) INNER JOIN dbo.TB_CONTRATO_OBRA co ON LTRIM(RTRIM(co.contrato)) = b.contrato_obra WHERE b.id_supervisora IS NOT NULL AND b.pode_sincronizar = 1) m WHERE m.rn_map = 1;

    INSERT INTO dbo.TB_SUPERVISORA_CONTRATO (id_supervisora, contrato_obra, ultima_alteracao, id_usuario, publicar, dablicar, id_usuario_publicar)
    OUTPUT N'TB_SUPERVISORA_CONTRATO', N'INSERT', inserted.contrato_obra, inserted.id_supervisoracontrato, N'id_supervisora=' + CONVERT(nvarchar(20), inserted.id_supervisora) INTO #log_exec (tabela, acao, contrato_obra, id_registro, detalhe)
    SELECT DISTINCT b.id_supervisora, LTRIM(RTRIM(b.contrato_obra)), GETDATE(), @IdUsuario, NULL, NULL, NULL
    FROM #base_u b INNER JOIN #co_inseridos ci ON ci.contrato_obra = LTRIM(RTRIM(b.contrato_obra))
    WHERE b.id_supervisora IS NOT NULL AND b.pode_sincronizar = 1 AND NULLIF(LTRIM(RTRIM(b.contrato_obra)), '') IS NOT NULL AND NOT EXISTS (SELECT 1 FROM dbo.TB_SUPERVISORA_CONTRATO sc WHERE sc.id_supervisora = b.id_supervisora AND LTRIM(RTRIM(sc.contrato_obra)) = LTRIM(RTRIM(b.contrato_obra)));

    INSERT INTO dbo.TB_CONFIG_SUPERVISORA_V2 (contrato_obra, executora, br, uf, contrato_supervisao, status_supervisao, supervisora, contrato_gerenciamento, gerenciadora, ext_obra, ext_superv, ext_gerenc, valor_contratado, valor_aditivado, valor_reajuste, total, repassado, empenhado, data_inicio, data_termino, tipo_contratacao, programa, publicar, km_inicial, km_final, total_float, vr_medicao_pi_mais_r, ultima_alteracao)
    OUTPUT N'TB_CONFIG_SUPERVISORA_V2', N'INSERT', inserted.contrato_obra, inserted.id_configsupervisora, N'br=' + COALESCE(inserted.br, N'') + N' | supervisora=' + COALESCE(inserted.supervisora, N'') + N' | contrato_supervisao=' + COALESCE(inserted.contrato_supervisao, N'') + N' | status_supervisao=' + COALESCE(inserted.status_supervisao, N'NULL') INTO #log_exec (tabela, acao, contrato_obra, id_registro, detalhe)
    SELECT src.contrato_obra, src.executora, src.br, src.uf, src.contrato_supervisao, src.status_supervisao, src.supervisora, NULL, NULL, CONVERT(nvarchar(50), ISNULL(src.ext_obra, 0)), N'0', N'0', CONVERT(nvarchar(50), ISNULL(src.valor_contratado, 0)), CONVERT(nvarchar(50), ISNULL(src.valor_aditivado, 0)), CONVERT(nvarchar(50), ISNULL(src.valor_reajuste, 0)), CONVERT(nvarchar(50), ISNULL(src.total, 0)), NULL, CASE WHEN src.empenhado IS NULL THEN NULL ELSE CONVERT(nvarchar(50), src.empenhado) END, CONVERT(nvarchar(50), src.dt_inicio, 103), CONVERT(nvarchar(50), src.dt_termino, 103), src.tipo_contratacao, src.programa, NULL, NULL, NULL, TRY_CONVERT(float, src.total), TRY_CONVERT(float, src.vr_medicao_pi_mais_r), GETDATE()
    FROM (SELECT b.contrato_obra, ISNULL(NULLIF(LTRIM(RTRIM(b.construtora)), ''), '-') AS executora, ISNULL(NULLIF(LTRIM(RTRIM(b.br_config)), N''), N'-') AS br, ISNULL(NULLIF(LTRIM(RTRIM(b.uf)), ''), '-') AS uf, b.contrato_supervisao, COALESCE(b.situacao_supervisao, b.situacao_contrato) AS status_supervisao, b.nome_supervisora AS supervisora, b.ext_obra, b.valor_contratado, b.valor_aditivado, b.valor_reajuste, b.total, b.empenhado, b.dt_inicio, b.dt_termino, ISNULL(NULLIF(LTRIM(RTRIM(b.tipo)), ''), '-') AS tipo_contratacao, b.programa, b.vr_medicao_pi_mais_r FROM #base_u b WHERE b.id_supervisora IS NOT NULL AND b.pode_sincronizar = 1 AND (COALESCE(b.situacao_supervisao, b.situacao_contrato) IS NULL OR LTRIM(RTRIM(COALESCE(b.situacao_supervisao, b.situacao_contrato))) IN (N'ATIVO', N'ATIVO - AGUARDANDO CONCLUSÃO', N'CADASTRADO', N'CONCLUÍDO', N'Não possui Supervisora', N'PARALISADO'))) AS src
    WHERE NOT EXISTS (SELECT 1 FROM dbo.TB_CONFIG_SUPERVISORA_V2 cfg WHERE LTRIM(RTRIM(cfg.contrato_obra)) = src.contrato_obra);

    INSERT INTO dbo.TB_CONTRATO_OBRA_VGEO (id_contrato_obra, id_usuario, contrato, uf, construtora, supervisora, br, situacao_contrato, num_processo, unidade_gestora, observacao, tipo)
    OUTPUT N'TB_CONTRATO_OBRA_VGEO', N'INSERT', inserted.contrato, inserted.id_contrato_obra_vgeo, N'id_contrato_obra=' + CONVERT(nvarchar(20), inserted.id_contrato_obra) + N' | br=' + COALESCE(inserted.br, N'') INTO #log_exec (tabela, acao, contrato_obra, id_registro, detalhe)
    SELECT src.id_contrato_obra, NULL, src.contrato, src.uf, src.construtora, src.supervisora, src.br, src.situacao_contrato, src.num_processo, src.unidade_gestora, NULL, src.tipo
    FROM (SELECT m.id_contrato_obra, b.contrato_obra AS contrato, ISNULL(NULLIF(LTRIM(RTRIM(b.uf)), ''), '-') AS uf, ISNULL(NULLIF(LTRIM(RTRIM(b.construtora)), ''), '-') AS construtora, b.supervisora_fmt AS supervisora, ISNULL(NULLIF(LTRIM(RTRIM(b.br)), ''), '-') AS br, b.situacao_contrato, b.num_processo, b.unidade_gestora, b.tipo FROM #base_u b INNER JOIN #map_co m ON m.contrato_obra = b.contrato_obra INNER JOIN #co_inseridos ci ON ci.contrato_obra = LTRIM(RTRIM(b.contrato_obra)) WHERE b.id_supervisora IS NOT NULL AND b.pode_sincronizar = 1) AS src
    WHERE NOT EXISTS (SELECT 1 FROM dbo.TB_CONTRATO_OBRA_VGEO vg WHERE LTRIM(RTRIM(vg.contrato)) = LTRIM(RTRIM(src.contrato)));

    COMMIT;

    -- RS0: resumo por tabela
    SELECT t.tabela, ISNULL(i.qtd_insert, 0) AS qtd_insert
    FROM (VALUES (N'TB_CONTRATO_OBRA'), (N'TB_SUPERVISORA_CONTRATO'), (N'TB_CONFIG_SUPERVISORA_V2'), (N'TB_CONTRATO_OBRA_VGEO')) AS t(tabela)
    LEFT JOIN (SELECT l.tabela, COUNT(*) AS qtd_insert FROM #log_exec l WHERE l.acao = N'INSERT' GROUP BY l.tabela) AS i ON i.tabela = t.tabela
    ORDER BY t.tabela;

    -- RS1: total
    SELECT COUNT(*) AS qtd_insert_total FROM #log_exec l WHERE l.acao = N'INSERT';

    -- RS2: contratos considerados
    SELECT b.contrato_obra, b.contrato_supervisao, b.nome_supervisora, b.id_supervisora, b.br, b.supervisora_fmt, b.situacao_contrato, b.situacao_supervisao, b.pode_sincronizar, b.tabelas_historico, CASE WHEN b.id_supervisora IS NULL THEN N'PENDENTE' WHEN b.pode_sincronizar = 0 AND NULLIF(b.tabelas_historico, N'') IS NOT NULL THEN N'BLOQUEADO_HISTORICO' WHEN b.pode_sincronizar = 1 THEN N'PROCESSADO' ELSE N'PENDENTE' END AS status_execucao
    FROM #base_u b ORDER BY b.contrato_obra;

    -- RS3: log de INSERTs
    SELECT l.tabela, l.acao, l.contrato_obra, l.id_registro, l.detalhe FROM #log_exec l WHERE l.acao = N'INSERT' ORDER BY l.id_log;

    -- RS4: pendencias
    SELECT p.contrato_obra, p.contrato_supervisao, p.nome_supervisora, p.motivo FROM #pendencias p ORDER BY p.contrato_obra;

    IF OBJECT_ID('tempdb..#log_exec') IS NOT NULL DROP TABLE #log_exec;
    IF OBJECT_ID('tempdb..#historico_hit') IS NOT NULL DROP TABLE #historico_hit;
    IF OBJECT_ID('tempdb..#catalogo_historico') IS NOT NULL DROP TABLE #catalogo_historico;
    IF OBJECT_ID('tempdb..#map_co') IS NOT NULL DROP TABLE #map_co;
    IF OBJECT_ID('tempdb..#co_inseridos') IS NOT NULL DROP TABLE #co_inseridos;
    IF OBJECT_ID('tempdb..#base_u') IS NOT NULL DROP TABLE #base_u;
    IF OBJECT_ID('tempdb..#base') IS NOT NULL DROP TABLE #base;
    IF OBJECT_ID('tempdb..#pendencias') IS NOT NULL DROP TABLE #pendencias;

END TRY
BEGIN CATCH
    IF @@TRANCOUNT > 0 ROLLBACK;
    IF OBJECT_ID('tempdb..#log_exec') IS NOT NULL DROP TABLE #log_exec;
    IF OBJECT_ID('tempdb..#historico_hit') IS NOT NULL DROP TABLE #historico_hit;
    IF OBJECT_ID('tempdb..#catalogo_historico') IS NOT NULL DROP TABLE #catalogo_historico;
    IF OBJECT_ID('tempdb..#map_co') IS NOT NULL DROP TABLE #map_co;
    IF OBJECT_ID('tempdb..#co_inseridos') IS NOT NULL DROP TABLE #co_inseridos;
    IF OBJECT_ID('tempdb..#base_u') IS NOT NULL DROP TABLE #base_u;
    IF OBJECT_ID('tempdb..#base') IS NOT NULL DROP TABLE #base;
    IF OBJECT_ID('tempdb..#pendencias') IS NOT NULL DROP TABLE #pendencias;
    THROW;
END CATCH;
"""

RS_LABELS = [
    "resumo_tabelas",
    "total",
    "contratos",
    "log_inserts",
    "pendencias",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--contrato-obra",       default=None)
    ap.add_argument("--contrato-supervisora", default=None)
    args = ap.parse_args()

    load_env()
    try:
        ep = supra_targets_for_mode(pick_supra_mode())[0]
        conn = connect_endpoint(ep)
        conn.autocommit = True   # a query gerencia sua própria transação
    except Exception as e:
        print(json.dumps({"erro": str(e)}))
        return

    try:
        cur = conn.cursor()
        cur.execute(SQL, (args.contrato_obra, args.contrato_supervisora))

        result_sets = []
        rs_idx = 0
        while True:
            try:
                cols = [d[0] for d in cur.description]
                rows = [[_serialize(v) for v in row] for row in cur.fetchall()]
                label = RS_LABELS[rs_idx] if rs_idx < len(RS_LABELS) else f"rs{rs_idx}"
                result_sets.append({"label": label, "columns": cols, "rows": rows})
                rs_idx += 1
            except Exception:
                pass
            if not cur.nextset():
                break

        print(json.dumps({
            "status": "ok",
            "target": ep.label,
            "result_sets": result_sets,
        }, ensure_ascii=False))

    except Exception as e:
        print(json.dumps({"erro": str(e)}))
    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
