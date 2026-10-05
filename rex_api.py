"""Cliente da API REX, conforme Servicos da Plataforma REX, revisao 2.0.

Python 3.10+, sem dependencias externas. Veja README.md para exemplos.
Snapshots e boletins de campo sao recebidos por webhook, fora deste cliente.
"""

from __future__ import annotations

import argparse
import getpass
import json
import math
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RexAPIError(Exception):
    """Erro de comunicacao ou de contrato com o REX."""


class RexHTTPError(RexAPIError):
    def __init__(self, status_code: int, method: str, path: str):
        self.status_code = status_code
        hint = " Autentique novamente e confira as permissoes." if status_code in (401, 403) else ""
        super().__init__(f"REX retornou HTTP {status_code} em {method} {path}.{hint}")


class RexConnectionError(RexAPIError):
    """Falha de rede, TLS ou timeout. A chamada nao e repetida automaticamente."""


class RexProtocolError(RexAPIError):
    """A resposta recebida nao segue o formato documentado."""


class RexImportError(RexAPIError):
    def __init__(self, result: dict[str, Any]):
        self.result = result
        super().__init__(
            f"Importacao com {len(result['fail'])} falha(s) e "
            f"{len(result['success'])} sucesso(s). Consulte a propriedade result."
        )


class _NoRedirect(HTTPRedirectHandler):
    # Evita encaminhar credenciais ou Bearer para um destino redirecionado.
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RexClient:
    """Autenticacao, producao diaria e cadastros da plataforma REX.

    Reutiliza o token em memoria. Nao presume validade ou mecanismo de refresh,
    pois o PDF nao informa esses detalhes. Em HTTP 401, chame login() de novo.
    """

    API_URL = "https://open-api.rexagri.com"
    IMPORT_URL = "https://world.rexagri.com"
    ENTITIES = (
        "cost-center", "operation", "operator", "equipment", "implement",
        "product", "recipe", "task-order", "task-order/v2",
    )
    FILTERS = {
        "product": frozenset({"disabled"}),
        "recipe": frozenset({"disabled", "since"}),
        "task-order/v2": frozenset({"status", "since"}),
    }

    def __init__(
        self, email: str | None = None, password: str | None = None,
        token: str | None = None, timeout: float = 30, opener=None,
    ):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout deve ser um numero positivo e finito.")
        self._email = email
        self._password = password
        self._token = self._validate_token(token) if token is not None else None
        self.timeout = timeout
        self._opener = opener if opener is not None else build_opener(_NoRedirect())

    @staticmethod
    def _validate_token(token: str) -> str:
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token deve ser uma string nao vazia.")
        value = token.strip()
        if any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise ValueError("token deve conter apenas caracteres ASCII visiveis, sem espacos ou quebras de linha.")
        return value

    @classmethod
    def from_env(cls) -> RexClient:
        """Le REX_TOKEN ou REX_EMAIL/REX_PASSWORD; nao carrega arquivos .env."""
        token = os.environ.get("REX_TOKEN") or None
        email = os.environ.get("REX_EMAIL") or None
        password = os.environ.get("REX_PASSWORD") or None
        if not token and not (email and password):
            raise ValueError("Defina REX_EMAIL e REX_PASSWORD, ou REX_TOKEN.")
        return cls(email=email, password=password, token=token)

    def login(self) -> str:
        """Obtem um novo token usando email/senha e o armazena em memoria."""
        if not isinstance(self._email, str) or not self._email.strip() or not isinstance(self._password, str) or not self._password:
            raise ValueError("O login exige email e senha. Confira as credenciais configuradas.")
        self._token = None
        result = self._request(
            "POST", self.API_URL, "/api/v1/oauth/token",
            payload={"email": self._email, "password": self._password}, authenticated=False,
        )
        if not isinstance(result, dict) or not isinstance(result.get("token"), str) or not result["token"].strip():
            raise RexProtocolError("Login retornou uma resposta sem token valido.")
        try:
            self._token = self._validate_token(result["token"])
        except ValueError:
            raise RexProtocolError("Login retornou um token invalido para o header HTTP.") from None
        return self._token

    def _request(
        self, method: str, base_url: str, path: str,
        *, payload=None, params=None, authenticated: bool = True,
    ):
        # Serializa antes do login para rejeitar um payload invalido sem rede.
        data = None if payload is None else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if authenticated and self._token is None:
            self.login()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        query = "?" + urlencode(params) if params else ""
        request = Request(base_url + path + query, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = response.getcode()
                if not 200 <= status < 300:
                    if status == 401:
                        self._token = None
                    raise RexHTTPError(status, method, path)
                body = response.read()
        except HTTPError as error:
            error.close()
            if error.code == 401:
                self._token = None
            # Nao inclui corpo, headers ou credenciais na mensagem de erro.
            raise RexHTTPError(error.code, method, path) from None
        except (URLError, TimeoutError, OSError):
            raise RexConnectionError(
                f"Falha de conexao em {method} {path}. Confira rede/TLS e timeout. "
                "A chamada nao foi repetida."
            ) from None
        try:
            return json.loads(body.decode("utf-8-sig"))
        except (ValueError, UnicodeError):
            raise RexProtocolError(f"Resposta JSON invalida em {method} {path}.") from None

    @staticmethod
    def _date(value: str) -> date:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Use datas no formato AAAA-MM-DD.")
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ValueError("Data de calendario invalida.") from None

    def daily_production(self, begin: str, end: str, equipment_code: str | None = None) -> dict:
        """Consulta o consolidado do periodo; mantem tipos/valores do REX."""
        if self._date(begin) > self._date(end):
            raise ValueError("A data inicial deve ser menor ou igual a final.")
        params = {"begin": begin, "end": end}
        if equipment_code is not None:
            if not isinstance(equipment_code, str) or not equipment_code.strip():
                raise ValueError("equipment_code deve ser uma string nao vazia.")
            params["equipment-code"] = equipment_code
        result = self._request("GET", self.API_URL, "/api/v1/reports/daily-production", params=params)
        if not isinstance(result, dict):
            raise RexProtocolError("Producao diaria deve retornar um objeto por equipamento.")
        return result

    def _entity_path(self, entity: str) -> str:
        if entity not in self.ENTITIES:
            raise ValueError("Entidade desconhecida. Opcoes: " + ", ".join(self.ENTITIES))
        return "/v1/import/" + entity

    def list_entities(self, entity: str, **filters) -> list:
        """Consulta cadastros. Filtros aceitos dependem da entidade."""
        path = self._entity_path(entity)
        params = {key: value for key, value in filters.items() if value is not None}
        unsupported = set(params) - self.FILTERS.get(entity, frozenset())
        if unsupported:
            raise ValueError("Filtro nao documentado para esta entidade: " + ", ".join(sorted(unsupported)))
        if "disabled" in params:
            if not isinstance(params["disabled"], bool):
                raise ValueError("disabled deve ser True ou False.")
            params["disabled"] = str(params["disabled"]).lower()
        if "status" in params and params["status"] not in {"aberto", "andamento", "suspenso", "encerrado"}:
            raise ValueError("Status invalido de ordem de servico.")
        if "since" in params and (not isinstance(params["since"], str) or not params["since"].strip()):
            raise ValueError("since deve ser uma string ISO-8601 nao vazia.")
        result = self._request("GET", self.IMPORT_URL, path, params=params)
        if not isinstance(result, list):
            raise RexProtocolError("A consulta de cadastros deve retornar uma lista.")
        return result

    def _import_entities(self, method: str, entity: str, objects: list[dict]) -> dict:
        path = self._entity_path(entity)
        if not isinstance(objects, list) or not objects:
            raise ValueError("Envie uma lista nao vazia de objetos.")
        for index, obj in enumerate(objects):
            if not isinstance(obj, dict) or not isinstance(obj.get("code"), str) or not obj["code"].strip():
                raise ValueError(f"Objeto {index}: code deve ser uma string nao vazia.")
            if method == "PUT" and len(obj) < 2:
                raise ValueError(f"Objeto {index}: envie code e ao menos um atributo para atualizar.")
        result = self._request(method, self.IMPORT_URL, path, payload=objects)
        if not isinstance(result, dict) or not isinstance(result.get("success"), list) or not isinstance(result.get("fail"), list):
            raise RexProtocolError("Importacao deve retornar listas success e fail.")
        if "warning" in result and not isinstance(result["warning"], list):
            raise RexProtocolError("warning deve ser uma lista quando presente.")
        if result["fail"]:
            raise RexImportError(result)
        return result

    def create_entities(self, entity: str, objects: list[dict]) -> dict:
        """POST em lote. Confira os campos da entidade no PDF antes de enviar."""
        return self._import_entities("POST", entity, objects)

    def update_entities(self, entity: str, objects: list[dict]) -> dict:
        """PUT em lote. Campos omitidos sao preservados pelo REX."""
        return self._import_entities("PUT", entity, objects)


def _cli_client() -> RexClient:
    token = os.environ.get("REX_TOKEN") or None
    email = os.environ.get("REX_EMAIL") or None
    password = os.environ.get("REX_PASSWORD") or None
    if not token and not (email and password):
        if not sys.stdin.isatty():
            raise ValueError("Defina REX_EMAIL e REX_PASSWORD, ou REX_TOKEN, para executar sem terminal interativo.")
        email = email or input("Email REX: ").strip()
        password = password or getpass.getpass("Senha REX (oculta): ")
    return RexClient(email=email, password=password, token=token)


def _write_result(result, output: str | None) -> None:
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(content + "\n", encoding="utf-8")
        print(f"Resultado salvo em {output}")
    else:
        print(content)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Conexao Python com a API REX (revisao 2.0).")
    commands = parser.add_subparsers(dest="command", required=True)
    production = commands.add_parser("producao", help="Consultar producao diaria.")
    production.add_argument("--inicio", required=True, help="AAAA-MM-DD")
    production.add_argument("--fim", required=True, help="AAAA-MM-DD")
    production.add_argument("--equipamento")
    production.add_argument("--saida", help="Arquivo JSON de destino.")
    listing = commands.add_parser("listar", help="Consultar cadastros.")
    listing.add_argument("entidade", choices=RexClient.ENTITIES)
    listing.add_argument("--disabled", choices=("true", "false"))
    listing.add_argument("--status", choices=("aberto", "andamento", "suspenso", "encerrado"))
    listing.add_argument("--since", help="ISO-8601; recipe e task-order/v2.")
    listing.add_argument("--saida")
    for command, help_text in (("criar", "Inserir cadastros via POST."), ("atualizar", "Alterar cadastros via PUT.")):
        write = commands.add_parser(command, help=help_text)
        write.add_argument("entidade", choices=RexClient.ENTITIES)
        write.add_argument("--arquivo", required=True, help="Arquivo JSON com uma lista de objetos.")
        write.add_argument("--saida")
    args = parser.parse_args(argv)
    try:
        # Le o arquivo de lote antes de solicitar credenciais.
        objects = None
        if args.command in ("criar", "atualizar"):
            objects = json.loads(Path(args.arquivo).read_text(encoding="utf-8-sig"))
        client = _cli_client()
        if args.command == "producao":
            result = client.daily_production(args.inicio, args.fim, args.equipamento)
        elif args.command == "listar":
            result = client.list_entities(
                args.entidade, disabled=None if args.disabled is None else args.disabled == "true",
                status=args.status, since=args.since,
            )
        elif args.command == "criar":
            result = client.create_entities(args.entidade, objects)
        else:
            result = client.update_entities(args.entidade, objects)
        try:
            _write_result(result, args.saida)
        except OSError:
            print(
                "A API concluiu a chamada, mas o resultado nao foi salvo no arquivo local. "
                "O retorno segue abaixo; confira-o antes de repetir uma escrita.",
                file=sys.stderr,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
            return 1
        return 0
    except RexImportError as error:
        # O lote pode ter sucessos mesmo com HTTP 2xx e registros em fail.
        print(str(error), file=sys.stderr)
        try:
            _write_result(error.result, args.saida)
        except OSError:
            print("Nao foi possivel salvar o retorno; ele segue abaixo:", file=sys.stderr)
            print(json.dumps(error.result, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except (RexAPIError, ValueError, OSError, EOFError) as error:
        print(f"Erro: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
