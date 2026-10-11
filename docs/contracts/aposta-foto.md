# Foto original da aposta — backend #192

Contrato candidato desta árvore, para frontend #22/#50 e detalhe #19. Só fica
integrado quando o administrador mesclar a PR na main. Não exige os recursos
de filtros, USDT, projeção de detalhe, grupos ou Hipotética das PRs abertas.
O OpenAPI **inteiro** está em `tests/contract/schemas/openapi.json`, gerado pelo
app desta árvore; `docs/API.md` é a referência gerada. Não unir schemas de branches.

## Leitura e autorização

`GET /api/v1/apostas/{chave}/foto`, um parâmetro de caminho obrigatório,
string de 1 a 256 caracteres. Codificar a chave como um segmento de URL.
Sem corpo ou parâmetro de consulta. A chave vem da aposta acessível, nunca do hash.
O JSON de lista/detalhe permanece inalterado: não há campo novo de presença,
dimensão, URL, binário ou base64. Fazer a leitura sob demanda e interpretar 404
como ausência segura; `midia_hash` não é permissão nem comprovação de disponibilidade.

Sessão autenticada atual (cookie HttpOnly hospedado, ou Bearer no modo legado),
usuário ativo, propriedade e RLS obrigatórios. Usa snapshot REPEATABLE READ no
primário para evitar réplica atrasada após resolução, revogação ou remoção.
Permissão explícita `aposta_foto`: FULL_WRITE **e READ_ONLY**. Não concede direito
de escrita, não chama IA/extração, não cobra gasto e não altera eventos, apostas,
consolidações, movimentos de Caixa ou evidência financeira.

Depois da resolução válida da revisão, a imagem continua acessível quando seu
vínculo foi conservado. Aposta apagada logicamente continua legível ao dono, como
o detalhe existente; apagar/restaurar não muda foto nem retenção. Remoção definitiva,
anonimização, ausência de evidência e expiração devolvem ausência segura.

## Origens comprovadas e retenção

| Origem | Prova persistida usada | Armazenamento / limite de disponibilidade |
| --- | --- | --- |
| Importação de foto/export Telegram legada | `apostas.midia_hash` gravado pelo materializador e snapshot de replay | `Midia` + `MidiaArquivo`: banco ou S3 privado; política de retenção existente do recurso |
| Revisão com vínculo exato | Mesmo tenant e `extracao_bruta.aposta_chave` exatamente igual à chave | Revisão aberta ou resolvida; aceita somente um hash distinto, sem escolher a primeira revisão |
| Foto confirmada pelo bot Telegram | Chave UUID canônica + draft CONFIRMED + evento APOSTA_CRIADA do dono com draft exato + hash do draft/mídia | `TelegramMedia` privado cifrado; até `TELEGRAM_MEDIA_RETENTION_DAYS` após fechamento, padrão 7 dias; unlink remove imediatamente |
| Bot confirmado antigo com hash da aposta ausente | Mesmo vínculo exato draft/evento privado | Mesmo prazo; nunca recuperar pelo hash global |
| Manual, planilha, coleta/extensão, dados antigos sem vínculo suficiente | Nenhuma inferência de foto | 404 se não houver proveniência conservada |

O materializador do bot agora conserva o hash no fato e no snapshot de replay.
Isso não copia a imagem para o armazenamento global nem prolonga retenção. A rota
verifica o prazo mesmo antes da execução horária do purge. Um arquivo idêntico
no export de outro usuário não ressuscita mídia privada removida ou vencida.
`Midia` global deduplicada só é consultada após a prova da aposta/revisão do dono;
a identidade do primeiro remetente não decide acesso a um arquivo compartilhado.

## Transporte, segurança e falhas

200 retorna **os bytes originais** através da API autenticada, na mesma origem.
Banco e S3 têm o mesmo contrato. O servidor baixa S3 com SDK, valida o conteúdo e
entrega os bytes; nenhuma URL assinada, redirect, token, bucket, chave de objeto
ou referência cifrada vai ao navegador. A rota antiga de revisão não substitui
este recurso e continua limitada à fila aberta.

Até 20 MiB e 40 milhões de pixels. JPEG, PNG, GIF ou WebP reais, verificando
formato, MIME persistido, integridade do arquivo e SHA-256. SVG, HTML, arquivos
corrompidos, MIME divergente e limites excedidos são rejeitados. Nome fixo:
`Content-Disposition: inline; filename="foto-aposta.png"` (jpg/gif/webp conforme
formato). `Content-Type` é o MIME validado.

