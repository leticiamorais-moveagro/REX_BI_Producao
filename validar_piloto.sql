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
  COUNT(*) AS total_registros,
  COUNT(DISTINCT data) AS dias_com_dados,
  MIN(data) AS primeira_data,
  MAX(data) AS ultima_data
FROM rex-bi-producao.rex.producao_diaria_piloto_2025_2026;