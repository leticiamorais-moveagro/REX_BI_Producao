import argparse
import getpass
import hashlib
import json
import shutil
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from rex_api import RexClient, RexHTTPError
from rex_local.model import normalize_daily


PASTA = Path("C:/Consultas Dados/REX_PC_Trabalho") / "exportacao_piloto_rex"

CAMPOS = set("""
data equipamento_chave unidade_codigo unidade_nome
equipamento_codigo modelo_codigo modelo_nome
operacao_codigo operacao_nome classe_operacao tipo_operacao
operador_codigo operador_nome turno_codigo turno_nome
centro_custo_codigo centro_custo_nome horimetro_horas
horas_trabalhando horas_deslocamento horas_motor_ocioso
horas_parado horas_desligado horas_total area distancia
combustivel snapshots offline importado_em carga_id
""".split())


class ErroExportacao(Exception):
    pass


def serializar(valor):
    if isinstance(valor, Decimal):
        if not valor.is_finite():
            raise ErroExportacao("Foi encontrado um número inválido.")
        return str(valor)

    if isinstance(valor, (date, datetime)):
        return valor.isoformat()

    raise ErroExportacao("Tipo de dado incompatível.")


def checksum(arquivo):
    with arquivo.open("rb") as origem:
        return hashlib.file_digest(origem, "sha256").hexdigest()


