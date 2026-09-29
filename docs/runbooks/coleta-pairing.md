# Pareamento da coleta por instalação — #107

O servidor identifica a instalação e o usuário pelo token apresentado. O UUID
opaco enviado pelo cliente serve apenas para reconexão; não identifica tenant,
casa de apostas, login da casa ou navegador. Duas instalações do mesmo usuário
possuem credenciais independentes. A extensão e sua interface ficam em outro
repositório; sessões e o envelope v2 são escopo da #108.

## Contrato e uso

Todos os caminhos abaixo exigem **HTTPS**, inclusive em desenvolvimento. Use TLS
local ou um proxy local de confiança. A aplicação usa o scheme ASGI; não confia
diretamente em `X-Forwarded-Proto`. Configure os IPs de proxies confiáveis no
servidor ASGI e bloqueie acesso direto ao backend. Não habilite confiança global
em headers encaminhados por clientes. Queries são recusadas nesses endpoints;
segredos pertencem ao corpo JSON ou ao header documentado. Respostas têm
`Cache-Control: no-store`; redirecionamentos são convertidos em erro sem Location.

| Operação | Autenticação | Resultado |
| --- | --- | --- |
| `POST /api/v1/coleta/pairing-codes` | JWT do usuário | 201: `codigo`, `expira_em` |
| `POST /api/v1/coleta/pairing-exchange` | Código descartável, sem JWT | 200: `instalacao_id`, `token` |
| `GET /api/v1/coleta/installations` | JWT | Instalações próprias e timestamps, sem hash/segredo |
| `POST /api/v1/coleta/installations/{instalacao_id}/rotate` | JWT do proprietário | Nova credencial; anterior invalidada atomicamente |
| `DELETE /api/v1/coleta/installations/{instalacao_id}` | JWT do proprietário | 204; somente essa instalação revogada |
| `GET /api/v1/coleta/status` | `X-Coleta-Token` | Identidade validada no primário |
| `POST /api/v1/coleta` ou `/coleta` | `X-Coleta-Token` | Contrato de coleta v1 preservado |

Exemplo **sintético**, sem credencial utilizável:

```json
{
  "codigo": "codigo-retornado-uma-unica-vez",
  "instalacao_publica_id": "91b643c0-46e6-4b1b-b488-6254247128fd",
  "nome_dispositivo": "Meu dispositivo"
}
```

O código tem 192 bits aleatórios, prefixo `cpc_` e validade padrão de 600 segundos.
O token tem 256 bits aleatórios e prefixo `cti_`. Ambos são gerados com `secrets`;
o servidor armazena somente HMAC-SHA256 com `COLETA_TOKEN_SECRET`, com domínio
separado para o código. A listagem expõe só os primeiros 12 caracteres do token
como pista de identificação, jamais uma credencial recuperável. Não existe
endpoint de recuperação de segredo: se a resposta se perder, emitir novo código
e parear novamente. Nunca repetir automaticamente uma rotação cujo retorno se
perdeu sem considerar que ela já pode ter invalidado o token anterior.

Um código é consumido em uma única transação junto com a instalação. Dez trocas
concorrentes geram um sucesso. Reparear o mesmo UUID **do mesmo usuário** mantém
uma instalação, substitui a credencial e remove sua revogação. UUID igual em
outro tenant permanece independente. Em reconexões concorrentes com códigos
distintos, a última transação define a credencial vigente. Rotação de instalação
revogada é recusada; a reconexão exige código novo. Revogar novamente é idempotente.

A coleta mantém o lock da credencial até seu commit. Rotação/revogação aguardam
operações já admitidas; depois que retornam, a credencial antiga não inicia
novas coletas. Trabalho que já foi aceito antes da revogação mantém sua durabilidade.
Tokens de instalação não são JWTs e não autorizam apostas, caixa, listagem,
emissão de códigos ou gerenciamento de instalações. Conta desativada não autentica.

## Limites, falhas e observabilidade

| Configuração | Padrão | Limite |
| --- | ---: | --- |
| `COLETA_PAIRING_TTL_SECONDS` | 600 | 60–1800 segundos; também limitado no banco |
| `COLETA_PAIRING_WINDOW_SECONDS` | 60 | 1–86400 segundos |
| `COLETA_PAIRING_CREATE_LIMIT` | 5 | Por usuário/janela |
| `COLETA_PAIRING_EXCHANGE_LIMIT` | 20 | Por endereço de origem/janela |
| `COLETA_PAIRING_GLOBAL_LIMIT` | 300 | Global por ação/janela |

