# Operação do bot Telegram

Escopo v1: chat privado vinculado, uma aposta/foto por vez, sem álbum ou criação
financeira por texto puro. Texto pode preencher/corrigir um rascunho. `/confirmar`
é obrigatório; foto e extração nunca gravam aposta. `/continuar` lê o estado salvo,
`/corrigir odd 1,95` corrige somente odd, `casa=Betano; stake=2` preenche dois campos,
`cupom=1` escolhe um bilhete em foto ambígua e `/cancelar` encerra sem efeito financeiro.
Stake extraída só é aceita quando a imagem declara unidades explicitamente;
valor monetário não é convertido por suposição.

## Configuração e rotação

1. Aplicar migrations e grants com o papel administrativo; API/worker usam o
   papel restrito `bancaemdia_app`. PostgreSQL 16 e Redis 7 são dependências reais.
2. Injetar `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET` e `COLETA_TOKEN_SECRET`
   pelo gerenciador de segredos. Nunca colocar valores em comandos gravados,
   logs, URLs de navegador, tickets ou fixtures.
3. Em webhook, usar `TELEGRAM_MODE=webhook` e registrar por `setWebhook` a URL HTTPS
   `/api/v1/integrations/telegram/webhook`, com `secret_token` igual ao segredo.
   Consultar `getWebhookInfo` e observar `pending_update_count` cair. Resposta 202
   só acontece depois do commit. 403 indica segredo incorreto; 413 corpo acima
   de 128 KiB; 415 tipo diferente de JSON; 503 configuração ou dependência indisponível.
4. Subir worker de materialização e Beat. Verificar `telegram.tick` a cada 5 s
   e `telegram.purge` a cada hora; conservar agendas de billing na integração.

Exemplo de registro (somente quando o operador autorizar o ambiente; não executado
pela validação desta issue):

```python
import asyncio, os
from bancaemdia.integrations.telegram.client import TelegramClient


async def register():
    client = TelegramClient()
    try:
        await client._request(
            "setWebhook",
            {
                "url": os.environ["TELEGRAM_WEBHOOK_URL"],
                "secret_token": os.environ["TELEGRAM_WEBHOOK_SECRET"],
            },
        )
        assert (await client.webhook_info())["url"] == os.environ["TELEGRAM_WEBHOOK_URL"]
    finally:
        await client.aclose()


asyncio.run(register())
```

Para rotação do token, pausar delivery, trocar o token pelo BotFather e no secret
store, reiniciar workers, testar `getWebhookInfo` e retomar filas. Para o segredo
do webhook, coordenar configuração da API com novo `setWebhook`; updates rejeitados
durante a janela serão reenviados. Não limpar inbox/outbox. Rotação da chave de
payload exige procedimento diferente: ver [privacidade](../privacy/telegram-data.md).

Polling local: remover webhook do bot de desenvolvimento, usar
`APP_ENV=development TELEGRAM_MODE=polling`, e executar
`python -m bancaemdia.integrations.telegram.polling`. O processo recusa webhook
ativo e confirma offset somente após persistir. Não executar vários pollers.

## Limites e indisponibilidade

`TELEGRAM_ACTION_LIMITS` é JSON `{ação: [chat, usuário, global]}`. Padrões por
janela de `TELEGRAM_LIMIT_WINDOW_SECONDS=60`:

| Ação | Chat | Usuário | Global |
| --- | ---: | ---: | ---: |
| link | 5 | 5 | 100 |
| photo | 10 | 10 | 200 |
| correction (inclui comandos de controle) | 30 | 30 | 1000 |
| confirmation | 20 | 20 | 500 |

