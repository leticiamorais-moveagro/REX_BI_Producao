@echo off
setlocal EnableExtensions DisableDelayedExpansion
title REX - Integracao local
pushd "%~dp0"
if errorlevel 1 goto pasta_inacessivel
if not exist ".venv\Scripts\python.exe" goto falta_instalacao
if not exist "rex_local\__main__.py" goto pacote_incompleto

:menu
echo.
echo REX - Integracao local
echo.
echo 1 - Configurar inicio da base, Google Sheets e pasta de backup
echo 2 - Informar usuario e senha REX neste Windows
echo 3 - Testar acesso, coletar somente ontem e fazer backup
echo 4 - Coletar dias recentes e faltantes da base configurada
echo 5 - Coletar um intervalo de datas
echo 6 - Publicar a base no Google Sheets
echo 7 - Atualizar: coletar, fazer backup e publicar
echo 8 - Ver status da base local
echo 9 - Fazer backup
echo 0 - Sair
echo.
choice /c 1234567890 /n /m "Escolha: "
if errorlevel 255 goto falha_menu
if errorlevel 10 goto sair
if errorlevel 9 goto fazer_backup
if errorlevel 8 goto ver_status
if errorlevel 7 goto atualizar
if errorlevel 6 goto publicar
if errorlevel 5 goto coletar_intervalo
if errorlevel 4 goto coletar
if errorlevel 3 goto testar
if errorlevel 2 goto credenciais
if errorlevel 1 goto configurar
goto menu

:configurar
".venv\Scripts\python.exe" -m rex_local configurar
goto resultado

:credenciais
".venv\Scripts\python.exe" -m rex_local configurar --credenciais
goto resultado

:testar
".venv\Scripts\python.exe" -m rex_local testar
goto resultado

:coletar
".venv\Scripts\python.exe" -m rex_local coletar
goto resultado

:coletar_intervalo
".venv\Scripts\python.exe" -c "import subprocess, sys; inicio = input('Data inicial (AAAA-MM-DD): ').strip(); fim = input('Data final (AAAA-MM-DD): ').strip(); raise SystemExit(subprocess.call([sys.executable, '-m', 'rex_local', 'coletar', '--inicio', inicio, '--fim', fim]))"
goto resultado

:publicar
".venv\Scripts\python.exe" -m rex_local publicar
goto resultado

:atualizar
".venv\Scripts\python.exe" -m rex_local atualizar
goto resultado

:ver_status
".venv\Scripts\python.exe" -m rex_local status
goto resultado

:fazer_backup
".venv\Scripts\python.exe" -m rex_local backup
goto resultado

:resultado
set "rexLocalExit=%ERRORLEVEL%"
echo.
if "%rexLocalExit%"=="0" echo Operacao concluida. Confira o resultado acima.
if not "%rexLocalExit%"=="0" echo A operacao terminou com codigo %rexLocalExit%. Confira a mensagem acima.
pause
goto menu

:falta_instalacao
echo Execute instalar_rex.cmd antes de abrir o programa.
goto finalizar_erro

:pacote_incompleto
echo Nao encontrei rex_local\__main__.py. Extraia todos os arquivos do pacote.
goto finalizar_erro

:falha_menu
echo Nao consegui ler a opcao do menu.

:finalizar_erro
pause
popd
exit /b 1

:sair
popd
exit /b 0

:pasta_inacessivel
echo Nao consegui abrir a pasta do programa. Extraia o pacote em uma pasta local.
pause
exit /b 1