def salvar_json(arquivo, conteudo):
    temporario = Path(str(arquivo) + ".tmp")
    temporario.write_text(
        json.dumps(conteudo, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporario.replace(arquivo)


def caminhos(dia):
    nome = dia.isoformat()
    return (
        PASTA / "diarios" / f"{nome}.ndjson",
        PASTA / "diarios" / f"{nome}.ok.json",
    )


def verificar_dia(dia, conta):
    arquivo, marcador = caminhos(dia)

    if not marcador.exists():
        return None

    info = json.loads(marcador.read_text(encoding="utf-8"))

    valido = (
        info.get("versao") == 1
        and info.get("data") == dia.isoformat()
        and info.get("conta_sha256") == conta
        and type(info.get("linhas")) is int
        and info["linhas"] >= 0
        and arquivo.is_file()
    )

    if not valido or checksum(arquivo) != info.get("sha256"):
        raise ErroExportacao(
            f"Arquivo ou marcador incompatível em {dia}. "
            "Envie essa mensagem antes de continuar."
        )

    return info


def consultar(sessao, dia):
    renovou = False
    falhas_transitorias = 0

    while True:
        try:
            return sessao["cliente"].daily_production(
                dia.isoformat(),
                dia.isoformat(),
            )

        except RexHTTPError as erro:
            if erro.status_code == 401:
                if renovou:
                    raise ErroExportacao(
                        f"HTTP 401 persistente em {dia}, "
                        "mesmo após tentar uma nova sessão."
                    ) from None

                renovou = True

                print(
                    f"  HTTP 401 em {dia}: autenticação rejeitada. "
                    "Tentando uma nova sessão uma vez...",
                    flush=True,
                )

                sessao["cliente"] = RexClient(
                    email=sessao["email"],
                    password=sessao["senha"],
                )
                continue

            transitorio = erro.status_code in (
                408, 429, 500, 502, 503, 504
            )

            if not transitorio:
                raise

            falhas_transitorias += 1

            if falhas_transitorias >= 3:
                raise

            espera = 2 ** falhas_transitorias

            print(
                f"  HTTP {erro.status_code} em {dia}: "
                f"nova tentativa em {espera}s.",
                flush=True,
            )

            time.sleep(espera)


def exportar_dia(sessao, dia, conta):
    payload = consultar(sessao, dia)
    momento = datetime.now(timezone.utc).isoformat()
    carga = uuid.uuid4().hex

    linhas = normalize_daily(
        payload,
        dia,
        imported_at=momento,
        load_id=carga,
    )

    if not isinstance(linhas, list):
        raise ErroExportacao("O conversor não retornou uma lista.")

    arquivo, marcador = caminhos(dia)
    temporario = Path(str(arquivo) + ".tmp")

    with temporario.open(
        "w", encoding="utf-8", newline="\n"
    ) as destino:
        for linha in linhas:
            if not isinstance(linha, dict):
                raise ErroExportacao(
                    "Registro com formato incompatível."
                )

            texto = json.dumps(
                linha,
                ensure_ascii=False,
                allow_nan=False,
                default=serializar,
            )
            registro = json.loads(texto)

            valido = (
                not (set(registro) - CAMPOS)
                and registro.get("data") == dia.isoformat()
                and bool(registro.get("equipamento_chave"))
                and registro.get("importado_em") == momento
                and registro.get("carga_id") == carga
            )

            if not valido:
                raise ErroExportacao(
                    f"Registro incompatível com a tabela em {dia}."
                )

            destino.write(texto + "\n")

    temporario.replace(arquivo)

    info = {
        "versao": 1,
        "data": dia.isoformat(),
        "conta_sha256": conta,
        "linhas": len(linhas),
        "equipamentos": len(payload),
        "importado_em": momento,
        "carga_id": carga,
        "sha256": checksum(arquivo),
    }
    salvar_json(marcador, info)

    return info


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--inicio",
        type=date.fromisoformat,
        default=date(2025, 1, 1),
    )
    parser.add_argument(
        "--fim",
        type=date.fromisoformat,
        default=date(2026, 10, 4),
    )

    args = parser.parse_args()

    if args.inicio > args.fim:
        parser.error(
            "A data inicial deve ser menor ou igual à final."
        )

    (PASTA / "diarios").mkdir(parents=True, exist_ok=True)

    quantidade = (args.fim - args.inicio).days + 1
    dias = [
        args.inicio + timedelta(days=i)
        for i in range(quantidade)
    ]

    email = input("Email REX: ").strip()

    if not email:
        raise ErroExportacao("Informe o email REX.")

    conta = hashlib.sha256(
        email.casefold().encode("utf-8")
    ).hexdigest()

    sessao = None

    for numero, dia in enumerate(dias, start=1):
        info = verificar_dia(dia, conta)

        if info is not None:
            print(
                f"[{numero}/{quantidade}] {dia}: já salvo, "
                f"{info['linhas']} registros.",
                flush=True,
            )
            continue

        if sessao is None:
            senha = getpass.getpass("Senha REX (oculta): ")

            sessao = {
                "email": email,
                "senha": senha,
                "cliente": RexClient(
                    email=email,
                    password=senha,
                ),
            }

        print(
            f"[{numero}/{quantidade}] Consultando {dia}...",
            flush=True,
        )

        info = exportar_dia(sessao, dia, conta)

        print(
            f"  {info['equipamentos']} equipamentos na resposta; "
            f"{info['linhas']} registros salvos.",
            flush=True,
        )

        time.sleep(0.5)

    nome = f"producao_diaria_{args.inicio}_{args.fim}.ndjson"
    consolidado = PASTA / nome
    temporario = Path(str(consolidado) + ".tmp")

    total = 0
    vazios = 0

    with temporario.open("wb") as destino:
        for dia in dias:
            info = verificar_dia(dia, conta)

            if info is None:
                raise ErroExportacao(
                    f"Falta concluir o dia {dia}."
                )

            arquivo = caminhos(dia)[0]

            with arquivo.open("rb") as origem:
                shutil.copyfileobj(origem, destino)

            total += info["linhas"]
            vazios += int(info["linhas"] == 0)

    temporario.replace(consolidado)
    tamanho = consolidado.stat().st_size

    salvar_json(
        PASTA / f"{nome}.resumo.json",
        {
            "inicio": args.inicio.isoformat(),
            "fim": args.fim.isoformat(),
            "dias_consultados": quantidade,
            "dias_sem_registros": vazios,
            "registros": total,
            "bytes": tamanho,
            "sha256": checksum(consolidado),
        },
    )

    print("\nEXPORTAÇÃO CONCLUÍDA")
    print(f"Dias consultados: {quantidade}")
    print(f"Dias sem registros: {vazios}")
    print(f"Registros: {total}")
    print(f"Tamanho: {tamanho / 1_000_000:.2f} MB")
    print(f"Arquivo: {consolidado}")


try:
    main()

except KeyboardInterrupt:
    print("\nInterrompido. Repita o comando para continuar.")
    raise SystemExit(130)

except Exception as erro:
    if isinstance(erro, RexHTTPError):
        detalhe = f"HTTP {erro.status_code}"
    elif isinstance(erro, ErroExportacao):
        detalhe = str(erro)
    else:
        detalhe = str(type(erro))

    print(f"\nEXPORTAÇÃO INTERROMPIDA: {detalhe}")
    print("Os dias concluídos foram preservados.")
    print("Envie o resultado para verificarmos a falha.")

    raise SystemExit(1)
    