import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from google.cloud import bigquery
from google.api_core.exceptions import (
    Conflict,
    GoogleAPICallError,
    NotFound,
)


PROJETO = "rex-bi-producao"
REGIAO = "southamerica-east1"
DESTINO = f"{PROJETO}.rex.producao_diaria_piloto_2025_2026"

PASTA = Path(
    "C:/Consultas Dados/REX_PC_Trabalho/exportacao_piloto_rex"
)
ARQUIVO = PASTA / "producao_diaria_2025-01-01_2026-10-04.ndjson"
RESUMO = Path(str(ARQUIVO) + ".resumo.json")
RECIBO = PASTA / "recibo_piloto_bigquery.json"

INICIO = date(2025, 1, 1)
FIM = date(2026, 10, 4)
REGISTROS = 58383
DIAS = 642

TIPOS = [
    ("data", "DATE"),
    ("equipamento_chave", "STRING"),
    ("unidade_codigo", "STRING"),
    ("unidade_nome", "STRING"),
    ("equipamento_codigo", "STRING"),
    ("modelo_codigo", "STRING"),
    ("modelo_nome", "STRING"),
    ("operacao_codigo", "STRING"),
    ("operacao_nome", "STRING"),
    ("classe_operacao", "STRING"),
    ("tipo_operacao", "STRING"),
    ("operador_codigo", "STRING"),
    ("operador_nome", "STRING"),
    ("turno_codigo", "STRING"),
    ("turno_nome", "STRING"),
    ("centro_custo_codigo", "STRING"),
    ("centro_custo_nome", "STRING"),
    ("horimetro_horas", "NUMERIC"),
    ("horas_trabalhando", "NUMERIC"),
    ("horas_deslocamento", "NUMERIC"),
    ("horas_motor_ocioso", "NUMERIC"),
    ("horas_parado", "NUMERIC"),
    ("horas_desligado", "NUMERIC"),
    ("horas_total", "NUMERIC"),
    ("area", "NUMERIC"),
    ("distancia", "NUMERIC"),
    ("combustivel", "NUMERIC"),
    ("snapshots", "INTEGER"),
    ("offline", "BOOLEAN"),
    ("importado_em", "TIMESTAMP"),
    ("carga_id", "STRING"),
]

ESQUEMA = [
    bigquery.SchemaField(nome, tipo, mode="NULLABLE")
    for nome, tipo in TIPOS
]
CAMPOS = {nome for nome, tipo in TIPOS}


class FalhaCarga(Exception):
    pass


def rejeitar_constante(valor):
    raise FalhaCarga("O arquivo contém NaN ou Infinity.")


def assinatura(esquema):
    aliases = {
        "INT64": "INTEGER",
        "BOOL": "BOOLEAN",
        "DECIMAL": "NUMERIC",
    }
    return sorted(
        (
            campo.name,
            aliases.get(campo.field_type, campo.field_type),
            campo.mode or "NULLABLE",
        )
        for campo in (esquema or [])
    )


