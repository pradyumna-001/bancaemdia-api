# Identidade e sessão — contrato implementado da issue #167

Destino: [frontend #49](https://github.com/pradyumna-001/bancaemdia-frontend/issues/49). OpenAPI gerado em [`tests/contract/schemas/openapi.json`](../../tests/contract/schemas/openapi.json), referência [`docs/API.md`](../API.md) e `/openapi.json` na API. A feature exige configuração completa e `AUTH_ENABLED=true`; sem isso as rotas de identidade retornam 503. A presença do PR não significa backend integrado nem ambiente publicado.

## Jornada e responsabilidades

Frontend navega para `/auth/start?return_to=/caminho&intent=login|signup|recover`. A API grava fluxo de dez minutos, vincula state a cookie HttpOnly e envia Code+PKCE S256+nonce ao login hospedado. No emissor o usuário usa os links reais de cadastro ou recuperação; a API não guarda senhas, envia e-mails nem simula esses serviços. Cadastro só chega ao callback depois da confirmação; API também exige `email_verified=true` no ID token.

Callback troca código/verificador no servidor, valida identidade e cria/vincula um usuário interno idempotentemente. O par externo `(issuer,sub)` nunca vira diretamente `usuario_id`. E-mail coincidente com outra conta recebe 409, sem auto-link. Usuário inativo recebe 401. Cadastro não concede trial nem assinatura. Recovery precisa retornar pelo fluxo `intent=recover`: após login verificado, a API revoga sessões locais anteriores da identidade. Recuperação iniciada diretamente no provedor, fora desse fluxo, não notifica automaticamente esta API.

## Operações

| Método/rota | Entrada/transporte | Resultado real |
| --- | --- | --- |
| GET `/auth/start` | `return_to` caminho interno; `intent` login/signup/recover | 302 Location do emissor; cookie de fluxo HttpOnly Lax, dez minutos |
| GET `/auth/callback` | `state`, `code` e cookie do mesmo navegador | 302 ao frontend configurado + caminho armazenado; cookie de sessão; fluxo consumido e cookie de fluxo apagado |
| GET `/auth/session` | Cookie de sessão, `credentials: include` | 200 `SessionStatus`; identidade, versão, CSRF e prazos, sem token |
| POST `/auth/refresh` | Cookie + Origin exata + `X-CSRF-Token` da sessão atual | Grant real, 200 SessionStatus, novo cookie/prova/geração; identidade preservada |
| POST `/auth/logout` | Cookie + Origin exata + `X-CSRF-Token`; `all_sessions=true` opcional | 200 `{"logged_out":true}` somente após revogação local persistida; apaga cookie e agenda revogação do refresh externo |
| GET `/auth/jwks` | Público | 200 RSA pública com kid/alg/use/n/e/key_ops; nunca chave privada |

Não há `/signup`, `/forgot-password` ou `/token` fingindo sucesso. A interface do emissor é o caminho concreto de cadastro/recovery. Logout da API não promete remover cookie SSO do emissor; início sempre força login (`prompt=login`). O serviço de manutenção retenta revogação do refresh externo quando o emissor estiver disponível.

`SessionStatus`: `usuario_id`, `nome`, `email`, `session_version` (`UUID:geração`), `csrf_token`, `access_expires_at`, `session_expires_at` e `refresh_required`. Datas são RFC3339. Identidade sem assinatura continua autenticada. Os gates comerciais por operação, quando integrados, podem negar com 402/account_read_only; não limpam a sessão e não são substituídos por esta implementação. Importação, exportação, upload e outras operações precisam da própria política, independentemente de usarem GET/POST.

## Claims, armazenamento e efeitos

ID token externo: RS256, issuer exato da configuração OIDC, audience client ID, exp/iat/sub, azp quando requerido, nonce no login e e-mail confirmado booleano. Token externo nunca é aceito diretamente como identidade numérica da API.

JWT interno: RS256, `iss=JWT_ISSUER`, `aud=JWT_AUDIENCE`, `sub` decimal positivo do usuário interno, `exp`, `iat`, `sid` e `ver`. Reutiliza o validador existente e exige sessão ativa, geração atual e usuário ativo a cada pedido. Token, refresh externo e ID token ficam criptografados no servidor. Logout/recovery/reuso/expiração bloqueiam a capacidade local inclusive sobre JWT já emitido. JWT da geração anterior também perde validade após refresh.

Navegador recebe apenas cookie opaco, HttpOnly Secure SameSite=Lax Path=/, sem Domain, nome `__Host-bancaemdia_session`. Somente teste loopback usa `bancaemdia_session` sem Secure. O frontend guarda identidade/prova CSRF apenas em memória; não há tokens em localStorage/sessionStorage, bundle, VITE_ ou URL de retorno. State/código só transitam no protocolo de callback, com `no-referrer` e logs sanitizados. Respostas privadas usam `Cache-Control: private, no-store` e CSP estrita.

## Estados, erros e renovação

| Status/code | Interpretação e ação |
| --- | --- |
| 400 `invalid_destination`, `invalid_flow` | Entrada inválida, state ausente/expirado/repetido ou cookie de outro navegador; iniciar novo fluxo |
| 401 `not_authenticated`, `session_expired`, `identity_rejected`, `account_inactive` | Sem sessão autorizada; encerrar estado local e solicitar login |
| 401 `access_expired` | Consultar `/auth/session`; se vivo, realizar refresh único e repetir o pedido uma vez |
| 401 `refresh_reused` | Cookie anterior reapresentado; família revogada; login novo, sem loop de retry |
| 403 `email_unconfirmed` | Nenhum novo acesso autorizado; concluir confirmação pelo emissor |
| 403 `csrf_failed`, `origin_not_allowed` | Cookie/mutação sem origem/prova válida ou origem fora da lista; corrigir integração, não tratar como billing |
| 409 `identity_conflict` | Conta existente não vinculada por e-mail; resolução administrativa de identidade, sem associação automática |
| 429 `error=rate_limited`, `retry_after` | Observar Retry-After; não disparar repetição paralela |
| 503 `identity_not_configured`, `refresh_not_available` | Operador precisa completar configuração/refresh do cliente |
| 503 `issuer_unavailable` | Preservar sessão, exibir indisponibilidade e retry limitado; callback consumido exige novo login |
| 503 genérico | Banco/dependência indisponível; não afirmar logout concluído sem resposta válida |

Em `/auth/*`, falhas de identidade usam `{"detail": "Identity request could not be completed", "code": "..."}`. Nas rotas protegidas existentes, o corpo `ErrorResponse` é preservado (`detail` sem propriedade nova); o código vem no header adicional `X-Auth-Error`, exposto pelo CORS; erros gerais seguem `ErrorResponse`, validação 422 segue o contrato FastAPI. 401 tem WWW-Authenticate. Nunca interpretar 402 como expiração de login.

Refresh é explícito e serializado: um único pedido por sessão entre abas (Web Locks/BroadcastChannel podem coordenar sem guardar segredo). Em cada renovação substituir versão/prova a partir da resposta; duas renovações simultâneas não são uma estratégia suportada. Reuso posterior do cookie antigo revoga a família. Mesmo no refresh a sessão precisa estar dentro dos limites absoluto/inatividade. Padrões: acesso 5 min, sessão 7 dias, inatividade desde última renovação 12 h; valores efetivos vêm da resposta/configuração. GET `/auth/session` continua 200 com refresh_required após expirar acesso, enquanto a sessão existir.

Se o emissor rotacionar refresh e JWKS falhar, a API retém o grant criptografado sem autorizar novo acesso; retry valida o pendente. Se ele próprio expirar antes de validar, login novo é necessário. JWKS frio indisponível é 503; antiamplificação limita busca por trinta segundos. Não confundir 503 com identidade rejeitada.

## Integração do navegador

API e frontend devem usar mesma origem em produção via reverse proxy; configurações também aceitam mesma hostname com portas distintas, usadas no sandbox. HTTPS e Secure são obrigatórios fora de loopback. `AUTH_PUBLIC_URL`/`AUTH_FRONTEND_ORIGIN` são origens exatas aprovadas; CORS credenciado só permite essas origens, sem `*`. O preflight admite Authorization, Content-Type, X-CSRF-Token, X-Request-ID e X-Read-Replica. Toda mutação por cookie exige Origin exata e CSRF, incluindo logout/refresh. Bearer possui validação própria e não pode cair para cookie se o header for inválido.

Ao iniciar, retornar do callback ou receber 401, consultar a sessão. Antes de trocar/encerrar usuário, cancelar requests antigos (AbortController), interromper refresh/retries, descartar caches/query state e dados de apostas/upload/exports/revisões/painel do usuário anterior. Cada request captura `session_version`; ignore resposta que pertença a versão anterior mesmo que cancelamento tenha chegado tarde. Após refresh, atualizar a versão e repetir apenas requests ainda pertencentes à sessão. Após logout/recovery, não reutilizar resposta, cookie manual ou prova anterior. Não armazenar dados privados em Service Worker/cache público.

## Estado de publicação

Esta implementação permite ao frontend implementar o contrato sem inventar autenticação. O aceite de frontend #49 ainda exige revisão/merge, emissor escolhido/configurado pelo administrador, origens/callback/SMTP/chaves aprovados, deploy e homologação real do ambiente publicado. Sandbox Keycloak não é um tenant Cognito/Auth0 de produção. Nenhum serviço pago ou domínio foi provisionado nesta tarefa.
