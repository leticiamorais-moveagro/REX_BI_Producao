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

SELECT
  EXTRACT(YEAR FROM data) AS ano,
  EXTRACT(MONTH FROM data) AS mes,
  COUNT(*) AS registros,
  COUNT(DISTINCT data) AS dias_com_dados,
  COUNT(DISTINCT equipamento_chave) AS equipamentos,
  COUNT(DISTINCT unidade_codigo) AS unidades
FROM rex-bi-producao.rex.producao_diaria_piloto_2025_2026
GROUP BY ano, mes
ORDER BY ano, mes;