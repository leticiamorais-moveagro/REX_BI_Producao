@echo off
setlocal EnableExtensions DisableDelayedExpansion
title REX - Instalacao local
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
pushd "%~dp0"
if errorlevel 1 goto pasta_inacessivel

echo REX - Instalacao para este computador
echo.
echo Sera criado um ambiente Python .venv nesta pasta.
echo As bibliotecas serao baixadas conforme requirements-local.txt.
echo Dados e credenciais nao sao incluidos no pacote.
echo.

if not exist "requirements-local.txt" goto pacote_incompleto
if exist ".venv\Scripts\python.exe" goto verificar_ambiente

where py >nul 2>&1
if errorlevel 1 goto tentar_python
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10, 7) else 1)" >nul 2>&1
if errorlevel 1 goto tentar_python
py -3 -m venv ".venv"
if errorlevel 1 goto falha_instalacao
goto instalar_bibliotecas

:tentar_python
where python >nul 2>&1
if errorlevel 1 goto python_ausente
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10, 7) else 1)" >nul 2>&1
if errorlevel 1 goto python_ausente
python -m venv ".venv"
if errorlevel 1 goto falha_instalacao
goto instalar_bibliotecas

:verificar_ambiente
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10, 7) else 1)" >nul 2>&1
if errorlevel 1 goto ambiente_incompativel

:instalar_bibliotecas
".venv\Scripts\python.exe" -m pip install -r "requirements-local.txt"
if errorlevel 1 goto falha_instalacao
".venv\Scripts\python.exe" -c "import googleapiclient.discovery, google_auth_oauthlib.flow, google_auth_httplib2, keyring; from zoneinfo import ZoneInfo; ZoneInfo('America/Sao_Paulo')"
if errorlevel 1 goto falha_instalacao
echo.
echo Instalacao concluida. Abra iniciar_rex.cmd para configurar e testar.
pause
popd
exit /b 0

:python_ausente
echo.
echo Nao encontrei um Python compativel. E necessario Python 3.10.7 ou superior.
echo Instale Python com a orientacao da TI e execute este arquivo novamente.
echo Guia oficial: https://www.python.org/downloads/windows/
goto finalizar_erro

:ambiente_incompativel
echo.
echo O ambiente .venv existente nao pode ser usado neste computador.
echo Se voce copiou .venv de outro PC, extraia novamente o pacote limpo.
echo A instalacao deve ser feita no computador em que o programa vai executar.
goto finalizar_erro

:pacote_incompleto
echo.
echo Nao encontrei requirements-local.txt. Extraia todos os arquivos do pacote.
goto finalizar_erro

:falha_instalacao
echo.
echo A instalacao nao foi concluida. Confira a mensagem exibida acima.
echo Se o download estiver bloqueado pela empresa, consulte a TI.

:finalizar_erro
pause
popd
exit /b 1

:pasta_inacessivel
echo Nao consegui abrir a pasta do programa. Extraia o pacote em uma pasta local.
pause
exit /b 1
