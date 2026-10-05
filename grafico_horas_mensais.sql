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
  FORMAT_DATE('%Y-%m', data) AS mes_referencia,
  ROUND(SUM(horas_trabalhando), 2) AS horas_registradas
FROM rex-bi-producao.rex.producao_diaria_piloto_2025_2026
GROUP BY mes_referencia
ORDER BY mes_referencia;