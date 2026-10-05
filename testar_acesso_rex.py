"""Teste interativo de login REX. Nao grava senha, token ou dados em arquivo."""

import argparse
import getpass
import json
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path

from rex_api import RexAPIError, RexClient, RexConnectionError, RexHTTPError


def _testar(argv=None):
    parser = argparse.ArgumentParser(description="Testar acesso REX com senha oculta.")
    parser.add_argument("--producao", action="store_true", help="Verificar tambem a consulta de producao diaria.")
    parser.add_argument("--data", default=(date.today() - timedelta(days=1)).isoformat(), help="Data da consulta, AAAA-MM-DD; padrao ontem no computador.")
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        print("Abra o terminal integrado do VS Code e execute o script interativamente.")
        return 3
    try:
        if args.producao:
            RexClient._date(args.data)
        email = input("Email REX: ").strip()
        # Se nao houver mecanismo para ocultar a senha, interrompe em vez de ecoa-la.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            senha = getpass.getpass("Senha REX (oculta): ")
        client = RexClient(email=email, password=senha)
        client.login()
    except getpass.GetPassWarning:
        print("Este terminal nao consegue ocultar a senha. Use o terminal integrado do VS Code.")
        return 3
    except RexHTTPError as error:
        if error.status_code in (401, 403):
            print(f"LOGIN NAO ACEITO: HTTP {error.status_code}.")
            print("Confira email/senha e se a conta esta liberada para a API REX.")
            return 1
        print(f"TESTE INCONCLUSIVO: o login retornou HTTP {error.status_code}.")
        print("Essa resposta nao confirma credenciais invalidas. Confira a disponibilidade do servico.")
        return 3
    except RexConnectionError:
        print("TESTE INCONCLUSIVO: falha de rede/TLS ou timeout. Isso nao comprova senha invalida.")
        return 3
    except (RexAPIError, ValueError) as error:
        print(f"TESTE INCONCLUSIVO: {error}")
        return 3
    except (EOFError, KeyboardInterrupt):
        print("\nTeste cancelado.")
        return 3

    print("LOGIN ACEITO: a API REX emitiu um token para essas credenciais.")
    if not args.producao:
        print("Para verificar tambem o acesso aos dados, execute novamente com --producao.")
        return 0
    try:
        dados = client.daily_production(args.data, args.data)
    except RexHTTPError as error:
        if error.status_code in (401, 403):
            print(f"CONSULTA NAO ACEITA: HTTP {error.status_code}. O login anterior foi aceito.")
            print("Confirme com a REX a permissao para producao diaria e a validade da sessao.")
            return 2
        print(f"CONSULTA INCONCLUSIVA: HTTP {error.status_code}. O login anterior foi aceito.")
        print("Essa resposta nao comprova falta de permissao. Confira a disponibilidade do servico.")
        return 3
    except RexAPIError as error:
        print(f"CONSULTA INCONCLUSIVA: {error}. O login anterior foi aceito.")
        return 3
    except KeyboardInterrupt:
        print("\nConsulta cancelada. O login anterior foi aceito.")
        return 3
    print(f"CONSULTA ACEITA: producao diaria em {args.data}.")
    print(f"Equipamentos presentes na resposta: {len(dados)}.")
    if not dados:
        print("A consulta retornou um objeto vazio; confira o periodo e o acesso aos equipamentos da conta.")
    return 0


def main(argv=None):
    # Resumo opcional para acompanhar a execucao; nunca grava dados de login.
    registro = argparse.ArgumentParser(add_help=False)
    registro.add_argument("--resultado")
    opcoes, argumentos = registro.parse_known_args(argv)
    codigo = _testar(argumentos)
    if opcoes.resultado:
        try:
            Path(opcoes.resultado).write_text(
                json.dumps({
                    "origem": "testar_acesso_rex",
                    "codigo": codigo,
                    "teste_producao": "--producao" in argumentos,
                }) + "\n",
                encoding="utf-8",
            )
        except OSError:
            print("Nao foi possivel registrar o resumo. Confira as mensagens acima.")
    return codigo


if __name__ == "__main__":
    raise SystemExit(main())
