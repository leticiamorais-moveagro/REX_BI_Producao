"""Interactive login and Windows-only protected credential persistence."""

from __future__ import annotations

import getpass
import hashlib
import json
import os
from pathlib import Path
import sys
import warnings

from rex_api import RexClient


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class AuthError(ValueError):
    pass


def _vault():
    if os.name != "nt":
        raise AuthError("Credenciais salvas exigem o Gerenciador de Credenciais do Windows.")
    try:
        from keyring.backends.Windows import WinVaultKeyring
        vault = WinVaultKeyring()
        if vault.priority <= 0:
            raise RuntimeError
        vault.persist = "local machine"
        return vault
    except Exception:
        raise AuthError("Nao foi possivel abrir o cofre do Windows. Execute instalar_rex.cmd.") from None


def _service(folder: Path, label: str) -> str:
    key = hashlib.sha256(str(folder.resolve()).casefold().encode("utf-8")).hexdigest()[:24]
    return f"REXIntegracao:{key}:{label}"


def _secret(prompt: str) -> str:
    if not sys.stdin.isatty():
        raise AuthError("Execute em um terminal interativo para digitar a senha.")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            value = getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise AuthError("O terminal nao permite ocultar a senha. Use o menu iniciar_rex.cmd.") from None
    if not value:
        raise AuthError("A senha nao pode ficar vazia.")
    return value


def save_rex_credentials(folder: Path) -> None:
    if not sys.stdin.isatty():
        raise AuthError("Abra iniciar_rex.cmd para configurar as credenciais.")
    vault = _vault()
    email = input("Email da conta REX: ").strip()
    if not email:
        raise AuthError("Informe o email REX.")
    password = _secret("Senha REX (oculta): ")
    try:
        # Different service names prevent keyring's collision/roaming fallback.
        vault.set_password(_service(folder, "rex_email"), "conta", email)
        vault.set_password(_service(folder, "rex_password"), "conta", password)
    except Exception:
        raise AuthError("Falha ao salvar no cofre do Windows. Nenhuma senha foi gravada em arquivo.") from None


def rex_client(folder: Path) -> RexClient:
    token = os.environ.get("REX_TOKEN") or None
    email = os.environ.get("REX_EMAIL") or None
    password = os.environ.get("REX_PASSWORD") or None
    if token:
        return RexClient(token=token, email=email, password=password, timeout=60)
    if not (email and password):
        try:
            vault = _vault()
            email = email or vault.get_password(_service(folder, "rex_email"), "conta")
            password = password or vault.get_password(_service(folder, "rex_password"), "conta")
        except AuthError:
            if not sys.stdin.isatty():
                raise
        except Exception:
            raise AuthError("Nao foi possivel ler as credenciais REX do cofre do Windows.") from None
    if not (email and password):
        if not sys.stdin.isatty():
            raise AuthError("Configure as credenciais REX no menu antes da execucao agendada.")
        email = email or input("Email da conta REX: ").strip()
        password = password or _secret("Senha REX (oculta): ")
    if not email:
        raise AuthError("Informe o email REX.")
    return RexClient(email=email, password=password, timeout=60)


def read_oauth_client(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
        client = document["installed"]
        valid = (
            isinstance(client, dict)
            and isinstance(client.get("client_id"), str)
            and client["client_id"].endswith(".apps.googleusercontent.com")
            and isinstance(client.get("client_secret"), str)
            and bool(client["client_secret"])
            and client.get("auth_uri") in {
                "https://accounts.google.com/o/oauth2/auth",
                "https://accounts.google.com/o/oauth2/v2/auth",
            }
            and client.get("token_uri") == "https://oauth2.googleapis.com/token"
        )
        if not valid:
            raise ValueError
        return client
    except (OSError, ValueError, KeyError, TypeError):
        raise AuthError("Selecione um JSON OAuth oficial do Google, tipo Aplicativo para computador.") from None


def google_service(folder: Path, *, interactive: bool):
    client_path = folder / "cliente_google.json"
    client = read_oauth_client(client_path)
    vault = _vault()
    label = "google_refresh_" + hashlib.sha256(client["client_id"].encode()).hexdigest()[:16]
    service_name = _service(folder, label)
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from google_auth_httplib2 import AuthorizedHttp
        import httplib2
    except ImportError:
        raise AuthError("Instale as dependencias executando instalar_rex.cmd.") from None
    try:
        refresh_token = vault.get_password(service_name, "conta")
        if refresh_token:
            credentials = Credentials(
                token=None, refresh_token=refresh_token, token_uri=client["token_uri"],
                client_id=client["client_id"], client_secret=client["client_secret"], scopes=SCOPES,
            )
            credentials.refresh(Request())
        else:
            if not interactive:
                raise AuthError("Autorize o Google pelo comando publicar no menu antes de agendar.")
            print("O navegador abrira para autorizar sua conta Google corporativa.")
            flow = InstalledAppFlow.from_client_config({"installed": client}, SCOPES)
            credentials = flow.run_local_server(
                host="localhost", port=0, open_browser=True, timeout_seconds=180,
                authorization_prompt_message="Autorize no navegador; nao compartilhe o link de autorizacao.",
                success_message="Conta autorizada. Pode fechar esta aba e voltar ao terminal REX.",
            )
            if not credentials.refresh_token:
                raise AuthError("Google nao retornou autorizacao persistente. Confira o aplicativo OAuth.")
            vault.set_password(service_name, "conta", credentials.refresh_token)
        # Access tokens remain in memory; only the small refresh token uses WinVault.
        return build(
            "sheets", "v4", http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=120)),
            cache_discovery=False,
        )
    except AuthError:
        raise
    except Exception:
        raise AuthError(
            "Falha na autorizacao Google. Confira rede, aplicativo OAuth e permissoes da empresa. "
            "Se a autorizacao foi revogada, remova a entrada Google REXIntegracao no cofre do Windows "
            "e autorize novamente pelo menu."
        ) from None
