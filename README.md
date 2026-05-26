# MaskUSDT

## Deploy Automático (Push -> VPS)

Este repositório agora possui workflow em [.github/workflows/deploy-arbbot.yml](.github/workflows/deploy-arbbot.yml).

Quando houver push na `main`, o GitHub Actions:

1. sincroniza `brl_arb_bot/` para o servidor via `rsync`
2. preserva segredos locais do servidor (`.env`, `vault/users.db`, `vault/.vault_key`)
3. reinicia o serviço systemd do bot

### Secrets obrigatórios no GitHub

Configure em `Settings -> Secrets and variables -> Actions`:

- `DEPLOY_HOST` (ex.: `188.245.165.157`)
- `DEPLOY_USER` (ex.: `root`)
- `DEPLOY_PATH` (ex.: `/opt/arbbot`)
- `DEPLOY_SERVICE` (ex.: `arbbot`)
- `DEPLOY_SSH_KEY` (chave privada SSH para acessar o servidor)

Sem esses secrets, o workflow falha por segurança.