# ADR 025 — identidade OIDC e sessões revogáveis da API

Estado: implementação proposta pela [issue #167](https://github.com/pradyumna-001/bancaemdia-api/issues/167), para destravar [frontend #49](https://github.com/pradyumna-001/bancaemdia-frontend/issues/49). A escolha e configuração do emissor de produção continuam sob responsabilidade do administrador.

## Problema e base

Main `bd055417459f796fed960b5b37efb33a9744419f` integra o validador do PR #71, mas nenhum emissor, provisionamento ou refresh. O seu `sub` é um ID interno decimal positivo, não o sujeito normalmente opaco de OIDC. #130/#145/#147 estão abertos; suas branches não são código integrado. Esta entrega parte diretamente de main e reutiliza o validador, com migração mínima de python-jose para PyJWT, sem importar a cadeia de billing ou infraestrutura. Não depende administrativamente de merge desses PRs. Seus conflitos previsíveis são imports/fixtures JWT, middleware/main, snapshot OpenAPI e o head Alembic; ao integrá-los depois, preservar IDs publicados e testar a composição efetiva.

| Requisito | Existente | Alteração e prova |
| --- | --- | --- |
| Identidade utilizável | JWT fornecido externamente | OIDC hospedado Code + PKCE S256, nonce e state; Chromium + Keycloak real |
| Usuário interno | NumericDate/RS256/sub numérico e RLS | `(issuer,subject)` único e usuário único; trava transacional; concorrência PostgreSQL |
| E-mail | Sem fluxo de confirmação | Emissor confirma; API exige `email_verified=true`; coincidência nunca vincula |
| Renovação | Ausente | Grant real, cookie/geração rotativos e reuso revoga família |
| Recuperação | Ausente | Tela/e-mail reais do emissor; login com intent recover revoga sessões locais anteriores |
| Logout | Expiração do JWT | Ledger consultado em cada acesso e outbox de revogação externa |
| Transporte | Bearer | Cookie HttpOnly Secure Lax, Origin exata e prova CSRF; tokens somente no servidor |

## Decisão implementada

O backend é cliente OIDC e servidor de sessão (BFF). O emissor guarda senhas e opera cadastro, confirmação de e-mail, recuperação e login hospedados. A API valida o ID token RS256, `iss` exato, `aud` do cliente, `exp`, `iat`, `sub`, `azp` quando aplicável e nonce no primeiro grant. O sujeito externo é vinculado ao usuário após identidade e e-mail confirmados, numa transação idempotente. Não há vínculo por e-mail nem autoativação de usuário inativo. Conta legada com e-mail coincidente recebe 409 e exige resolução administrativa explícita, fora desta entrega; não se inventa posse.

A API emite um JWT interno RS256 usando o mesmo `verify_token`: `sub` decimal do usuário, `iss`/`aud` configurados na API, `iat`, `exp`, `sid` e `ver`. `/auth/jwks` só publica RSA pública. O JWT fica criptografado no banco junto do refresh/ID token externo e nunca é entregue ao frontend. Com `AUTH_ENABLED=true`, Bearer também deve ser JWT interno ligado a sessão viva e geração atual. JWT direto do emissor não autoriza a API. O caminho legado Bearer só permanece com a funcionalidade desabilitada; endpoints novos então respondem 503, não sucesso simulado.

Cookie opaco de 256 bits, só hash no índice; dados de sessão/fluxo/outbox usam AES-256-GCM com chave identificada e AAD da finalidade/linha. State/verificador PKCE/nonce são secretos de uso único; o fluxo expira em dez minutos e é consumido atomicamente antes da troca do código. Destino é somente caminho interno armazenado, sem esquema, `//`, barra invertida, controles ou callback da API. Callback indisponível exige novo login; não reaproveita código. Login sempre pede `prompt=login`; logout não promete apagar o cookie SSO do emissor.

Sessões têm limite absoluto, inatividade e acesso curto. Acesso expira em cinco minutos por padrão, sessão em sete dias e inatividade em doze horas. Renovação só ocorre por POST explícito, serializado pelo frontend e pela trava de banco; rotaciona cookie/geração e verifica novamente emissor/sujeito. Reuso de cookie retirado revoga sua família. Expiração ou usuário inativo bloqueiam cada acesso. Logout local é imediato também sobre JWT já emitido, independentemente de disponibilidade do emissor. Revogação do refresh externo fica numa outbox durável e é tentada após logout e pela manutenção periódica.

Quando um grant já rotacionou o refresh externo e JWKS falha, a resposta fica criptografada como pendente, sem novo acesso autorizado; retry verifica essa resposta sem consumir novamente o refresh anterior. Falha do token endpoint não descarta a sessão; 503 pede nova tentativa. JWKS externo quente só vale dentro do TTL de cinco minutos; após TTL sem atualização a validação externa falha 503. Busca fria/unknown kid conserva cooldown antiamplificação de trinta segundos. Acesso interno previamente validado continua conforme seus próprios limites/ledger.

`auth_private` só é acessível por credencial separada membro de `bancaemdia_auth` (NOLOGIN/NOBYPASSRLS). Esse papel pode criar/ler usuário interno e operar sessões, mas não ler apostas, alterar usuário, editar vínculos ou apagar auditoria. O papel comum `bancaemdia_app` não lê os segredos. Provisionamento não concede trial/assinatura e não implementa billing. Contrato 401 de identidade permanece distinto de 402/account_read_only; autorização comercial pertence a cada operação, inclusive export/import/upload, e não pode ser inferida do verbo HTTP. Esses gates comerciais não estão integrados na main desta base.

## Emissor de produção: decisão encaminhada ao administrador

Consulta de preços em 29/09/2026; MAU são usuários ativos mensais, não total cadastrado.

| Opção | Compatibilidade | Operação e custo inicial | Consequência |
| --- | --- | --- | --- |
| Cognito | Login hospedado OIDC/RS256/PKCE, refresh e revogação; cadastro/e-mail/recovery gerenciados | Lite/Essentials oferecem 10 mil MAU gratuitos para credenciais diretas/social; envio de mensagens e extras podem cobrar | Recomendação para a Fase 1 AWS; não vale extrapolar esse free tier a federação OIDC/SAML (50 MAU) ou Plus |
| Auth0 | OIDC/PKCE e Universal Login; offline_access/refresh precisam configuração do cliente | Free até 25 mil MAU; tabela Essentials começa US$ 35/mês em 500 MAU | Operação gerenciada, mas upgrade pago teria impacto relevante no teto R$ 200 da Fase 1 |
| Keycloak | Protocolo comprovado por esta CI, cadastro, SMTP, recovery, rotação/revogação | Software aberto; hospedagem, memória, SMTP, patches, backup e disponibilidade ficam com o operador | Usá-lo no sandbox não decide adotá-lo em produção; requer dimensionamento junto da instância de 4 GB |

Fontes primárias: [Cognito pricing](https://aws.amazon.com/cognito/pricing/), [Auth0 pricing](https://auth0.com/pricing/), [Keycloak documentation](https://www.keycloak.org/documentation). Inferência: Cognito tende a ter melhor relação custo/operação para a Fase 1 aprovada; não é autorização para criar conta, pool, contratar ou provisionar. O administrador deve confirmar emissor, região/tenant, operação de e-mail/recovery, plano e limites. A API usa discovery configurado e endpoint de revogação explícito se o emissor não o anunciar.

## Consequências e limites

Consultar o ledger por pedido aumenta a dependência do banco e torna revogação verificável. Chaves de assinatura/criptografia exigem backup separado e rotação coordenada; perder chaves AES perde sessões, não a identidade nem as apostas. Rollback da migration recusa apagar um esquema com vínculos existentes. Sem emissor/origens/cliente/chaves/SMTP de produção aprovados, a feature continua desabilitada. Não existe domínio publicado nem serviço contratado por esta entrega.

A jornada obrigatória usa Keycloak, Mailpit, Chromium e PostgreSQL descartáveis, sem cobrança live. Não substitui a homologação do emissor que o administrador escolher, nem merge/deploy. [Contrato do frontend](../contracts/identity-session.md) e [operação](../runbooks/identity-session.md) detalham essas fronteiras.

Referências de protocolo: [OIDC Core](https://openid.net/specs/openid-connect-core-1_0.html#IDTokenValidation), [OAuth Security BCP](https://www.rfc-editor.org/rfc/rfc9700.html).
