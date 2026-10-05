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
  metrica,
  COUNT(*) AS registros,
  COUNTIF(valor IS NULL) AS sem_valor,
  COUNTIF(valor < 0) AS negativos,
  MIN(valor) AS menor_valor,
  MAX(valor) AS maior_valor
FROM rex-bi-producao.rex.producao_diaria_piloto_2025_2026
UNPIVOT INCLUDE NULLS (
  valor FOR metrica IN (
    horas_trabalhando,
    horas_total,
    area,
    distancia,
    combustivel
  )
)
GROUP BY metrica
ORDER BY metrica;