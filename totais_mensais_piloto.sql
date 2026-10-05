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
  COUNT(DISTINCT equipamento_chave) AS equipamentos,
  ROUND(SUM(horas_trabalhando), 2) AS horas_registradas,
  ROUND(SUM(area), 2) AS area_registrada_ha,
  ROUND(SUM(combustivel), 2) AS combustivel_registrado_l
FROM rex-bi-producao.rex.producao_diaria_piloto_2025_2026
GROUP BY ano, mes
ORDER BY ano, mes;