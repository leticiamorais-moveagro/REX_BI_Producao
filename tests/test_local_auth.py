"""Validate trusted Google endpoints and never fall back to plaintext secrets."""

import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from rex_local import auth


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.folder = Path(self.directory.name)
        self.path = self.folder / "cliente_google.json"
        self.client = {
            "client_id": "123-example.apps.googleusercontent.com",
            "client_secret": "fake-secret", "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"],
        }

    def write_client(self, client):
        self.path.write_text(json.dumps(client), encoding="utf-8")

    def test_official_desktop_client_and_rejection_of_web_or_redirect_endpoints(self):
        self.write_client({"installed": self.client})
        self.assertEqual(auth.read_oauth_client(self.path), self.client)
        for document in ({"web": self.client}, {"installed": {**self.client, "token_uri": "https://evil.invalid/token"}}, {"installed": {**self.client, "auth_uri": "https://evil.invalid/login"}}, {"installed": []}):
            with self.subTest(document_type=list(document)):
                self.write_client(document)
                with self.assertRaises(auth.AuthError) as caught:
                    auth.read_oauth_client(self.path)
                self.assertNotIn("fake-secret", str(caught.exception))
                self.assertNotIn("evil.invalid", str(caught.exception))

    def test_rex_environment_is_read_without_persisting(self):
        with patch.dict(auth.os.environ, {"REX_EMAIL": "fake@example.invalid", "REX_PASSWORD": "fake-password", "REX_TOKEN": ""}), patch.object(auth, "_vault") as vault, patch("rex_api.build_opener", return_value=object()):
            client = auth.rex_client(self.folder)
        self.assertEqual(client._email, "fake@example.invalid")
        self.assertEqual(client._password, "fake-password")
        vault.assert_not_called()
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_saved_rex_credentials_only_use_explicit_windows_vault(self):
        vault = Mock()
        vault.get_password.side_effect = ["fake@example.invalid", "fake-password"]
        with patch.dict(auth.os.environ, {"REX_EMAIL": "", "REX_PASSWORD": "", "REX_TOKEN": ""}), patch.object(auth, "_vault", return_value=vault), patch("rex_api.build_opener", return_value=object()):
            client = auth.rex_client(self.folder)
        self.assertEqual(client._password, "fake-password")
        self.assertEqual(vault.get_password.call_count, 2)
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_noninteractive_execution_without_credentials_fails_without_prompt(self):
        with patch.dict(auth.os.environ, {"REX_EMAIL": "", "REX_PASSWORD": "", "REX_TOKEN": ""}), patch.object(auth, "_vault", side_effect=auth.AuthError("Cofre indisponivel.")), patch.object(auth.sys, "stdin", io.StringIO()), patch("builtins.input") as prompt:
            with self.assertRaises(auth.AuthError):
                auth.rex_client(self.folder)
            prompt.assert_not_called()

    def test_secret_never_accepts_getpass_echo_fallback(self):
        tty = Mock()
        tty.isatty.return_value = True
        with patch.object(auth.sys, "stdin", tty), patch.object(auth.getpass, "getpass", side_effect=auth.getpass.GetPassWarning):
            with self.assertRaises(auth.AuthError):
                auth._secret("Senha: ")


if __name__ == "__main__":
    unittest.main()
