# 🤖 Bot de Arbitragem — BRZ / BRLA vs USDT/USDC

Monitor de spread on-chain entre stablecoins brasileiras e dólares digitais,
rodando via Telegram com vault criptografado.

---

## Tokens monitorados

| Token | Emissor       | Redes                          |
|-------|--------------|-------------------------------|
| BRZ   | Transfero    | Ethereum, Polygon, Arbitrum    |
| BRLA  | —            | Polygon, Base                  |
| USDT  | Tether       | Ethereum, Polygon, Arbitrum    |
| USDC  | Circle       | Ethereum, Polygon, Arbitrum, Base |

---

## Estrutura do projeto

```
brl_arb_bot/
├── main.py
├── config.py             # tokens, redes, thresholds
├── requirements.txt
├── .env.example
├── vault/
│   ├── vault.py          # cofre AES-256
│   ├── users.db          # gerado automaticamente
│   └── .vault_key        # chave mestra (chmod 600)
├── bot/
│   └── handlers.py       # fluxo Telegram
└── engine/
    ├── prices.py          # preços via 1inch + GeckoTerminal
    └── arbitrage.py       # detecção de spread e cálculo de lucro líquido
```

---

## Como o lucro líquido é calculado

```
lucro_bruto  = amount_usd × spread_pct / 100
lucro_líquido = lucro_bruto − gas_usd − fee_swap − slippage
```

O bot só alerta quando `lucro_líquido > $0.50` (ajustável em `config.py`).

---

## Instalação e execução

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edite .env e adicione seu TELEGRAM_BOT_TOKEN

python main.py
```

---

## Deploy Hetzner VPS (produção)

```bash
# VPS CX21 ~€5/mês — Ubuntu 22.04
sudo apt update && sudo apt install -y python3 python3-venv git

git clone SEU_REPO /opt/brl_arb_bot
cd /opt/brl_arb_bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env

# Serviço systemd
sudo tee /etc/systemd/system/brlbot.service << 'EOF'
[Unit]
Description=BRL Arb Bot
After=network.target

[Service]
WorkingDirectory=/opt/brl_arb_bot
EnvironmentFile=/opt/brl_arb_bot/.env
ExecStart=/opt/brl_arb_bot/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now brlbot
sudo journalctl -u brlbot -f
```

---

## Fluxo do aluno no Telegram

```
/start      → boas-vindas
/cadastrar  → 2 etapas:
               1. Endereço público (0x...)
               2. Private key → deletada do chat automaticamente
              ✅ Pronto — nunca mais precisa digitar
/iniciar    → liga o monitor (scan a cada 20s)
/parar      → pausa
/status     → estado atual
/historico  → últimas 10 operações
```

---

## Boas práticas para seus alunos

1. Criar carteira **nova e dedicada** só ao bot — nunca a MetaMask principal
2. Depositar apenas o capital que aceita arriscar
3. Polygon e Base têm gas barato — ideais para começar com capital menor
4. Ajustar `AMOUNT_USDT_PADRAO` em `config.py` conforme o capital disponível
5. Ajustar `MIN_LUCRO_USD` para não operar com margem muito pequena

---

## Próximos passos

- [ ] Execução automática do swap via 1inch API (já tem a infraestrutura)
- [ ] Painel admin para você ver todos os alunos e suas operações
- [ ] Suporte a BRL1 (Mercado Bitcoin) quando tiver liquidez on-chain
- [ ] Alertas de P&L diário
- [ ] Integração com Li.Fi para arbitragem cross-chain automatizada