def validar_arquivo(origem):
    resumo = json.loads(RESUMO.read_text(encoding="utf-8"))

    esperado = (
        resumo.get("inicio") == INICIO.isoformat()
        and resumo.get("fim") == FIM.isoformat()
        and resumo.get("dias_consultados") == DIAS
        and resumo.get("dias_sem_registros") == 0
        and resumo.get("registros") == REGISTROS
    )
    if not esperado:
        raise FalhaCarga("O resumo não corresponde ao piloto completo.")

    digest = hashlib.sha256()
    total = 0
    tamanho = 0
    datas = set()

    for numero, bruto in enumerate(origem, start=1):
        digest.update(bruto)
        tamanho += len(bruto)
        registro = json.loads(
            bruto,
            parse_constant=rejeitar_constante,
        )

        if not isinstance(registro, dict):
            raise FalhaCarga(f"Formato inválido na linha {numero}.")

        if set(registro) - CAMPOS:
            raise FalhaCarga(f"Coluna desconhecida na linha {numero}.")

        for nome in (
            "data", "equipamento_chave", "importado_em", "carga_id"
        ):
            valor = registro.get(nome)
            if not isinstance(valor, str) or not valor.strip():
                raise FalhaCarga(
                    f"Campo obrigatório {nome} inválido "
                    f"na linha {numero}."
                )

        dia = date.fromisoformat(registro["data"])
        if not INICIO <= dia <= FIM:
            raise FalhaCarga(f"Data fora do piloto na linha {numero}.")
        datas.add(dia)

        for nome, tipo in TIPOS:
            valor = registro.get(nome)
            if valor is None:
                continue

            if tipo == "STRING" and not isinstance(valor, str):
                raise FalhaCarga(
                    f"Texto inválido em {nome}, linha {numero}."
                )

            if tipo == "NUMERIC":
                if (
                    isinstance(valor, bool)
                    or not isinstance(valor, (str, int, float))
                    or not Decimal(str(valor)).is_finite()
                ):
                    raise FalhaCarga(
                        f"Número inválido em {nome}, linha {numero}."
                    )

            if tipo == "INTEGER" and type(valor) is not int:
                raise FalhaCarga(
                    f"Inteiro inválido em {nome}, linha {numero}."
                )

            if tipo == "BOOLEAN" and type(valor) is not bool:
                raise FalhaCarga(
                    f"Booleano inválido em {nome}, linha {numero}."
                )

            if tipo == "TIMESTAMP":
                momento = datetime.fromisoformat(valor)
                if momento.utcoffset() is None:
                    raise FalhaCarga(
                        f"Timestamp sem fuso na linha {numero}."
                    )

        total += 1

    sha = digest.hexdigest()

    if (
        total != REGISTROS
        or len(datas) != DIAS
        or tamanho != resumo.get("bytes")
        or sha != resumo.get("sha256")
    ):
        raise FalhaCarga(
            "O arquivo não corresponde ao resumo da exportação."
        )

    return sha, tamanho


def verificar_tabela(tabela, exigir_vazia=False):
    if (
        tabela.table_type != "TABLE"
        or tabela.location.lower() != REGIAO
        or tabela.time_partitioning is not None
        or tabela.range_partitioning is not None
        or assinatura(tabela.schema) != assinatura(ESQUEMA)
    ):
        raise FalhaCarga(
            "A tabela de destino tem uma configuração incompatível."
        )

    if exigir_vazia and tabela.num_rows != 0:
        raise FalhaCarga(
            "A tabela de destino já contém dados. "
            "Envie essa mensagem antes de continuar."
        )


def verificar_job(job):
    if not isinstance(job, bigquery.LoadJob):
        raise FalhaCarga("O job encontrado não é uma carga.")

    referencia = job.destination
    destino = (
        f"{referencia.project}."
        f"{referencia.dataset_id}."
        f"{referencia.table_id}"
        if referencia is not None else None
    )

    valido = (
        destino == DESTINO
        and job.project == PROJETO
        and job.location.lower() == REGIAO
        and job.source_format == "NEWLINE_DELIMITED_JSON"
        and job.write_disposition == "WRITE_EMPTY"
        and job.create_disposition == "CREATE_IF_NEEDED"
        and not job.autodetect
        and not job.ignore_unknown_values
        and job.max_bad_records in (None, 0)
        and assinatura(job.schema) == assinatura(ESQUEMA)
    )
    if not valido:
        raise FalhaCarga("O job encontrado tem outra configuração.")


