# Telegram transport

The Telegram webhook writes each `update_id` to `telegram_inbox` before returning HTTP 202. Duplicate updates return the same response and do not create another business effect. The `telegram.tick` Celery task runs every five seconds on the materialization worker; Celery beat must be running. It processes due inbox rows and sends due tenant-owned `telegram_outbox` replies. A link redemption and its reply row commit together.

## Configure webhook

Set `TELEGRAM_WEBHOOK_SECRET` on the API service and `TELEGRAM_BOT_TOKEN` on the materialization worker through Secrets Manager. Both must be real, separate secret values; Terraform creates only secret metadata. Use `TELEGRAM_MODE=webhook` (the default). Register the HTTPS endpoint `/api/v1/integrations/telegram/webhook` with Telegram `setWebhook`, passing the same `secret_token` as `TELEGRAM_WEBHOOK_SECRET`. Telegram sends that value in `X-Telegram-Bot-Api-Secret-Token`. Do not put tokens in logs, tickets, or command history. The endpoint accepts JSON up to 128 KiB and has no user JWT requirement.

Check `getWebhookInfo` for the registered URL and pending update count. The endpoint should return 202 for a valid update after the database commit. A 503 means webhook mode or its secret is missing; 403 means the header does not match. Telegram should retry non-2xx responses.

## Local polling

Set `APP_ENV=development`, `TELEGRAM_MODE=polling`, and `TELEGRAM_BOT_TOKEN`, then run `python -m bancaemdia.integrations.telegram.polling`. Remove any registered webhook first. The command checks `getWebhookInfo` and refuses to poll when a webhook URL exists. It writes through the same inbox contract and advances the offset only after a committed update. Do not run multiple pollers for the same bot.

## Recovery and operations

Track `telegram_inbox_depth`, `telegram_outbox_depth`, their oldest age gauges, `telegram_transport_dlq_count`, retry totals, webhook rejection totals, and Bot API latency/error metrics. A persistent queue or rising age means the worker, beat, database, or Bot API needs attention. Retry delays are bounded; after eight failed attempts an item enters `DLQ`. Inspect `last_error_code` and the service state without printing encrypted payloads or credentials. Repair the cause, then requeue by setting `status='PENDING'`, `attempts=0`, and `next_attempt_at=now()` in an approved maintenance session. Do not requeue poison payloads until they are repaired or removed.

An outbox send is leased for 60 seconds. If a worker dies before calling Telegram, another tick can send after the lease expires. If Telegram accepts a `sendMessage` call and the worker dies before recording `SENT`, the Bot API provides no idempotency key for that call, so a repeated visible reply is possible. The database still produces only one outbox row per logical effect; external delivery is at least once across that narrow failure window.

Processed inbox ciphertext is cleared. Remaining transport metadata and outbox ciphertext are subject to the retention and privacy work in issue #102. Production payments remain under ADR 025 `NO_GO`; this transport does not activate Mercado Pago.
