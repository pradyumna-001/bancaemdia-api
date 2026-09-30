# OperaÃ§Ã£o de identidade e sessÃµes

Entrega [#167](https://github.com/pradyumna-001/bancaemdia-api/issues/167), arquitetura [ADR 025](../adrs/025-oidc-identity-server-sessions.md), [contrato](../contracts/identity-session.md). A Fase 1 aprovada Ã© Lightsail/Caddy, teto R$ 200; esta tarefa nÃ£o executa Terraform, cria pool/tenant ou contrata SMTP. A infraestrutura do PR #146 ainda nÃ£o integrada nÃ£o Ã© prÃ©-requisito de cÃ³digo desta branch.

## PendÃªncias externas e responsÃ¡veis

Administrador: confirmar o emissor/plano (recomendaÃ§Ã£o Cognito), regiÃ£o/tenant, domÃ­nio e origens finais; configurar cliente OIDC hospedado com Code, S256 obrigatÃ³rio, callback exato `${AUTH_PUBLIC_URL}/auth/callback`, audience/client ID, RS256, confirmaÃ§Ã£o de e-mail obrigatÃ³ria e recovery/SMTP reais. Confirmar polÃ­tica de rotaÃ§Ã£o/revogaÃ§Ã£o/expiraÃ§Ã£o de refresh, custos de mensagens e limites. Keycloak/Mailpit da CI sÃ£o descartÃ¡veis, nunca serviÃ§os de produÃ§Ã£o.

Mantenedor: revisar/merge da issue/PR. Operador: configurar credenciais/chaves/URLs, migrar e publicar a imagem, executar a jornada contra o emissor aprovado e demonstrar CORS/HTTPS/backup/revogaÃ§Ã£o. Sem essas aÃ§Ãµes nÃ£o hÃ¡ ambiente publicado nem aceite final do frontend #49. As origens reais ainda nÃ£o foram informadas: nÃ£o preencher com domÃ­nio fictÃ­cio que pareÃ§a disponÃ­vel.

## ConfiguraÃ§Ã£o do backend (somente servidor)

| VariÃ¡vel | Valor operacional |
| --- | --- |
| `AUTH_ENABLED` | `true` somente depois de preparar o conjunto completo; padrÃ£o false/rotas 503 |
| `AUTH_DATABASE_URL` | DSN asyncpg da credencial exclusiva de identidade; SecretStr, nunca VITE_ |
| `AUTH_PUBLIC_URL` | Origem HTTPS pÃºblica exata da API, sem path/query/credenciais |
| `AUTH_FRONTEND_ORIGIN` | Origem HTTPS exata do frontend; preferir mesma origem por Caddy; mesma hostname obrigatÃ³ria |
| `AUTH_COOKIE_SECURE` | true; false exclusivamente loopback de testes |
| `AUTH_SIGNING_KEYS_FILE` | JSON server-only de assinatura, montagem somente leitura, proprietÃ¡rio do processo e permissÃ£o 0600 |
| `AUTH_ENCRYPTION_KEYS_FILE` | JSON server-only AES-256, mesmas proteÃ§Ãµes, separado do backup PostgreSQL |
| `JWT_ALGORITHM` | RS256 obrigatÃ³rio |
| `JWT_ISSUER`, `JWT_AUDIENCE` | Issuer e audience do JWT interno da API; operadores/consumidores server-side devem concordar |
| `OIDC_ISSUER` | Issuer HTTPS exato retornado por discovery e nos tokens; nÃ£o endpoint arbitrary do browser |
| `OIDC_CLIENT_ID` | Cliente aprovado no emissor |
| `OIDC_CLIENT_SECRET` | Se cliente confidencial: segredo servidor, Basic auth no token/revocation; nunca bundle |
| `OIDC_SCOPES` | openid email profile por padrÃ£o; Auth0 pode exigir offline_access e permissÃ£o de refresh; Cognito nÃ£o aceita offline_access |
| `OIDC_REVOCATION_ENDPOINT` | Opcional endpoint HTTPS confiÃ¡vel aprovado, para emissor cujo discovery nÃ£o anuncia revocation_endpoint |
| `AUTH_ACCESS_SECONDS` | 300 padrÃ£o; 30â€“900 |
| `AUTH_SESSION_SECONDS` | 604800 padrÃ£o; 300â€“2592000 |
| `AUTH_IDLE_SECONDS` | 43200 padrÃ£o; 300â€“604800; nunca amplia prazo absoluto |

Discovery exige autorizaÃ§Ã£o, token, JWKS, revogaÃ§Ã£o e RS256. ConfiguraÃ§Ã£o invÃ¡lida recusa startup; descoberta indisponÃ­vel/configuraÃ§Ã£o externa faltante produz 503. CORS sÃ³ permite as duas origens exatas. Retain CSP estrita (`default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'` nas respostas privadas); no frontend incluir somente API/origens realmente aprovadas em connect-src, sem relaxar scripts. NÃ£o habilitar captura de headers/corpos no proxy, OTel, APM ou logger HTTP; callback/e-mail action URLs contÃªm segredos temporÃ¡rios.

## Migration e privilÃ©gios

Executar `alembic upgrade head` com papel de migration que possa criar schema/role/policies. Revision `i167session2026` desce de `a9d6e3f1c210`, sem renomear revisÃµes publicadas. Ela cria grupo `bancaemdia_auth` NOLOGIN/NOBYPASSRLS e schema `auth_private`; nÃ£o cria LOGIN/senha.

Operador cria LOGIN exclusivo com senha obtida do gerenciador de segredos, concede apenas membership em `bancaemdia_auth` e USAGE de public se removido pelo ambiente. Essa credencial nÃ£o deve ser membro de `bancaemdia_app`, owner, superuser nem BYPASSRLS. `DATABASE_URL`/rÃ©plica continuam papÃ©is comuns da API; nÃ£o trocar pelo papel de identidade. Nenhum worker genÃ©rico, frontend ou script de exportaÃ§Ã£o recebe `AUTH_DATABASE_URL`/chaves. VÃ­nculos e auditoria nÃ£o sÃ£o editÃ¡veis/apagÃ¡veis pelo papel de identidade; contas inativas exigem aÃ§Ã£o administrativa existente. E-mail coincidente 409 nÃ£o autoriza SQL manual para ligar contas sem prova/documentaÃ§Ã£o de posse.

Rollback: migration recusa downgrade com identidade existente. Desabilitar a feature tambÃ©m reativa o transporte Bearer legado; nÃ£o fazer isso enquanto tokens antigos forem aceitos ou enquanto houver usuÃ¡rios usando sessÃµes. Em rollback operacional manter ledger e chaves, invalidar sessÃµes/refresh conforme procedimento de incidente e usar imagem compatÃ­vel com revogaÃ§Ã£o; jamais apagar schema para contornar a proteÃ§Ã£o.

## Chaves e manutenÃ§Ã£o

No host servidor, gerar material novo com `python -m bancaemdia.auth.keygen /diretorio-privado-aprovado`. O comando gera RSA 3072/AES 256, arquivos exclusivos 0600/umask 077; nÃ£o sobrescreve material existente nem imprime chaves. Monte em API/manutenÃ§Ã£o como somente leitura com proprietÃ¡rio do processo. NÃ£o commitar a pasta nem expor via endpoint/backup de cÃ³digo. JSON de assinatura: `{"active":"kid","keys":{"kid":"PEM privado ativo", "kid-antigo":"PEM pÃºblico retido"}}`; AES: `{"active":"kid","keys":{"kid":"base64 urlsafe de 32 bytes"}}`. As expressÃµes sÃ£o formato, nÃ£o segredos utilizÃ¡veis.

Agendar pelo operador um timer/cron local a cada minuto executando `python -m bancaemdia.auth.maintenance` com a credencial exclusiva e arquivos montados. Ele revoga sessÃµes que ultrapassaram prazo, drena atÃ© 50 revogaÃ§Ãµes externas por execuÃ§Ã£o (SKIP LOCKED), retenta falhas apÃ³s um minuto e limpa fluxos expirados hÃ¡ mais de um dia. Monitorar quantidade/idade/attempts da outbox sem ler/imprimir ciphertext; atraso deixa revogaÃ§Ã£o externa pendente, mas nÃ£o reautoriza capacidade local. Ledger, cookies retirados e auditoria sÃ£o preservados; retenÃ§Ã£o de histÃ³rico deve ser definida e implementada pelo operador com polÃ­tica de privacidade antes de crescimento sustentado, sem apagar revogaÃ§Ã£o que ainda pode ser consultada. Nenhum processo esconde pendÃªncia externa com sucesso fictÃ­cio.

RotaÃ§Ã£o: gerar novo kid Ãºnico e RSA/AES; adicionar ao arquivo mantendo chaves anteriores. Distribuir arquivos atomicamente a todas as instÃ¢ncias/API/manutenÃ§Ã£o e reiniciar coordenadamente para carregar cache. RSA: guardar pÃºblicas anteriores atÃ© todas as sessÃµes/JWTs correspondentes expirarem/revogarem; privados antigos nÃ£o sÃ£o necessÃ¡rios para validar. AES: manter todas as chaves que ainda cifram fluxos/sessÃµes/outbox; retirar apenas apÃ³s recriptografia auditada ou tÃ©rmino/revogaÃ§Ã£o dessas sessÃµes com outbox entregue. Nova prova CSRF vem de GET /auth/session apÃ³s rotaÃ§Ã£o; nÃ£o reciclar prova antiga. Backup criptografado das chaves separado do dump; restore deve testar decriptaÃ§Ã£o/ledger e nÃ£o ressuscitar sessÃ£o revogada. Incidente exige invalidar todas as sessÃµes comprometidas e refresh externo, inclusive os de backups antigos, antes de restaurar serviÃ§o.

## ValidaÃ§Ã£o descartÃ¡vel reproduzÃ­vel

Usar Docker e Python 3.12, dependÃªncias dev e Chromium (`python -m playwright install --with-deps chromium`). Gerar `POSTGRES_PASSWORD`/`KC_BOOTSTRAP_ADMIN_PASSWORD` e `KC_BOOTSTRAP_ADMIN_USERNAME` aleatÃ³rios fora do repositÃ³rio; exportar DATABASE_URL/REPLICA/TEST_DATABASE_URL para PostgreSQL descartÃ¡vel em 127.0.0.1:55432. NÃ£o apontar este teste a produÃ§Ã£o: ele cria usuÃ¡rios, papÃ©is e inativa contas descartÃ¡veis.

`docker compose -f tests/identity/docker-compose.yml up -d --wait postgres mailpit` e `... up -d issuer`. Keycloak 26.7.4 e Mailpit v1.31.3, portas de loopback; issuer nativo em 58081, proxy de falha real do teste em 58080, API em 58000 e frontend mÃ­nimo de aceite em 58001. O proxy encaminha protocolo/tokens/HTML reais e sÃ³ interrompe rede/JWKS; nÃ£o emite identidades de fixture.

Executar `pytest -n 0 tests/identity/journey.py tests/integration/test_identity_db.py --junitxml=identity-results.xml`, depois `python scripts/check_identity_results.py identity-results.xml`. Zero skips exigidos. A CI executa essas mesmas provas e preserva JUnit; imagens nÃ£o sÃ£o subidas em conta live. Encerrar apenas esse projeto com `docker compose -f tests/identity/docker-compose.yml down`.

Provas: cadastro no formulÃ¡rio real, SMTP/confirmar e-mail antes do primeiro usuÃ¡rio interno, recuperaÃ§Ã£o real por e-mail, mesmo usuÃ¡rio numÃ©rico autorizado/RLS entre dois tenants, acesso invÃ¡lido/expirado/inativo, refresh real e rotaÃ§Ã£o, reuso, logout sobre cookie/JWT jÃ¡ emitido, issuer/JWKS offline/recuperados, concorrÃªncia de provisionamento e isolamento dos segredos por papel. Fixtures/mocks de JWT permanecem auxiliares. Homologar tambÃ©m no emissor escolhido antes do lanÃ§amento; prova de Keycloak nÃ£o finge configuraÃ§Ã£o Cognito/Auth0.
