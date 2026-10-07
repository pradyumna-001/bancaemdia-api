# Operação de identidade e sessões

Entrega [#167](https://github.com/pradyumna-001/bancaemdia-api/issues/167), arquitetura [ADR 025](../adrs/025-oidc-identity-server-sessions.md), [contrato](../contracts/identity-session.md). A Fase 1 aprovada é Lightsail/Caddy, teto R$ 200; esta tarefa não executa Terraform, cria pool/tenant ou contrata SMTP. A infraestrutura do PR #146 ainda não integrada não é pré-requisito de código desta branch.

## Pendências externas e responsáveis

Administrador: confirmar o emissor/plano (recomendação Cognito), região/tenant, domínio e origens finais; configurar cliente OIDC hospedado com Code, S256 obrigatório, callback exato `${AUTH_PUBLIC_URL}/auth/callback`, audience/client ID, RS256, confirmação de e-mail obrigatória e recovery/SMTP reais. Confirmar política de rotação/revogação/expiração de refresh, custos de mensagens e limites. Keycloak/Mailpit da CI são descartáveis, nunca serviços de produção.

Mantenedor: revisar/merge da issue/PR. Operador: configurar credenciais/chaves/URLs, migrar e publicar a imagem, executar a jornada contra o emissor aprovado e demonstrar CORS/HTTPS/backup/revogação. Sem essas ações não há ambiente publicado nem aceite final do frontend #49. As origens reais ainda não foram informadas: não preencher com domínio fictício que pareça disponível.

## Configuração do backend (somente servidor)

| Variável | Valor operacional |
| --- | --- |
| `AUTH_ENABLED` | `true` somente depois de preparar o conjunto completo; padrão false/rotas 503 |
| `AUTH_DATABASE_URL` | DSN asyncpg da credencial exclusiva de identidade; SecretStr, nunca VITE_ |
| `AUTH_PUBLIC_URL` | Origem HTTPS pública exata da API, sem path/query/credenciais |
| `AUTH_FRONTEND_ORIGIN` | Origem HTTPS exata do frontend; preferir mesma origem por Caddy; mesma hostname obrigatória |
| `AUTH_COOKIE_SECURE` | true; false exclusivamente loopback de testes |
| `AUTH_SIGNING_KEYS_FILE` | JSON server-only de assinatura, montagem somente leitura, proprietário do processo e permissão 0600 |
| `AUTH_ENCRYPTION_KEYS_FILE` | JSON server-only AES-256, mesmas proteções, separado do backup PostgreSQL |
| `JWT_ALGORITHM` | RS256 obrigatório |
| `JWT_ISSUER`, `JWT_AUDIENCE` | Issuer e audience do JWT interno da API; operadores/consumidores server-side devem concordar |
| `OIDC_ISSUER` | Issuer HTTPS exato retornado por discovery e nos tokens; não endpoint arbitrary do browser |
| `OIDC_CLIENT_ID` | Cliente aprovado no emissor |
| `OIDC_CLIENT_SECRET` | Se cliente confidencial: segredo servidor, Basic auth no token/revocation; nunca bundle |
| `OIDC_SCOPES` | openid email profile por padrão; Auth0 pode exigir offline_access e permissão de refresh; Cognito não aceita offline_access |
| `OIDC_REVOCATION_ENDPOINT` | Opcional endpoint HTTPS confiável aprovado, para emissor cujo discovery não anuncia revocation_endpoint |
| `AUTH_ACCESS_SECONDS` | 300 padrão; 30–900 |
| `AUTH_SESSION_SECONDS` | 604800 padrão; 300–2592000 |
| `AUTH_IDLE_SECONDS` | 43200 padrão; 300–604800; nunca amplia prazo absoluto |

Discovery exige autorização, token, JWKS, revogação e RS256. Configuração inválida recusa startup; descoberta indisponível/configuração externa faltante produz 503. CORS só permite as duas origens exatas. Preservar CSP estrita (`default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'` nas respostas privadas); no frontend incluir somente API/origens realmente aprovadas em connect-src, sem relaxar scripts. Não habilitar captura de headers/corpos no proxy, OTel, APM ou logger HTTP; callback/e-mail action URLs contêm segredos temporários.

## Migration e privilégios

Executar `alembic upgrade head` com papel de migration que possa criar schema/role/policies. Revision `i167session2026` desce de `a9d6e3f1c210`, sem renomear revisões publicadas. Ela cria grupo `bancaemdia_auth` NOLOGIN/NOBYPASSRLS e schema `auth_private`; não cria LOGIN/senha.

Operador cria LOGIN exclusivo com senha obtida do gerenciador de segredos, concede apenas membership em `bancaemdia_auth` e USAGE de public se removido pelo ambiente. Essa credencial não deve ser membro de `bancaemdia_app`, owner, superuser nem BYPASSRLS. O startup e o comando de manutenção verificam os privilégios efetivos e recusam uma credencial ampla, inclusive herdada de outro papel. `DATABASE_URL`/réplica continuam papéis comuns da API; não trocar pelo papel de identidade. Nenhum worker genérico, frontend ou script de exportação recebe `AUTH_DATABASE_URL`/chaves. Vínculos e auditoria não são editáveis/apagáveis pelo papel de identidade; contas inativas exigem ação administrativa existente. E-mail coincidente 409 não autoriza SQL manual para ligar contas sem prova/documentação de posse.

Rollback: migration recusa downgrade com identidade existente. Desabilitar a feature também reativa o transporte Bearer legado; não fazer isso enquanto tokens antigos forem aceitos ou enquanto houver usuários usando sessões. Em rollback operacional manter ledger e chaves, invalidar sessões/refresh conforme procedimento de incidente e usar imagem compatível com revogação; jamais apagar schema para contornar a proteção.

## Chaves e manutenção

No host servidor, gerar material novo com `python -m bancaemdia.auth.keygen /diretorio-privado-aprovado`. O comando gera RSA 3072/AES 256, arquivos exclusivos 0600/umask 077; não sobrescreve material existente nem imprime chaves. Monte em API/manutenção como somente leitura com proprietário do processo. Não commitar a pasta nem expor via endpoint/backup de código. JSON de assinatura: `{"active":"kid","keys":{"kid":"PEM privado ativo", "kid-antigo":"PEM público retido"}}`; AES: `{"active":"kid","keys":{"kid":"base64 urlsafe de 32 bytes"}}`. As expressões são formato, não segredos utilizáveis.

Agendar pelo operador um timer/cron local a cada minuto executando `python -m bancaemdia.auth.maintenance` com a credencial exclusiva e arquivos montados. Ele revoga sessões que ultrapassaram prazo, drena até 50 revogações externas por execução (SKIP LOCKED), retenta falhas após um minuto e limpa fluxos expirados há mais de um dia. Monitorar quantidade/idade/attempts da outbox sem ler/imprimir ciphertext; atraso deixa revogação externa pendente, mas não reautoriza capacidade local. Ledger, cookies retirados e auditoria são preservados; retenção de histórico deve ser definida e implementada pelo operador com política de privacidade antes de crescimento sustentado, sem apagar revogação que ainda pode ser consultada. Nenhum processo esconde pendência externa com sucesso fictício.

Rotação: gerar novo kid único e RSA/AES; adicionar ao arquivo mantendo chaves anteriores. Distribuir arquivos atomicamente a todas as instâncias/API/manutenção e reiniciar coordenadamente para carregar cache. RSA: guardar públicas anteriores até todas as sessões/JWTs correspondentes expirarem/revogarem; privados antigos não são necessários para validar. AES: manter todas as chaves que ainda cifram fluxos/sessões/outbox; retirar apenas após recriptografia auditada ou término/revogação dessas sessões com outbox entregue. Nova prova CSRF vem de GET /auth/session após rotação; não reciclar prova antiga. Backup criptografado das chaves separado do dump; restore deve testar decriptação/ledger e não ressuscitar sessão revogada. Incidente exige invalidar todas as sessões comprometidas e refresh externo, inclusive os de backups antigos, antes de restaurar serviço.

## Validação descartável reproduzível

Usar Docker e Python 3.12, dependências dev e Chromium (`python -m playwright install --with-deps chromium`). Gerar `POSTGRES_PASSWORD`/`KC_BOOTSTRAP_ADMIN_PASSWORD` e `KC_BOOTSTRAP_ADMIN_USERNAME` aleatórios fora do repositório; exportar DATABASE_URL/REPLICA/TEST_DATABASE_URL para PostgreSQL descartável em 127.0.0.1:55432. Não apontar este teste a produção: ele cria usuários, papéis e inativa contas descartáveis.

`docker compose -f tests/identity/docker-compose.yml up -d --wait postgres mailpit` e `... up -d issuer`. Keycloak 26.7.4 e Mailpit v1.31.3, portas de loopback; issuer nativo em 58081, proxy de falha real do teste em 58080, API em 58000 e frontend mínimo de aceite em 58001. O proxy encaminha protocolo/tokens/HTML reais e só interrompe rede/JWKS; não emite identidades de fixture.

Executar `pytest -n 0 tests/identity/journey.py tests/integration/test_identity_db.py --junitxml=identity-results.xml`, depois `python scripts/check_identity_results.py identity-results.xml`. Zero skips exigidos. A CI executa essas mesmas provas e preserva JUnit; imagens não são subidas em conta live. Encerrar apenas esse projeto com `docker compose -f tests/identity/docker-compose.yml down`.

Provas: cadastro no formulário real, SMTP/confirmar e-mail antes do primeiro usuário interno, recuperação real por e-mail, mesmo usuário numérico autorizado/RLS entre dois tenants, acesso inválido/expirado/inativo, refresh real e rotação, reuso, logout sobre cookie/JWT já emitido, issuer/JWKS offline/recuperados, concorrência de provisionamento e isolamento dos segredos por papel. Fixtures/mocks de JWT permanecem auxiliares. Homologar também no emissor escolhido antes do lançamento; prova de Keycloak não finge configuração Cognito/Auth0.
