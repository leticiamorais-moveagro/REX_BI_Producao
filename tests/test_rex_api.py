"""Testes locais do cliente REX; nenhuma chamada HTTP real é efetuada."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import rex_api

from rex_api import (
    RexClient,
    RexConnectionError,
    RexHTTPError,
    RexImportError,
    RexProtocolError,
)


class FakeResponse(io.BytesIO):
    def __init__(self, payload, status=200):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        super().__init__(raw)
        self.status = status

    def getcode(self):
        return self.status


class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        if not self.responses:
            raise AssertionError("Chamada HTTP inesperada ou repetida")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class RexClientTests(unittest.TestCase):
    def test_login_uses_documented_host_and_body_without_bearer(self):
        opener = FakeOpener(FakeResponse({"token": "  fresh-token  "}))
        client = RexClient(
            email="person@example.com", password="senha-ç", token="old-token", timeout=12, opener=opener
        )
        self.assertEqual(client.login(), "fresh-token")
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://open-api.rexagri.com/api/v1/oauth/token")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"email": "person@example.com", "password": "senha-ç"})
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(timeout, 12)

    def test_lazy_login_reuses_token_across_both_hosts(self):
        production = {"EQ01": {"value": 12}}
        opener = FakeOpener(FakeResponse({"token": "example-token"}), FakeResponse(production), FakeResponse([]))
        client = RexClient(email="person@example.com", password="example-secret", opener=opener)
        self.assertEqual(opener.calls, [])
        self.assertEqual(client.daily_production("2026-10-01", "2026-10-02"), production)
        self.assertEqual(client.list_entities("equipment"), [])
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(urlsplit(opener.calls[1][0].full_url).netloc, "open-api.rexagri.com")
        self.assertEqual(urlsplit(opener.calls[2][0].full_url).netloc, "world.rexagri.com")
        for request, _ in opener.calls[1:]:
            self.assertEqual(request.get_header("Authorization"), "Bearer example-token")

    def test_login_requires_a_nonempty_string_token_in_response(self):
        for response in ({}, {"token": None}, {"token": 123}, {"token": ""}, {"token": "  "}, []):
            with self.subTest(response=response):
                opener = FakeOpener(FakeResponse(response))
                client = RexClient(email="person@example.com", password="example-secret", opener=opener)
                with self.assertRaises(RexProtocolError):
                    client.login()
                self.assertEqual(len(opener.calls), 1)

    def test_daily_production_encodes_equipment_as_one_query_value(self):
        opener = FakeOpener(FakeResponse({}))
        client = RexClient(token="example-token", opener=opener)
        client.daily_production("2024-02-29", "2024-03-01", equipment_code="São Paulo / EQ&+?")
        request, _ = opener.calls[0]
        parsed = urlsplit(request.full_url)
        self.assertEqual(parsed.path, "/api/v1/reports/daily-production")
        self.assertEqual(
            parse_qs(parsed.query),
            {"begin": ["2024-02-29"], "end": ["2024-03-01"], "equipment-code": ["São Paulo / EQ&+?"]},
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)

    def test_documented_entity_paths_include_v2(self):
        entities = (
            "cost-center", "operation", "operator", "equipment", "implement",
            "product", "recipe", "task-order", "task-order/v2",
        )
        opener = FakeOpener(*(FakeResponse([]) for _ in entities))
        client = RexClient(token="example-token", opener=opener)
        for entity in entities:
            self.assertEqual(client.list_entities(entity), [])
        self.assertEqual(
            [request.full_url for request, _ in opener.calls],
            ["https://world.rexagri.com/v1/import/" + entity for entity in entities],
        )

    def test_supported_filters_preserve_boolean_and_timezone(self):
        opener = FakeOpener(FakeResponse([]), FakeResponse([]), FakeResponse([]))
        client = RexClient(token="example-token", opener=opener)
        client.list_entities("product", disabled=True)
        client.list_entities("recipe", disabled=False, since="2026-10-01T12:30:00+03:00")
        client.list_entities("task-order/v2", status="andamento", since="2026-10-01T00:00:00Z")
        self.assertEqual(parse_qs(urlsplit(opener.calls[0][0].full_url).query), {"disabled": ["true"]})
        self.assertEqual(
            parse_qs(urlsplit(opener.calls[1][0].full_url).query),
            {"disabled": ["false"], "since": ["2026-10-01T12:30:00+03:00"]},
        )
        self.assertEqual(
            parse_qs(urlsplit(opener.calls[2][0].full_url).query),
            {"status": ["andamento"], "since": ["2026-10-01T00:00:00Z"]},
        )

    def test_create_sends_utf8_batch_and_preserves_warnings(self):
        result = {"success": ["OP01"], "fail": [], "warning": [{"message": "Revisar cadastro"}]}
        opener = FakeOpener(FakeResponse(result, status=201))
        client = RexClient(token="example-token", opener=opener)
        objects = [{"code": "OP01", "name": "João da Silva"}]
        self.assertEqual(client.create_entities("operator", objects), result)
        request, _ = opener.calls[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data.decode("utf-8")), objects)
        self.assertIn("João".encode("utf-8"), request.data)
        self.assertEqual(request.get_header("Content-type"), "application/json")

    def test_update_sends_only_supplied_attributes(self):
        opener = FakeOpener(FakeResponse({"success": ["OP01"], "fail": []}))
        client = RexClient(token="example-token", opener=opener)
        client.update_entities("operator", [{"code": "OP01", "name": "Novo nome"}])
        request, _ = opener.calls[0]
        self.assertEqual(request.get_method(), "PUT")
        self.assertEqual(json.loads(request.data), [{"code": "OP01", "name": "Novo nome"}])

    def test_partial_import_failure_keeps_successes_and_warning(self):
        result = {
            "success": ["OP01"],
            "fail": [{"code": "OP02", "message": "Cadastro inválido"}],
            "warning": [{"code": "OP01", "message": "Verificar dados"}],
        }
        opener = FakeOpener(FakeResponse(result))
        client = RexClient(token="example-token", opener=opener)
        with self.assertRaises(RexImportError) as raised:
            client.create_entities("operator", [{"code": "OP01"}, {"code": "OP02"}])
        self.assertEqual(raised.exception.result, result)
        self.assertEqual(len(opener.calls), 1)

    def test_http_error_redacts_body_and_does_not_retry(self):
        token, email, password = "private-token", "private@example.com", "private-password"
        url = "https://world.rexagri.com/v1/import/operator"
        error_body = json.dumps({"token": token, "email": email, "password": password}).encode()
        opener = FakeOpener(HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(error_body)))
        client = RexClient(email=email, password=password, token=token, opener=opener)
        with self.assertRaises(RexHTTPError) as raised:
            client.list_entities("operator")
        self.assertEqual(raised.exception.status_code, 401)
        for secret in (token, email, password):
            self.assertNotIn(secret, str(raised.exception))
        self.assertTrue(raised.exception.__suppress_context__)
        self.assertEqual(len(opener.calls), 1)
        # A próxima chamada deve pedir um novo token, sem repetir a chamada anterior.
        opener.responses.extend([FakeResponse({"token": "replacement"}), FakeResponse([])])
        client.list_entities("operator")
        self.assertEqual(opener.calls[1][0].full_url, "https://open-api.rexagri.com/api/v1/oauth/token")
        self.assertEqual(opener.calls[2][0].get_header("Authorization"), "Bearer replacement")

    def test_401_response_object_clears_token_without_automatic_retry(self):
        opener = FakeOpener(
            FakeResponse({}, status=401), FakeResponse({"token": "replacement"}), FakeResponse([])
        )
        client = RexClient(email="person@example.com", password="example-secret", token="expired", opener=opener)
        with self.assertRaises(RexHTTPError) as raised:
            client.list_entities("operator")
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(len(opener.calls), 1)
        client.list_entities("operator")
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(opener.calls[1][0].full_url, "https://open-api.rexagri.com/api/v1/oauth/token")
        self.assertIsNone(opener.calls[1][0].get_header("Authorization"))
        self.assertEqual(opener.calls[2][0].get_header("Authorization"), "Bearer replacement")

    def test_invalid_header_tokens_are_rejected_without_disclosing_value(self):
        tokens = ["private secret", "private\r\nsecret", "private\tsecret", "private\x00secret", "private\x7fsecret", "privateésecret"]
        for token in tokens:
            with self.subTest(token_kind=repr(token[7:8])):
                opener = FakeOpener()
                with self.assertRaises(ValueError) as raised:
                    RexClient(token=token, opener=opener)
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("secret", str(raised.exception))
                self.assertEqual(opener.calls, [])

    def test_login_rejects_invalid_header_token_without_disclosing_value(self):
        for token in ("private secret", "private\r\nsecret", "privateésecret"):
            with self.subTest(token_kind=repr(token[7:8])):
                opener = FakeOpener(FakeResponse({"token": token}))
                client = RexClient(email="person@example.com", password="example-secret", opener=opener)
                with self.assertRaises(RexProtocolError) as raised:
                    client.login()
                self.assertNotIn("private", str(raised.exception))
                self.assertNotIn("secret", str(raised.exception))
                self.assertTrue(raised.exception.__suppress_context__)
                self.assertEqual(len(opener.calls), 1)

    def test_visible_ascii_token_can_be_used_after_trimming_outer_whitespace(self):
        opener = FakeOpener(FakeResponse([]))
        client = RexClient(token="  abc.DEF_01-+/=  ", opener=opener)
        client.list_entities("operator")
        self.assertEqual(opener.calls[0][0].get_header("Authorization"), "Bearer abc.DEF_01-+/=")

    def test_from_env_uses_token_without_login_even_when_credentials_exist(self):
        environment = {"REX_TOKEN": "environment-token", "REX_EMAIL": "person@example.com", "REX_PASSWORD": "example-secret"}
        with patch.dict(rex_api.os.environ, environment, clear=True):
            client = RexClient.from_env()
        opener = FakeOpener(FakeResponse([]))
        client._opener = opener
        self.assertEqual(client.list_entities("operator"), [])
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0][0].get_header("Authorization"), "Bearer environment-token")

    def test_from_env_uses_credentials_for_lazy_login(self):
        environment = {"REX_EMAIL": "person@example.com", "REX_PASSWORD": "example-secret"}
        with patch.dict(rex_api.os.environ, environment, clear=True):
            client = RexClient.from_env()
        opener = FakeOpener(FakeResponse({"token": "environment-token"}), FakeResponse([]))
        client._opener = opener
        client.list_entities("operator")
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(json.loads(opener.calls[0][0].data), {"email": "person@example.com", "password": "example-secret"})

    def test_from_env_rejects_missing_or_incomplete_credentials(self):
        for environment in ({}, {"REX_EMAIL": "private@example.com"}, {"REX_PASSWORD": "private-password"}):
            with self.subTest(fields=tuple(environment)):
                with patch.dict(rex_api.os.environ, environment, clear=True):
                    with self.assertRaises(ValueError) as raised:
                        RexClient.from_env()
                self.assertNotIn("private", str(raised.exception))

    def test_default_opener_does_not_forward_bearer_on_redirect(self):
        client = RexClient(token="private-token")
        handler = next(handler for handler in client._opener.handlers if isinstance(handler, rex_api._NoRedirect))
        request = Request(
            "https://world.rexagri.com/v1/import/operator", headers={"Authorization": "Bearer private-token"}
        )
        with patch.object(client._opener, "open") as forwarded:
            result = handler.http_error_302(
                request, FakeResponse(b""), 302, "Found", {"location": "https://different.invalid/receive"}
            )
        self.assertIsNone(result)
        forwarded.assert_not_called()

    def test_cli_write_failure_keeps_completed_import_result_and_does_not_repeat(self):
        objects = [{"code": "OP01", "name": "João"}]
        result = {"success": ["OP01"], "fail": [], "warning": [{"message": "Verificar João"}]}
        client = Mock(spec=RexClient)
        client.create_entities.return_value = result
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("rex_api._cli_client", return_value=client), \
                patch("rex_api.Path.read_text", return_value=json.dumps(objects)), \
                patch("rex_api.Path.write_text", side_effect=PermissionError("local file denied")) as writing, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = rex_api.main(["criar", "operator", "--arquivo", "batch.json", "--saida", "result.json"])
        self.assertEqual(status, 1)
        client.create_entities.assert_called_once_with("operator", objects)
        writing.assert_called_once()
        self.assertEqual(stdout.getvalue(), "")
        notice, saved_result = stderr.getvalue().split("\n", 1)
        self.assertIn("A API concluiu a chamada", notice)
        self.assertIn("antes de repetir", notice)
        self.assertEqual(json.loads(saved_result), result)

    def test_cli_partial_import_output_failure_keeps_full_result_and_status_two(self):
        objects = [{"code": "OP01"}, {"code": "OP02"}]
        result = {"success": ["OP01"], "fail": [{"code": "OP02", "message": "Inválido"}], "warning": []}
        client = Mock(spec=RexClient)
        client.create_entities.side_effect = RexImportError(result)
        stderr = io.StringIO()
        with patch("rex_api._cli_client", return_value=client), \
                patch("rex_api.Path.read_text", return_value=json.dumps(objects)), \
                patch("rex_api.Path.write_text", side_effect=PermissionError("local file denied")), \
                redirect_stderr(stderr):
            status = rex_api.main(["criar", "operator", "--arquivo", "batch.json", "--saida", "result.json"])
        self.assertEqual(status, 2)
        client.create_entities.assert_called_once_with("operator", objects)
        lines = stderr.getvalue().split("\n", 2)
        self.assertIn("1 falha(s)", lines[0])
        self.assertIn("Nao foi possivel salvar", lines[1])
        self.assertEqual(json.loads(lines[2]), result)

    def test_connection_failures_are_reported_once_without_secret_details(self):
        errors = [URLError("private-password"), TimeoutError("private-password"), OSError("private-password")]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                opener = FakeOpener(error)
                client = RexClient(token="private-token", timeout=4, opener=opener)
                with self.assertRaises(RexConnectionError) as raised:
                    client.list_entities("operator")
                self.assertNotIn("private-password", str(raised.exception))
                self.assertNotIn("private-token", str(raised.exception))
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(opener.calls[0][1], 4)

    def test_invalid_json_is_a_protocol_error_without_response_body(self):
        for raw in (b"not-json private-token", b"\xff\xfe"):
            with self.subTest(raw=raw):
                opener = FakeOpener(FakeResponse(raw))
                client = RexClient(token="private-token", opener=opener)
                with self.assertRaises(RexProtocolError) as raised:
                    client.list_entities("operator")
                self.assertNotIn("private-token", str(raised.exception))

    def test_utf8_bom_json_response_is_accepted(self):
        opener = FakeOpener(FakeResponse(b"\xef\xbb\xbf[]"))
        client = RexClient(token="example-token", opener=opener)
        self.assertEqual(client.list_entities("operator"), [])

    def test_response_shapes_are_validated(self):
        cases = [
            ("daily", []),
            ("list", {}),
            ("create", {"success": [], "fail": "bad"}),
            ("create", {"success": [], "fail": [], "warning": "bad"}),
        ]
        for method, response in cases:
            with self.subTest(method=method, response=response):
                client = RexClient(token="example-token", opener=FakeOpener(FakeResponse(response)))
                with self.assertRaises(RexProtocolError):
                    if method == "daily":
                        client.daily_production("2026-10-01", "2026-10-02")
                    elif method == "list":
                        client.list_entities("operator")
                    else:
                        client.create_entities("operator", [{"code": "OP01"}])

    def test_invalid_dates_and_reversed_range_fail_before_login_or_network(self):
        cases = [
            ("2026-1-02", "2026-01-03"),
            ("2026-02-29", "2026-03-01"),
            ("02/10/2026", "2026-10-03"),
            ("2026-10-03", "2026-10-02"),
            ("", "2026-10-02"),
        ]
        for begin, end in cases:
            with self.subTest(begin=begin, end=end):
                opener = FakeOpener()
                client = RexClient(email="person@example.com", password="example-secret", opener=opener)
                with self.assertRaises((TypeError, ValueError)):
                    client.daily_production(begin, end)
                self.assertEqual(opener.calls, [])

    def test_invalid_batches_fail_before_login_or_network(self):
        cases = [[], {}, ["invalid"], [{}], [{"code": ""}], [{"code": "   "}], [{"code": 123}]]
        for method in ("create_entities", "update_entities"):
            for objects in cases:
                with self.subTest(method=method, objects=objects):
                    opener = FakeOpener()
                    client = RexClient(email="person@example.com", password="example-secret", opener=opener)
                    with self.assertRaises((TypeError, ValueError)):
                        getattr(client, method)("operator", objects)
                    self.assertEqual(opener.calls, [])

    def test_update_requires_an_attribute_besides_code(self):
        opener = FakeOpener()
        client = RexClient(token="example-token", opener=opener)
        with self.assertRaises((TypeError, ValueError)):
            client.update_entities("operator", [{"code": "OP01"}])
        self.assertEqual(opener.calls, [])

    def test_unknown_entity_is_rejected_before_network(self):
        opener = FakeOpener()
        client = RexClient(token="example-token", opener=opener)
        with self.assertRaises((TypeError, ValueError)):
            client.list_entities("../../login")
        self.assertEqual(opener.calls, [])

    def test_invalid_filters_are_rejected_before_network(self):
        cases = [
            ("product", {"disabled": "false"}),
            ("product", {"since": "2026-10-01"}),
            ("recipe", {"since": ""}),
            ("task-order", {"status": "aberto"}),
            ("task-order/v2", {"status": "unknown"}),
        ]
        for entity, filters in cases:
            with self.subTest(entity=entity, filters=filters):
                opener = FakeOpener()
                client = RexClient(email="person@example.com", password="example-secret", opener=opener)
                with self.assertRaises((TypeError, ValueError)):
                    client.list_entities(entity, **filters)
                self.assertEqual(opener.calls, [])

    def test_non_json_and_nonfinite_batch_values_fail_before_login(self):
        for value in (float("nan"), object()):
            with self.subTest(value=type(value).__name__):
                opener = FakeOpener()
                client = RexClient(email="person@example.com", password="example-secret", opener=opener)
                with self.assertRaises((TypeError, ValueError)):
                    client.create_entities("operator", [{"code": "OP01", "name": value}])
                self.assertEqual(opener.calls, [])

    def test_invalid_timeouts_are_rejected(self):
        for timeout in (0, -1, True, float("inf"), float("nan"), "30"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    RexClient(token="example-token", timeout=timeout, opener=FakeOpener())


if __name__ == "__main__":
    unittest.main()
