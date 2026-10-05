# REX no computador de trabalho

O programa guarda a produção diária em um banco local e envia uma janela de até 12 meses para uma planilha Google. O Data Studio usa essa planilha como fonte.

```text
API REX → Banco SQLite no PC → Google Sheets → Data Studio
                  ↓
          Cópias de segurança
```

A preparação acontece em duas etapas: primeiro configurar o computador e testar a coleta de ontem; depois autorizar o Google e publicar a base. Este pacote contém somente o programa. Ele não inclui registros da empresa, usuário, senha, tokens ou arquivos de autorização Google.

## 1. Instalar e testar somente a base local

1. No computador de trabalho, extraia todo o arquivo **REX_PC_Trabalho.zip** para uma pasta local em que você possa gravar arquivos. Não execute os arquivos de dentro do ZIP.
2. Tenha um Python **3.10.7 ou superior** autorizado pela TI. O instalador procura `py -3` e depois `python`; não depende do Python instalado em outro computador. [Python no Windows](https://docs.python.org/3/using/windows.html)
3. Abra **instalar_rex.cmd**. Ele cria `.venv` nessa pasta e instala as bibliotecas de **requirements-local.txt**. Aguarde a mensagem de instalação concluída. São necessários internet e acesso aos repositórios de pacotes permitidos pela empresa.
4. Abra **iniciar_rex.cmd** e escolha **1 — Configurar**. Para o primeiro teste, mantenha o início da base em **ontem**. Os campos do Google Sheets e do arquivo OAuth podem ficar vazios nesta etapa.
5. Escolha **2 — Informar usuário e senha REX**. Digite a credencial localmente; a senha não aparece enquanto você digita. O programa guarda as credenciais no Gerenciador de Credenciais deste usuário do Windows, usando `keyring`. [Documentação do keyring](https://keyring.readthedocs.io/en/latest/)
6. Escolha **3 — Testar**. Essa opção consulta somente a produção de ontem, grava o dia no banco local e faz backup; ela não publica nada no Google. Depois use **8 — Status** para conferir a base.

Se o computador bloquear Python, o download das bibliotecas ou o armazenamento de credenciais, leve a mensagem exibida à TI. Vamos continuar pelas telas desse computador; não é necessário enviar sua senha pelo chat.

Depois do piloto, amplie o início da base em etapas e importe períodos curtos. A opção **4 — Coletar** consulta os últimos 7 dias encerrados e os dias ainda faltantes desde o início configurado. Por isso, mudar o início para um ano atrás pode gerar muitas chamadas à API; primeiro confirme o volume e o acesso a esse histórico.

## Onde o banco e as credenciais ficam

O diretório padrão de execução é `%LOCALAPPDATA%\REXIntegracao`, pertencente ao usuário atual do Windows. A configuração e o banco SQLite ficam nesse diretório, fora da pasta do código e do OneDrive. As credenciais REX e a autorização OAuth são armazenadas no Gerenciador de Credenciais.

Mantenha o **banco ativo em uma pasta local sem sincronização**. Você pode indicar uma pasta de backup no Google Drive para computador ou no OneDrive: o programa cria uma cópia do banco e essa cópia pode ser sincronizada. O backup contém dados da empresa; escolha a pasta corporativa adequada. Os backups são versionados e não são apagados automaticamente: acompanhe o espaço disponível e mantenha a retenção definida pela empresa.

Ao trocar de computador, reinstale as bibliotecas usando o pacote limpo e informe as credenciais nesse Windows. A `.venv` e as credenciais de um computador não fazem parte do pacote portátil. Uma migração do histórico precisa ser feita por backup, com o programa fechado e o destino local conferido.

## 2. Autorizar e publicar no Google Sheets

Faça esta etapa depois de confirmar o teste local.

1. Crie uma planilha Google na conta da empresa para receber a produção. Copie seu ID: é o texto que aparece entre `/d/` e `/edit` na URL.
2. Com a TI, crie ou selecione um projeto Google Cloud da organização e habilite somente a **Google Sheets API**. Configure o app OAuth como **Interno**, quando disponível para a organização, e crie um cliente do tipo **Aplicativo para computador**. Baixe o JSON desse cliente para uma pasta local. Esse é o arquivo de cliente OAuth; não é uma chave de conta de serviço. [Preparação oficial para Python e Sheets](https://developers.google.com/workspace/sheets/api/quickstart/python)
3. Volte à opção **1 — Configurar** e informe o ID da planilha, o caminho do JSON OAuth e a pasta de backup desejada.
4. Escolha **6 — Publicar**. Na primeira vez, o navegador abre a autorização Google. Entre com a conta corporativa que pode editar a planilha e autorize o acesso solicitado. A empresa pode exigir liberação desse aplicativo pelo administrador.
5. Confira as abas **Producao_REX** e **Controle_REX**. A primeira tem os dados do painel; a segunda informa a janela e o resultado da publicação. No Data Studio, crie a fonte **Google Sheets**, selecione a planilha e a aba **Producao_REX**.

O app usa o escopo `https://www.googleapis.com/auth/spreadsheets`. A autorização é guardada localmente para as próximas execuções; não é preciso fazer login em cada atualização enquanto ela permanecer válida. Se o acesso for revogado, o app for bloqueado ou as políticas da empresa mudarem, poderá ser necessário autorizar novamente. [Escopos da Sheets API](https://developers.google.com/workspace/sheets/api/scopes)

Esta opção não usa BigQuery, Cloud Run nem uma conta de faturamento habilitada para executar a coleta. O uso padrão da Sheets API está disponível sem custo adicional, sujeito às cotas do Google; vamos operar dentro dessas cotas. [Limites e condições da Sheets API](https://developers.google.com/workspace/sheets/api/limits)

## Histórico e capacidade da planilha

O banco local conserva o histórico coletado. A publicação envia **até os últimos 12 meses**, a partir do início configurado quando a base for mais recente, sempre até ontem. Para publicar, os dias dessa janela precisam estar consultados de forma contínua; um dia validamente sem registros é diferente de um dia ainda não coletado. Se faltar uma data, complete a coleta antes de publicar.

O modelo publicado tem **31 colunas**, com nomes estáveis. A coluna `data` é publicada como uma data reconhecida pelo Google Sheets para funcionar no filtro de período do Data Studio. Credenciais não são colunas da planilha.

O SQLite conserva os valores decimais exatos. Na planilha, as medidas são números para análise e ficam sujeitas à precisão numérica do Google Sheets; códigos permanecem textos, inclusive zeros à esquerda. O publicador prepara uma aba temporária e só substitui a aba pública depois de terminar o envio. Os IDs das abas públicas permanecem estáveis para a conexão do Data Studio. Use somente este PC como publicador da planilha.

O Google informa atualmente um limite de 20 milhões de células. Dimensionamos esta versão de forma conservadora para **10 milhões de células no arquivo inteiro**, incluindo a aba temporária usada durante a publicação, a aba de controle e outras abas existentes. Uma estimativa inicial é:

```text
células durante a publicação ≈ 2 × 31 × linhas da janela + controle + outras abas
```

Isso dá cerca de **161 mil linhas antes de reservar espaço para cabeçalhos e outras abas**. Por exemplo, 500 registros por dia durante 365 dias já exigiriam cerca de 11,3 milhões de células com a área temporária. Por isso, ter um banco local não garante que 12 meses caibam em uma única planilha: precisamos medir o piloto e conferir o cálculo antes de ampliar a janela. O limite é de todo o arquivo, não de cada aba. [Limites dos arquivos Google](https://support.google.com/drive/answer/37603)

Não edite a aba de dados manualmente enquanto uma publicação estiver acontecendo. Para cálculos e gráficos, use o Data Studio ou abas separadas, considerando seu espaço no mesmo arquivo.

## Comandos e rotina diária

O menu cobre os comandos abaixo. Para uso em um terminal, abra-o na pasta extraída do programa e execute:

```powershell
.\.venv\Scripts\python.exe -m rex_local configurar
.\.venv\Scripts\python.exe -m rex_local configurar --credenciais
.\.venv\Scripts\python.exe -m rex_local testar
.\.venv\Scripts\python.exe -m rex_local coletar --inicio 2026-09-01 --fim 2026-09-07
.\.venv\Scripts\python.exe -m rex_local publicar
.\.venv\Scripts\python.exe -m rex_local atualizar
.\.venv\Scripts\python.exe -m rex_local status
.\.venv\Scripts\python.exe -m rex_local backup
```

As duas datas da coleta são inclusivas e devem ser fornecidas juntas, entre o início configurado e ontem. Sem datas, `coletar` revisita os últimos 7 dias encerrados e cobre as lacunas desde o início da base. A coleta faz backup ao terminar, incluindo dias já salvos quando uma consulta posterior falha. `atualizar` executa a coleta, cria backup e publica no Google. `backup` faz uma cópia do banco sem consultar a API.

Se um dia que já tinha registros voltar vazio durante o reprocessamento, a rotina preserva seus dados e interrompe para conferência. A opção de terminal `--permitir-dia-vazio`, disponível em `coletar` e `atualizar`, autoriza essa substituição somente quando o retorno vazio tiver sido confirmado. Um dia novo validamente vazio pode ser registrado como consultado sem criar linhas de produção.

Para usar outro diretório de execução, informe `--pasta` antes do comando, mantendo o destino fora de sincronização:

```powershell
.\.venv\Scripts\python.exe -m rex_local --pasta "C:\REXDados" status
```

Depois de validar a rotina manual e a autorização Google, podemos configurar o Agendador de Tarefas do Windows. A tarefa deve usar **o mesmo usuário Windows** que configurou as credenciais e chamar o Python de `.venv` com `-m rex_local atualizar`, usando a pasta do código como diretório inicial. O computador precisa estar ligado, disponível e conectado à internet no horário escolhido; não precisa permanecer ligado continuamente. Não há agendamento automático criado por estes arquivos. [Contexto de execução das tarefas Windows](https://learn.microsoft.com/en-us/windows/win32/taskschd/security-contexts-for-running-tasks)

## Sobre os motivos de parada

A produção diária é consolidada por equipamento e pelas dimensões disponíveis na REX. `operacao_nome` e `classe_operacao` podem identificar manutenção ou perdas cadastradas. O relatório não traz a causa raiz separada nem o horário de início/fim de cada parada. `offline` indica dados limitados e não significa máquina desligada.

Para distribuir o tempo por operação/classe, use `horas_total`. Não some esse total às demais colunas de horas; analisar somente `horas_parado` pode deixar de fora manutenção registrada com tempo desligado. A unidade de área, distância e combustível deve ser confirmada com a REX antes de rotular os gráficos.
