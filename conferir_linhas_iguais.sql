/* @datacloud.settings
{
  "version": 1,
  "service": "BIG_QUERY",
  "connectionInfo": {
    "billingProjectId": "INHERIT"
  },
  "dialect": "GOOGLE_SQL"
}
*/

WITH conteudo AS (
  SELECT
    TO_JSON_STRING((
      SELECT AS STRUCT t.* EXCEPT (importado_em, carga_id)
    )) AS registro
  FROM rex-bi-producao.rex.producao_diaria_piloto_2025_2026 AS t
),
contagem AS (
  SELECT
    registro,
    COUNT(*) AS quantidade
  FROM conteudo
  GROUP BY registro
)
SELECT
  SUM(quantidade) AS registros_avaliados,
  COUNTIF(quantidade > 1) AS grupos_com_linhas_iguais,
  SUM(quantidade - 1) AS linhas_iguais_adicionais
FROM contagem;