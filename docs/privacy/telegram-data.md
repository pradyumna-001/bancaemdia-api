# Dados do bot Telegram

O bot atende somente chats privados vinculados. O tenant é resolvido pelo vínculo
persistido; autores de encaminhamento e campos arbitrários não definem proprietário.
Callbacks e mensagens editadas são ignorados na v1 (inclusive versões antigas,
expiradas e de terceiros). A interface usa comandos textuais explícitos.

## Armazenamento e prazos

Não foi encontrada decisão aprovada de retenção de conteúdo do bot. Os padrões
abaixo são escolhas técnicas conservadoras e configuráveis, não obrigações legais.
O proprietário deve aprovar os prazos antes do lançamento.

| Dado / finalidade | Local | Remoção |
| --- | --- | --- |
| Payload normalizado, para processamento | `telegram_inbox`, Fernet | Imediata ao processar; terminal/DLQ após `TELEGRAM_RAW_RETENTION_DAYS` (7 dias) |
| Texto de resposta, para entrega recuperável | `telegram_outbox`, Fernet | SENT/DLQ após o mesmo prazo de 7 dias |
| Imagem, para conferir o rascunho | `telegram_media`, Fernet, RLS do tenant | `TELEGRAM_MEDIA_RETENTION_DAYS` (7 dias) após fechamento; unlink remove imediatamente |
| Referência de arquivo, legenda e metadados da foto | Rascunho | Mesmo prazo da mídia |
| Campos, candidatos e histórico de correções | Rascunho e correções, RLS | `TELEGRAM_DRAFT_RETENTION_DAYS` (30 dias) após fechamento |
| Resultado temporário da extração | Redis `tgext:<HMAC tenant>:<hash>:<versão>`, Fernet | TTL máximo 60 segundos, inclusive após crash/unlink; nunca cache global de exports |
| Contadores de abuso | `telegram_rate_buckets`, somente HMACs | Expiração de janela; remoção no purge seguinte |
| Fato financeiro explicitamente confirmado | Eventos/apostas do tenant | Política financeira/exportação/anonimização da conta; não é payload bruto do bot |

`telegram.purge` roda a cada hora via Beat, em lotes de
`TELEGRAM_PURGE_BATCH_SIZE` (100, máximo 1.000). Processamento pendente, outbox
PENDING/SENDING e rascunhos em uso não são apagados pelo timer. Alerta de backlog
exige recuperação ou decisão operacional de descarte; não se perde trabalho
duravelmente aceito para cumprir uma idade arbitrária. Cancelamento explícito
fecha o rascunho e inicia seus prazos.

O purge mantém somente `update_id`, estado e timestamps da inbox como tombstone
sem conteúdo ou identidade Telegram. No rascunho encerrado mantém UUID, tenant,
estado, versão, timestamp e HMAC da origem para impedir duplicação por uma foto
repetida com outro update. Outbox conserva chave idempotente e tenant; não conserva
chat nem texto. Esses identificadores técnicos vivem enquanto a conta existe;
anonimização remove os objetos do tenant. Não representam armazenamento de conteúdo
pessoal sob o nome de auditoria. O vínculo ativo precisa dos IDs para operar;
revogação bloqueia processamento e entrega pendentes.

Mídia nova do bot fica no PostgreSQL; não é enviada ao S3. O armazenamento de
exports legados permanece separado. Blobs antigos do bot sem dono adicional são
removidos por `telegram_purge_legacy_media`; um hash compartilhado com importação
ou revisão segue a política daquele recurso. A função nunca apaga blob de outro
fluxo nem um objeto S3. Instalações com migração externa para S3 devem tratar essas
referências pelo runbook de armazenamento antes da exclusão assistida.

## Solicitações de privacidade

Use `DELETE /api/v1/telegram/link` autenticado para desvincular. Ele neutraliza
inbox/outbox, cancela rascunhos ativos e remove sua mídia. Apostas confirmadas
continuam disponíveis. Uma chamada de rede já em voo não pode ser recolhida.
`DELETE /api/v1/usuario/me` aplica a anonimização canônica e limpa o transporte;
contas com assinatura externa ativa ou imports sem propriedade exclusiva mantêm
os bloqueios assistidos existentes. Não force exclusão ignorando essas proteções.

Fernet deriva a chave de `COLETA_TOKEN_SECRET`. Rotação desse segredo exige drenar
ou recriptografar payloads/mídias antes de trocar a chave; trocar somente o token
do bot ou segredo do webhook não muda a criptografia em repouso. Não exporte
conteúdo bruto em diagnóstico. Backups privados seguem a política operacional de
backup; restaurar backup exige reaplicar pedidos de exclusão antes de reabrir tráfego.