Identidades vêm do vínculo; tentativa sem vínculo usa HMAC do remetente. Os três
contadores são atômicos na transação da inbox, com relógio PostgreSQL. Falha do
backend não libera a operação: rollback/retry mantém o update durável; após oito
falhas ele permanece na DLQ para intervenção. Replays não gastam outra cota nem
geram respostas ilimitadas. Resposta de limite é deduplicada por chat/ação/janela.
Recusa não revela se outro usuário ou código existe. Quotas individuais de outros
tenants não são consumidas. O limite global é compartilhado intencionalmente.

## Recuperação, retry e replay

Diagnóstico seguro, em sessão administrativa privada:

```sql
SELECT status, count(*), min(created_at) FROM telegram_inbox GROUP BY status;
SELECT status, last_error_code, count(*), min(created_at)
FROM telegram_outbox GROUP BY status, last_error_code;
SELECT status, extraction_error_code, count(*) FROM rascunhos_aposta
GROUP BY status, extraction_error_code;
```

Nunca selecionar ciphertext, conteúdo, token, caption ou IDs externos para logs.
Corrigir a causa antes do retry. Worker encerrado antes de commit deixa a inbox
PENDING. Lease de outbox expira em 60 s; extração em 600 s. Reiniciar worker e Beat
recupera registros vencidos. A DLQ não é confirmação de entrega.

Replay dirigido, após identificar o ID interno em sessão privada:

```sql
BEGIN;
SET LOCAL app.telegram_transport='on';
UPDATE telegram_inbox SET status='PENDING', attempts=0, next_attempt_at=now()
WHERE id=:internal_id AND status='DLQ' AND payload_ciphertext IS NOT NULL;
UPDATE telegram_outbox SET status='PENDING', attempts=0, next_attempt_at=now(),
  lease_token=NULL, lease_until=NULL
WHERE id=:reply_id AND status='DLQ' AND octet_length(payload_ciphertext)>0
  AND last_error_code IS DISTINCT FROM 'unlinked';
COMMIT;
```

Verificar DONE/SENT, backlog caindo e uma aposta/evento por `telegram_draft:<UUID>`.
Não recriar update, chave financeira ou rascunho. Conteúdo já purgado não é
reprocessável. Telegram tem entrega externa **pelo menos uma vez**: crash após
`sendMessage` aceito e antes de SENT pode repetir mensagem visível. O efeito
financeiro e a linha lógica de outbox continuam idempotentes.

## Alertas e rollback

Carregar [regras Prometheus](../../monitoring/telegram-alerts.yml). Métricas são
contagens, latência/idade em segundos; labels somente ação, etapa e motivo estável.
Backlog: inbox >120 s ou outbox >300 s por 5 min. DLQ: crescimento em 10 min por
1 min. Extração/entrega: ≥5 falhas em 10 min por 2 min. Vínculo suspeito: ≥10
recusas de quota em 10 min por 2 min. São limiares iniciais de baixa carga;
operador ajusta regras versionadas após medir tráfego, sem incluir IDs nas labels.

Primeiro verificar banco/Redis, worker/Beat e disponibilidade do provedor. Não
reenviar manualmente respostas nem consumir IA paga para testar recuperação.
Para rollback, pausar ingestão e workers, preservar filas e aplicar imagem
anterior compatível com schema expandido. Downgrade recusa apagar mídia retida;
antes de revertê-lo, cumprir retenção/exclusão e verificar ausência de mídia.
Nunca reverter a origem `telegram_bot` com apostas existentes.

## Validação reproduzível

```bash
pytest tests/integration/test_telegram_hardening.py -n 0 --junitxml=telegram-results.xml
promtool check rules monitoring/telegram-alerts.yml
promtool test rules monitoring/telegram-alerts.test.yml
```

Docker/Testcontainers ou `TEST_DATABASE_URL` e `TEST_REDIS_URL` são obrigatórios.
O teste falha quando falta infraestrutura. Só Telegram e IA são simulados;
PostgreSQL, RLS, Redis, transações, cache, leases e HTTP da aplicação são reais.
Nenhum bot real, webhook produtivo, cobrança ou AWS foi acionado nesses testes.