def main():
    print("Conferindo arquivo e resumo...", flush=True)

    cliente = bigquery.Client(
        project=PROJETO,
        location=REGIAO,
    )

    config = bigquery.LoadJobConfig(
        schema=ESQUEMA,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=bigquery.WriteDisposition.WRITE_EMPTY,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        autodetect=False,
        max_bad_records=0,
        ignore_unknown_values=False,
    )

    with ARQUIVO.open("rb") as origem:
        sha, tamanho = validar_arquivo(origem)

        print(
            f"Arquivo validado: {REGISTROS} registros, {DIAS} datas.",
            flush=True,
        )

        dataset = cliente.get_dataset(f"{PROJETO}.rex")
        if dataset.location.lower() != REGIAO:
            raise FalhaCarga("A região do dataset é diferente.")

        identificacao = {
            "versao": 1,
            "arquivo_sha256": sha,
            "destino": DESTINO,
            "projeto": PROJETO,
            "regiao": REGIAO,
            "config": config.to_api_repr(),
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                identificacao,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        job_id = "rex_piloto_" + fingerprint

        try:
            job = cliente.get_job(
                job_id,
                project=PROJETO,
                location=REGIAO,
            )
            print("Recuperando a carga já enviada...", flush=True)

        except NotFound:
            job = None

        if job is None:
            try:
                tabela = cliente.get_table(DESTINO)
            except NotFound:
                tabela = None

            if tabela is not None:
                verificar_tabela(tabela, exigir_vazia=True)

            origem.seek(0)

            print(
                f"Enviando {tamanho / 1_000_000:.2f} MB "
                f"para {DESTINO}...",
                flush=True,
            )

            try:
                job = cliente.load_table_from_file(
                    origem,
                    DESTINO,
                    size=tamanho,
                    job_id=job_id,
                    project=PROJETO,
                    location=REGIAO,
                    job_config=config,
                    num_retries=3,
                    timeout=(30, 180),
                )
            except Conflict:
                job = cliente.get_job(
                    job_id,
                    project=PROJETO,
                    location=REGIAO,
                )

        verificar_job(job)

    print(f"Job: {job.job_id}", flush=True)
    print("Aguardando o BigQuery concluir a carga...", flush=True)

    try:
        job.result(timeout=1200)
    except Exception:
        if job.error_result:
            mensagem = job.error_result.get(
                "message", "Erro não detalhado."
            )
            raise FalhaCarga(
                f"O job terminou com erro: {mensagem}"
            ) from None

        raise FalhaCarga(
            "Não foi possível confirmar a conclusão. "
            "Repita o mesmo comando para recuperar a carga."
        ) from None

    tabela = cliente.get_table(DESTINO)
    verificar_tabela(tabela)

    if (
        job.output_rows != REGISTROS
        or tabela.num_rows != REGISTROS
    ):
        raise FalhaCarga(
            "A contagem no BigQuery está diferente da exportação."
        )

    expira = (
        tabela.expires.isoformat()
        if tabela.expires else "Não informado"
    )

    recibo = {
        "arquivo_sha256": sha,
        "tabela": DESTINO,
        "job_id": job.job_id,
        "registros": tabela.num_rows,
        "concluido_em": datetime.now(timezone.utc).isoformat(),
        "expira_em": expira,
    }

    temporario = Path(str(RECIBO) + ".tmp")
    temporario.write_text(
        json.dumps(recibo, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporario.replace(RECIBO)

    print("\nCARGA CONCLUÍDA NO BIGQUERY")
    print(f"Tabela: {DESTINO}")
    print(f"Registros: {tabela.num_rows}")
    print(f"Região: {tabela.location}")
    print(f"Expiração da tabela: {expira}")
    print(f"Recibo: {RECIBO}")


try:
    main()

except KeyboardInterrupt:
    print(
        "\nInterrompido. Repita o mesmo comando "
        "para verificar a carga."
    )
    raise SystemExit(130)

except (FalhaCarga, GoogleAPICallError) as erro:
    print(f"\nCARGA NÃO CONFIRMADA: {erro}")
    print("Envie essa mensagem para verificarmos.")
    raise SystemExit(1)

except Exception as erro:
    print(f"\nCARGA NÃO CONFIRMADA: {str(type(erro))}")
    print("Envie essa mensagem para verificarmos.")
    raise SystemExit(1)