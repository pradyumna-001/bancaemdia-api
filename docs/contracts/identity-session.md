# Identidade e sessÃ£o â€” contrato implementado da issue #167

Destino: [frontend #49](https://github.com/pradyumna-001/bancaemdia-frontend/issues/49). OpenAPI gerado em [`tests/contract/schemas/openapi.json`](../../tests/contract/schemas/openapi.json), referÃªncia [`docs/API.md`](../API.md) e `/openapi.json` na API. A feature exige configuraÃ§Ã£o completa e `AUTH_ENABLED=true`; sem isso as rotas de identidade retornam 503. A presenÃ§a do PR nÃ£o significa backend integrado nem ambiente publicado.

## Jornada e responsabilidades

Frontend navega para `/auth/start?return_to=/caminho&intent=login|signup|recover`. A API grava fluxo de dez minutos, vincula state a cookie HttpOnly e envia Code+PKCE S256+nonce ao login hospedado. No emissor o usuÃ¡rio usa os links reais de cadastro ou recuperaÃ§Ã£o; a API nÃ£o guarda senhas, envia e-mails nem simula esses serviÃ§os. Cadastro sÃ³ chega ao callback depois da confirmaÃ§Ã£o; API tambÃ©m exige `email_verified=true` no ID token.

Callback troca cÃ³digo/verificador no servidor, valida identidade e cria/vincula um usuÃ¡rio interno idempotentemente. O par externo `(issuer,sub)` nunca vira diretamente `usuario_id`. E-mail coincidente com outra conta recebe 409, sem auto-link. UsuÃ¡rio inativo recebe 401. Cadastro nÃ£o concede trial nem assinatura. Recovery precisa retornar pelo fluxo `intent=recover`: apÃ³s login verificado, a API revoga sessÃµes locais anteriores da identidade. RecuperaÃ§Ã£o iniciada diretamente no provedor, fora desse fluxo, nÃ£o notifica automaticamente esta API.

## OperaÃ§Ãµes

| MÃ©todo/rota | Entrada/transporte | Resultado real |
| --- | --- | --- |
| GET `/auth/start` | `return_to` caminho interno; `intent` login/signup/recover | 302 Location do emissor; cookie de fluxo HttpOnly Lax, dez minutos |
| GET `/auth/callback` | `state`, `code` e cookie do mesmo navegador | 302 ao frontend configurado + caminho armazenado; cookie de sessÃ£o; fluxo consumido e cookie de fluxo apagado |
| GET `/auth/session` | Cookie de sessÃ£o, `credentials: include` | 200 `SessionStatus`; identidade, versÃ£o, CSRF e prazos, sem token |
| POST `/auth/refresh` | Cookie + Origin exata + `X-CSRF-Token` da sessÃ£o atual | Grant real, 200 SessionStatus, novo cookie/prova/geraÃ§Ã£o; identidade preservada |
| POST `/auth/logout` | Cookie + Origin exata + `X-CSRF-Token`; `all_sessions=true` opcional | 200 `{"logged_out":true}` somente apÃ³s revogaÃ§Ã£o local persistida; apaga cookie e agenda revogaÃ§Ã£o do refresh externo |
| GET `/auth/jwks` | PÃºblico | 200 RSA pÃºblica com kid/alg/use/n/e/key_ops; nunca chave privada |

NÃ£o hÃ¡ `/signup`, `/forgot-password` ou `/token` fingindo sucesso. A interface do emissor Ã© o caminho concreto de cadastro/recovery. Logout da API nÃ£o promete remover cookie SSO do emissor; inÃ­cio sempre forÃ§a login (`prompt=login`). O serviÃ§o de manutenÃ§Ã£o retenta revogaÃ§Ã£o do refresh externo quando o emissor estiver disponÃ­vel.

`SessionStatus`: `usuario_id`, `nome`, `email`, `session_version` (`UUID:geraÃ§Ã£o`), `csrf_token`, `access_expires_at`, `session_expires_at` e `refresh_required`. Datas sÃ£o RFC3339. Identidade sem assinatura continua autenticada. Os gates comerciais por operaÃ§Ã£o, quando integrados, podem negar com 402/account_read_only; nÃ£o limpam a sessÃ£o e nÃ£o sÃ£o substituÃ­dos por esta implementaÃ§Ã£o. ImportaÃ§Ã£o, exportaÃ§Ã£o, upload e outras operaÃ§Ãµes precisam da prÃ³pria polÃ­tica, independentemente de usarem GET/POST.

## Claims, armazenamento e efeitos

ID token externo: RS256, issuer exato da configuraÃ§Ã£o OIDC, audience client ID, exp/iat/sub, azp quando requerido, nonce no login e e-mail confirmado booleano. Token externo nunca Ã© aceito diretamente como identidade numÃ©rica da API.

JWT interno: RS256, `iss=JWT_ISSUER`, `aud=JWT_AUDIENCE`, `sub` decimal positivo do usuÃ¡rio interno, `exp`, `iat`, `sid` e `ver`. Reutiliza o validador existente e exige sessÃ£o ativa, geraÃ§Ã£o atual e usuÃ¡rio ativo a cada pedido. Token, refresh externo e ID token ficam criptografados no servidor. Logout/recovery/reuso/expiraÃ§Ã£o bloqueiam a capacidade local inclusive sobre JWT jÃ¡ emitido. JWT da geraÃ§Ã£o anterior tambÃ©m perde validade apÃ³s refresh.

Navegador recebe apenas cookie opaco, HttpOnly Secure SameSite=Lax Path=/, sem Domain, nome `__Host-bancaemdia_session`. Somente teste loopback usa `bancaemdia_session` sem Secure. O frontend guarda identidade/prova CSRF apenas em memÃ³ria; nÃ£o hÃ¡ tokens em localStorage/sessionStorage, bundle, VITE_ ou URL de retorno. State/cÃ³digo sÃ³ transitam no protocolo de callback, com `no-referrer` e logs sanitizados. Respostas privadas usam `Cache-Control: private, no-store` e CSP estrita.

## Estados, erros e renovaÃ§Ã£o

| Status/code | InterpretaÃ§Ã£o e aÃ§Ã£o |
| --- | --- |
| 400 `invalid_destination`, `invalid_flow` | Entrada invÃ¡lida, state ausente/expirado/repetido ou cookie de outro navegador; iniciar novo fluxo |
| 401 `not_authenticated`, `session_expired`, `identity_rejected`, `account_inactive` | Sem sessÃ£o autorizada; encerrar estado local e solicitar login |
| 401 `access_expired` | Consultar `/auth/session`; se vivo, realizar refresh Ãºnico e repetir o pedido uma vez |
| 401 `refresh_reused` | Cookie anterior reapresentado; famÃ­lia revogada; login novo, sem loop de retry |
| 403 `email_unconfirmed` | Nenhum novo acesso autorizado; concluir confirmaÃ§Ã£o pelo emissor |
| 403 `csrf_failed`, `origin_not_allowed` | Cookie/mutaÃ§Ã£o sem origem/prova vÃ¡lida ou origem fora da lista; corrigir integraÃ§Ã£o, nÃ£o tratar como billing |
| 409 `identity_conflict` | Conta existente nÃ£o vinculada por e-mail; resoluÃ§Ã£o administrativa de identidade, sem associaÃ§Ã£o automÃ¡tica |
| 429 `error=rate_limited`, `retry_after` | Observar Retry-After; nÃ£o disparar repetiÃ§Ã£o paralela |
| 503 `identity_not_configured`, `refresh_not_available` | Operador precisa completar configuraÃ§Ã£o/refresh do cliente |
| 503 `issuer_unavailable` | Preservar sessÃ£o, exibir indisponibilidade e retry limitado; callback consumido exige novo login |
| 503 genÃ©rico | Banco/dependÃªncia indisponÃ­vel; nÃ£o afirmar logout concluÃ­do sem resposta vÃ¡lida |

Em `/auth/*`, falhas de identidade usam `{"detail": "Identity request could not be completed", "code": "..."}`. Nas rotas protegidas existentes, o corpo `ErrorResponse` é preservado (`detail` sem propriedade nova); o código vem no header adicional `X-Auth-Error`, exposto pelo CORS; erros gerais seguem `ErrorResponse`, validaÃ§Ã£o 422 segue o contrato FastAPI. 401 tem WWW-Authenticate. Nunca interpretar 402 como expiraÃ§Ã£o de login.

Refresh Ã© explÃ­cito e serializado: um Ãºnico pedido por sessÃ£o entre abas (Web Locks/BroadcastChannel podem coordenar sem guardar segredo). Em cada renovaÃ§Ã£o substituir versÃ£o/prova a partir da resposta; duas renovaÃ§Ãµes simultÃ¢neas nÃ£o sÃ£o uma estratÃ©gia suportada. Reuso posterior do cookie antigo revoga a famÃ­lia. Mesmo no refresh a sessÃ£o precisa estar dentro dos limites absoluto/inatividade. PadrÃµes: acesso 5 min, sessÃ£o 7 dias, inatividade desde Ãºltima renovaÃ§Ã£o 12 h; valores efetivos vÃªm da resposta/configuraÃ§Ã£o. GET `/auth/session` continua 200 com refresh_required apÃ³s expirar acesso, enquanto a sessÃ£o existir.

Se o emissor rotacionar refresh e JWKS falhar, a API retÃ©m o grant criptografado sem autorizar novo acesso; retry valida o pendente. Se ele prÃ³prio expirar antes de validar, login novo Ã© necessÃ¡rio. JWKS frio indisponÃ­vel Ã© 503; antiamplificaÃ§Ã£o limita busca por trinta segundos. NÃ£o confundir 503 com identidade rejeitada.

## IntegraÃ§Ã£o do navegador

API e frontend devem usar mesma origem em produÃ§Ã£o via reverse proxy; configuraÃ§Ãµes tambÃ©m aceitam mesma hostname com portas distintas, usadas no sandbox. HTTPS e Secure sÃ£o obrigatÃ³rios fora de loopback. `AUTH_PUBLIC_URL`/`AUTH_FRONTEND_ORIGIN` sÃ£o origens exatas aprovadas; CORS credenciado sÃ³ permite essas origens, sem `*`. O preflight admite Authorization, Content-Type, X-CSRF-Token, X-Request-ID e X-Read-Replica. Toda mutaÃ§Ã£o por cookie exige Origin exata e CSRF, incluindo logout/refresh. Bearer possui validaÃ§Ã£o prÃ³pria e nÃ£o pode cair para cookie se o header for invÃ¡lido.

Ao iniciar, retornar do callback ou receber 401, consultar a sessÃ£o. Antes de trocar/encerrar usuÃ¡rio, cancelar requests antigos (AbortController), interromper refresh/retries, descartar caches/query state e dados de apostas/upload/exports/revisÃµes/painel do usuÃ¡rio anterior. Cada request captura `session_version`; ignore resposta que pertenÃ§a a versÃ£o anterior mesmo que cancelamento tenha chegado tarde. ApÃ³s refresh, atualizar a versÃ£o e repetir apenas requests ainda pertencentes Ã  sessÃ£o. ApÃ³s logout/recovery, nÃ£o reutilizar resposta, cookie manual ou prova anterior. NÃ£o armazenar dados privados em Service Worker/cache pÃºblico.

## Estado de publicaÃ§Ã£o

Esta implementaÃ§Ã£o permite ao frontend implementar o contrato sem inventar autenticaÃ§Ã£o. O aceite de frontend #49 ainda exige revisÃ£o/merge, emissor escolhido/configurado pelo administrador, origens/callback/SMTP/chaves aprovados, deploy e homologaÃ§Ã£o real do ambiente publicado. Sandbox Keycloak nÃ£o Ã© um tenant Cognito/Auth0 de produÃ§Ã£o. Nenhum serviÃ§o pago ou domÃ­nio foi provisionado nesta tarefa.