Todos os resultados, inclusive erros de sessão/quota/storage, usam
`Cache-Control: private, no-store`, `Pragma: no-cache`,
`X-Content-Type-Options: nosniff`, `Vary: Authorization, Cookie`,
`Referrer-Policy: no-referrer` e CSP `default-src 'none'; frame-ancestors 'none'`.
Sem ETag/304/cache público. Não existem referências temporárias que o cliente
precise renovar. O SDK tem timeouts de conexão/leitura 3/5 segundos, uma repetição
e orçamento assíncrono de 10 segundos para download; indisponibilidade é recuperável.

| HTTP | JSON / conduta |
| --- | --- |
| 401 | `{ "detail": "sessão necessária para consultar a foto" }` ou mensagem pt-BR de sessão inválida/expirada/inativa; renovar login conforme fluxo existente |
| 404 | Sempre `{ "detail": "não achei a foto desta aposta" }`: aposta/foto inexistente, alheia, sem vínculo, vínculo ambíguo, removida/expirada ou objeto ausente; não revela qual caso ocorreu |
| 422 | `{ "detail": "a foto armazenada é inválida, incompatível ou excede o limite permitido" }`; chave inválida usa `a chave da aposta é inválida` |
| 429 | Erro pt-BR e `Retry-After` em segundos da quota existente; respeitar o intervalo |
| 503 | `{ "detail": "não foi possível carregar a foto agora — tente novamente em instantes" }`, `Retry-After: 5`; inclui banco/storage/configuração/cifra indisponíveis; falhas de autenticação mantêm seu erro de sessão e intervalo aplicável |
| 500 | Erro genérico pt-BR sem detalhes internos; tratamento defensivo existente e headers privados |

Falha de imagem não bloqueia detalhe, correção ou revisão. O cliente pode mostrar
ausência ou botão de tentar novamente. Não transformar falha em foto de outra aposta.

## Integração do frontend

Reservar espaço; fazer GET apenas quando a imagem estiver visível. Com cookie,
`fetch('/api/v1/apostas/' + encodeURIComponent(chave) + '/foto',
{credentials: 'same-origin', cache: 'no-store', signal})`. Verificar status antes
de `blob()`, criar URL local com `URL.createObjectURL(blob)` e atribuir ao elemento
de imagem. Não usar `<img src>` com Bearer nem guardar credenciais em URL.
Cancelar requisição ao sair; revogar cada Object URL ao trocar aposta, desmontar,
desvincular ou encerrar sessão. Não persistir bytes/URLs em localStorage, cache de
serviço, analytics ou logs. Não repetir automaticamente 401/404/422; para 429/503,
respeitar Retry-After e oferecer recuperação. Refazer leitura após resolução, sem
depender de `foto_url` da fila. Estes são requisitos de integração, não alterações
já executadas no frontend nesta PR.

## Provas e decisões

Gate `Aposta photo (PostgreSQL and S3)` executa API real, PostgreSQL 16 com
forced RLS/NOBYPASSRLS e servidor HTTP S3 descartável usando PUT/GET do SDK real.
Cobre dois tenants/deduplicação, hash insuficiente, revisão aberta/resolvida,
apagada/restaurada, ingestão de export e confirmação bot, unlink/expiração,
formatos/limites, falhas, READ_ONLY com rollout real, sessão cookie revogada,
quota e invariância das quatro tabelas financeiras, inclusive com Caixa e
consolidação não vazios. O checker exige inventário completo e zero skips.
Fixtures de imagem são sintéticas, geradas em memória; nenhum dado de usuário
ou pacote de leitor novo é publicado. Suítes gerais/contratos/identidade/segurança/
billing/Telegram/integridade/carga permanecem exigidas.

Decisões aplicadas: [ADR 002](../adrs/002-data-model-query-strategy.md),
[003](../adrs/003-storage-engine-indexing.md),
[007](../adrs/007-transaction-boundaries-isolation.md),
[008](../adrs/008-failure-modes-timeouts-observability.md),
[009](../adrs/009-consistency-model.md),
[identidade](../adrs/025-oidc-identity-server-sessions.md) e
[billing](../adrs/025-billing-provider.md). Acesso primário é deliberado:
consistência de evidência e revogação precedem roteamento comum de leitura.
