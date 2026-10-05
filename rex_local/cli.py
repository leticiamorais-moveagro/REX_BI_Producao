"""One local collector, full SQLite history, and an optional cloud publication."""

from __future__ import annotations

import argparse
from calendar import monthrange
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from rex_api import RexAPIError, RexHTTPError
from . import auth
from .model import NormalizationError, normalize_daily
from .store import Store, StoreError
from .sheets import SheetsError


class LocalError(ValueError):
    pass


def default_folder() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise LocalError("Use --pasta para indicar a pasta local de armazenamento.")
    return Path(base) / "REXIntegracao"


def parse_day(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise LocalError("Datas devem usar AAAA-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise LocalError("Data de calendario invalida.") from None


def yesterday(zone: str = "America/Sao_Paulo") -> date:
    try:
        return datetime.now(ZoneInfo(zone)).date() - timedelta(days=1)
    except ZoneInfoNotFoundError:
        raise LocalError("Instale as dependencias para usar o fuso America/Sao_Paulo.") from None


def twelve_month_start(end: date) -> date:
    boundary = end + timedelta(days=1)
    year = boundary.year - 1
    return date(year, boundary.month, min(boundary.day, monthrange(year, boundary.month)[1]))


def days(begin: date, end: date):
    current = begin
    while current <= end:
        yield current
        current += timedelta(days=1)


def sheet_id(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    match = re.fullmatch(r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)(?:/[\S]*)?", value)
    candidate = match.group(1) if match else value
    if not re.fullmatch(r"[A-Za-z0-9_-]{15,150}", candidate):
        raise LocalError("Informe o ID ou o link oficial de uma planilha Google dedicada a REX.")
    return candidate


def read_config(folder: Path) -> dict:
    try:
        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise LocalError("Execute configurar no menu iniciar_rex.cmd antes de coletar.") from None
    allowed = {"version", "inicio_historico", "planilha_id", "timezone", "backup_dir", "limite_celulas"}
    if not isinstance(config, dict) or set(config) != allowed or config.get("version") != 1:
        raise LocalError("Configuracao invalida. Execute configurar novamente pelo menu.")
    parse_day(config["inicio_historico"])
    config["planilha_id"] = sheet_id(config["planilha_id"])
    if config["timezone"] != "America/Sao_Paulo":
        raise LocalError("O fuso configurado deve ser America/Sao_Paulo.")
    if not isinstance(config["backup_dir"], str) or not config["backup_dir"]:
        raise LocalError("Informe uma pasta para os backups.")
    if type(config["limite_celulas"]) is not int or not 1 <= config["limite_celulas"] <= 20_000_000:
        raise LocalError("Limite de celulas invalido.")
    return config


def write_json(path: Path, document: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def configure(folder: Path, *, credentials: bool) -> None:
    if not sys.stdin.isatty():
        raise LocalError("Abra iniciar_rex.cmd para configurar em um terminal interativo.")
    if credentials:
        auth.save_rex_credentials(folder)
        print("Credenciais REX salvas no cofre deste usuario Windows.")
        return
    try:
        previous = read_config(folder)
    except LocalError:
        previous = {}
    initial = previous.get("inicio_historico", yesterday().isoformat())
    initial = input(f"Inicio do historico [{initial}]: ").strip() or initial
    if parse_day(initial) > yesterday():
        raise LocalError("Use ontem ou uma data anterior para iniciar o historico.")
    old_id = previous.get("planilha_id", "")
    identifier = sheet_id(input(f"Link/ID da planilha Google [{old_id or 'pode deixar vazio no piloto'}]: ").strip() or old_id)
    client_source = input("JSON OAuth do Google (caminho; vazio para configurar depois): ").strip().strip('"')
    client_document = None
    if client_source:
        source = Path(client_source).expanduser()
        auth.read_oauth_client(source)
        client_document = json.loads(source.read_text(encoding="utf-8-sig"))
    backup_default = previous.get("backup_dir", str(folder / "backups"))
    backup_text = input(f"Pasta de backups [{backup_default}]: ").strip().strip('"') or backup_default
    backup_path = Path(backup_text).expanduser().resolve()
    if not backup_path.is_absolute():
        raise LocalError("Use uma pasta absoluta para os backups.")
    document = {
        "version": 1, "inicio_historico": initial, "planilha_id": identifier,
        "timezone": "America/Sao_Paulo", "backup_dir": str(backup_path),
        "limite_celulas": previous.get("limite_celulas", 10_000_000),
    }
    if client_document is not None:
        write_json(folder / "cliente_google.json", client_document)
    write_json(folder / "config.json", document)
    print(f"Configuracao salva. Banco ativo: {folder / 'historico.sqlite3'}")
    print("O teste inicial consulta somente ontem. Amplie o inicio depois de conferir o volume.")


@contextmanager
def local_lock(folder: Path):
    path = folder / "execucao.lock"
    stream = path.open("a+b")
    acquired = False
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise LocalError("Outra execucao REX esta usando este armazenamento. Aguarde terminar.") from None
        yield
    finally:
        if acquired:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def collect(store: Store, client, requested: list[date], *, allow_empty: bool, sleep=time.sleep) -> int:
    rows_total = 0
    run_id = uuid.uuid4().hex
    for index, day in enumerate(requested):
        if index:
            sleep(1.1)
        iso = day.isoformat()
        try:
            payload = client.daily_production(iso, iso)
        except RexHTTPError as error:
            if error.status_code != 401:
                raise
            # A single re-login is safe for this read-only report, never a retry of imports.
            client.login()
            payload = client.daily_production(iso, iso)
        rows = normalize_daily(
            payload, day, imported_at=datetime.now(timezone.utc).isoformat(), load_id=run_id,
        )
        store.replace_day(day, rows, allow_empty=allow_empty)
        rows_total += len(rows)
        print(f"{iso}: {len(rows)} registros salvos.", flush=True)
    return rows_total


def requested_days(store: Store, config: dict, begin: str | None, end: str | None) -> list[date]:
    last = yesterday(config["timezone"])
    initial = parse_day(config["inicio_historico"])
    if initial > last:
        raise LocalError("Inicio do historico ainda nao e um dia encerrado.")
    if begin or end:
        if not (begin and end):
            raise LocalError("Informe --inicio e --fim juntos.")
        first, final = parse_day(begin), parse_day(end)
        if first > final or final > last or first < initial:
            raise LocalError("Intervalo deve estar entre o inicio configurado e ontem.")
        return list(days(first, final))
    already = store.collected_days()
    pending = set(days(initial, last)) - already
    pending.update(days(max(initial, last - timedelta(days=6)), last))
    return sorted(pending)


def make_backup(store: Store, config: dict) -> None:
    target = Path(config["backup_dir"])
    target.mkdir(parents=True, exist_ok=True)
    result = store.backup(target)
    print(f"Backup concluido: {result}")


def publish(store: Store, config: dict, folder: Path) -> None:
    from .sheets import Publisher
    if not config["planilha_id"]:
        raise LocalError("Configure uma planilha Google dedicada antes de publicar.")
    if not (folder / "cliente_google.json").is_file():
        raise LocalError("Configure o JSON OAuth do Google antes de publicar.")
    end = yesterday(config["timezone"])
    historical_start = parse_day(config["inicio_historico"])
    start = max(historical_start, twelve_month_start(end))
    if start > end:
        raise LocalError("O inicio do historico ainda nao e um dia encerrado.")
    missing = set(days(start, end)) - store.collected_days()
    if missing:
        raise LocalError(f"Publicacao suspensa: faltam {len(missing)} dias no intervalo. Execute coletar.")
    service = auth.google_service(folder, interactive=sys.stdin.isatty())
    Publisher(service, config["planilha_id"], cell_limit=config["limite_celulas"]).publish(
        store, start, end, historical_start=historical_start,
    )
    print(f"Publicacao concluida: {store.count(start, end)} registros, {start} a {end}.")
    print("Conecte o Data Studio a aba Producao_REX; Controle_REX mostra a ultima atualizacao.")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Historico local REX e publicacao Google Sheets.")
    root.add_argument("--pasta", help="Pasta local de dados; padrao LOCALAPPDATA/REXIntegracao.")
    commands = root.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configurar", help="Configurar o piloto ou a publicacao Google.")
    setup.add_argument("--credenciais", action="store_true", help="Salvar REX no cofre Windows.")
    commands.add_parser("testar", help="Coletar somente ontem e gerar backup; nao publica.")
    for name in ("coletar", "atualizar"):
        child = commands.add_parser(name, help="Coletar historico pendente e reconsultar 7 dias recentes.")
        child.add_argument("--inicio")
        child.add_argument("--fim")
        child.add_argument("--permitir-dia-vazio", action="store_true", help="Permitir substituir um dia com registros por resposta vazia.")
    commands.add_parser("publicar", help="Publicar os ultimos 12 meses ja coletados.")
    commands.add_parser("backup", help="Gerar uma copia consistente do banco.")
    commands.add_parser("status", help="Mostrar apenas volume, cobertura e tamanho do banco.")
    return root


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        folder = Path(args.pasta).expanduser().resolve() if args.pasta else default_folder()
        folder.mkdir(parents=True, exist_ok=True)
        with local_lock(folder):
            if args.command == "configurar":
                configure(folder, credentials=args.credenciais)
                return 0
            config = read_config(folder)
            with Store(folder / "historico.sqlite3") as store:
                if args.command == "status":
                    print(json.dumps(store.overview(), indent=2, ensure_ascii=False, default=str))
                    last = yesterday(config["timezone"])
                    start = max(parse_day(config["inicio_historico"]), twelve_month_start(last))
                    print(f"Registros para o painel: {store.count(start, last)} (ate 12 meses).")
                elif args.command == "backup":
                    make_backup(store, config)
                elif args.command == "publicar":
                    publish(store, config, folder)
                else:
                    requested = [yesterday(config["timezone"])] if args.command == "testar" else requested_days(store, config, args.inicio, args.fim)
                    print(f"Coleta diaria: {len(requested)} dias; nenhum registro antigo sera expirado.", flush=True)
                    client = auth.rex_client(folder)
                    # Backup even a partially completed history when a later API day fails.
                    try:
                        collect(store, client, requested, allow_empty=getattr(args, "permitir_dia_vazio", False))
                    finally:
                        make_backup(store, config)
                    if args.command == "atualizar":
                        publish(store, config, folder)
                    elif args.command == "testar":
                        print("PILOTO CONCLUIDO. Execute status e confira o volume antes de ampliar o periodo.")
        return 0
    except (LocalError, StoreError, AuthError, SheetsError, NormalizationError, RexAPIError) as error:
        print(f"Nao concluido: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Execucao interrompida. Dias ja salvos continuam no banco.", file=sys.stderr)
        return 130
    except Exception:
        # No raw exceptions from credential managers or Google HTTP bodies in logs.
        print("Nao concluido: falha local ou no Google. Confira pastas, rede, permissao e o guia.", file=sys.stderr)
        return 1


AuthError = auth.AuthError


if __name__ == "__main__":
    raise SystemExit(main())
