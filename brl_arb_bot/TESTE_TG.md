# Teste Guiado Telegram (9 telas)

## Preparacao

1. Copie `.env.example` para `.env` dentro de `brl_arb_bot/` e preencha os campos.
2. Instale dependencias:
   - `pip install -r requirements.txt`
3. Rode smoke test:
   - `python scripts/tg_smoke_test.py`
4. Inicie o bot:
   - `python main.py`

## Fluxo do aluno (1 a 7)

1. `/start` (novo usuario)
   - Esperado: mensagem inicial + botao de cadastro.
2. `/cadastrar`
   - Esperado: passo 1 pedindo endereco.
3. Envie endereco `0x...`
   - Esperado: passo 2 pedindo private key.
4. Envie private key
   - Esperado: mensagem de cadastro completo + botoes iniciar/definir modo.
5. `/modo`
   - Esperado: troca entre Manual e Automatico funcionando.
6. `/iniciar`
   - Esperado: monitor iniciado.
7. Oportunidade detectada
   - Manual: alerta com botoes Executar/Ignorar.
   - Automatico: execucao automatica com mensagem de resultado.
8. Execucao
   - Esperado: confirmacao de swap com rede/par/lucro e link explorer.
9. `/painel`
   - Esperado: menu com Dashboard, Operacoes, Lucros e Carteira.

## Fluxo do admin (8 e 9)

1. `/admin`
   - Esperado: tela com marcador ADMIN + 4 opcoes.
2. Dashboard geral
   - Esperado: usuarios ativos, total ops, taxa de sucesso, lucro total, lucro hoje, melhor spread, rede mais ativa.
3. Usuarios
   - Esperado: lista de usuarios cadastrados.
4. Lucro por aluno
   - Esperado: ranking por lucro.
5. Operacoes recentes
   - Esperado: lista das ultimas operacoes.

## Criterios de pronto para producao

1. Cadastro completo sem erro e com delecao da mensagem de private key.
2. Modo Manual e Automatico alternando corretamente por usuario.
3. Swap manual executando via botao.
4. Swap automatico executando no loop quando houver oportunidade.
5. Painel do aluno e painel admin navegando sem conflito de callback.