Contadores PostgreSQL serializam todos os processos; somente HMACs das chaves
de quota são persistidos. Tentativas recusadas também gastam cota. A admissão
é commitada antes da transação de negócio, para um código inválido não devolver
a quota ao atacante. 429 informa `Retry-After`. Falha do backend não permite
pareamento: retorna erro e exige retry. Não há fallback em memória. As quotas
expiradas são limpas em lotes de 100 durante admissões; códigos expirados do
usuário são removidos em lotes de 100 durante a emissão seguinte.

Códigos errados, consumidos, expirados e de contas inativas têm o mesmo erro.
IDs de instalações alheias respondem 404. Validação JSON não devolve valores
inválidos, códigos ou labels no erro. Logs `collection_credential` registram
ações estáveis (`code_created`, `paired`, `rotated`, `revoked`, `auth_rejected`,
`exchange_rejected`, `limited`, `limit_unavailable`) e ID interno quando disponível.
Não registrar corpos, tokens, hashes, prefixos, labels, IPs ou fingerprints.
Os sanitizadores cobrem logs estruturados, diffs, exceções e URLs de spans.
Métricas HTTP usam o template da rota, sem credenciais/IDs como labels.

## Migração e rollback

`c107pair2026` segue o HEAD publicado `a9d6e3f1c210`, sem alterar IDs anteriores.
Cria instalações, códigos e quotas com RLS forçado. Inventaria **uma instalação
revogada por linha legada**, com UUID novo e `token_legado_id`; não copia o hash
antigo para uma instalação ativa. Desativa todos os `coleta_token` antigos e a
API deixa de consultar essa tabela para autenticação. Antes do rollout, comunicar
a necessidade de novo pareamento a cada cliente. Não se pode inferir quantos
dispositivos compartilhavam uma credencial antiga; preservá-la como ativa
perpetuaria essa ambiguidade. Os registros legados servem só ao histórico.

Aplicar com usuário de migrations com visibilidade administrativa das tabelas
RLS, nunca com a role restrita. A função SECURITY DEFINER de quotas mantém esse
proprietário privilegiado (superuser/BYPASSRLS); não transferir sua propriedade
para a role da API. Conceder os grants
normais de tabelas/sequências e, se a role for criada depois da migration:

```sql
GRANT EXECUTE ON FUNCTION coleta_pairing_limit(text,integer,integer) TO bancaemdia_app;
```

API usa `bancaemdia_app`, sem privilégios de superuser/BYPASSRLS. O teste consulta
instalações/códigos de outro tenant com esse papel e IDs válidos. A função de
quota tem EXECUTE revogado de PUBLIC e search_path fixado.

Preferir rollback de aplicação compatível com o schema expandido. O downgrade
recusa remover inventário já pareado; primeiro preservar esse inventário por
procedimento administrativo aprovado. O downgrade permitido nunca reativa
credenciais legadas. Restaurar uma aplicação anterior não autoriza reativá-las.
Rotacionar `COLETA_TOKEN_SECRET` invalida todas as credenciais/códigos; coordenar
novo pareamento, sem imprimir segredos em comandos ou logs.

## Validação e integração

`tests/integration/coleta/test_pairing.py` usa HTTP da aplicação, JWT RS256
assinado e PostgreSQL 16 real com a role restrita. Não há skip quando falta o
banco exigido. O broker externo da materialização é substituído somente para
inspecionar o enqueue; autenticação, RLS, transações e locks são reais.

```sh
pytest tests/integration/coleta/test_pairing.py tests/integration/test_coleta_db.py tests/security/test_coleta_tokens.py -n 0 --junitxml=pairing-results.xml
pytest tests/contract/ -n 0
python scripts/generate_openapi.py --check
```

Use `TEST_DATABASE_URL` para um PostgreSQL descartável ou Docker/Testcontainers.
O teste de migration cria um banco isolado e comprova upgrade/rollback sem
reativar o legado. Windows sem Docker valida unidade/contrato; a CI executa o
banco real, suíte completa, cobertura mínima 80%, scanners e Docker build.

Base de produto: `origin/main`. As duas restrições de dependências do #161 foram
reproduzidas pontualmente para manter SQLAlchemy 2.0 e a instrumentação HTTPX
compatíveis, sem depender de merge ou copiar outras features.

O scanner também exigiu remover `python-jose`/`ecdsa` (PYSEC-2026-1325, sem versão
corrigida de ecdsa). A validação JWT usa PyJWT com as mesmas claims obrigatórias,
algoritmo RS256 fixo e seleção por `kid`; os testes de JWT/JWKS permanecem exigidos.
Essa troca converge com a já proposta no #130, sem importar sua cadeia de produto.

Na convergência com os PRs de billing/privacidade, preservar o gate de escrita da coleta depois
de resolver o usuário e incluir instalações/códigos na exclusão da conta. Não
restaurar a autenticação pelo token legado ao resolver conflitos. Esta entrega
não implementa nem declara concluídos billing, exclusão de conta ou contrato v2.
