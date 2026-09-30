# Matriz de integridade da coleta (#112)

O gate obrigatório executa a aplicação composta inteira três vezes em jobs paralelos,
cada qual com PostgreSQL 16 e Redis 7 exclusivos. As requisições e workers disputam
transações reais dentro de cada cenário. Os casos de banco são sequenciais dentro
de cada job: testes herdados de quotas, migrations e catálogo alteram estado global.
Isso evita interferência entre fixtures sem eliminar nenhuma corrida do produto.

## Base e reprodução

O PR da #112 nasce diretamente da main `bd055417459f796fed960b5b37efb33a9744419f`.
Ela ainda não possui os serviços das #107–#111. A CI constrói uma composição
descartável, sem incorporar a cadeia de commits ao diff do PR:

| Pré-requisito | SHA fixado |
| --- | --- |
| #107/#108, PR #164 (inclui #163) | `9210cef7e1b64664ac8ad347afa69ad02c056cef` |
| #94/#95, PR #135 (inclui #134 e sua base) | `427e6af8d6664bd2076bfe6518be754b5934d77b` |
| #109/#110, PR #166 | `db7f63eccd03635bac20235270bffa30351e41a9` |
| #111, PR #169 | `0dfd5a6cfe70a94bb813d47dac50b2f64ed3b073` |

`scripts/prepare_integrity_integration.py` reutiliza o preparador e a resolução
revisável de conflitos do #166. Acrescenta apenas os seis arquivos da CLI/journal/testes
da #111, a matriz da #112, a correção explícita descrita abaixo e uma revisão Alembic
de merge exclusiva do ensaio. Os IDs publicados dos pré-requisitos permanecem intactos;
o preparador original do #166 religa sua própria migration ainda não integrada ao merge
de dependências. Os dois testes herdados que verificam a ponta Alembic mantêm todas as
asserções; o valor esperado passa a ser a ponta completa `r112integration`.

Destinos existentes são recusados. Nenhum checkout de produto é mesclado ou substituído.
`integrity-heads.json` registra todos os SHAs, a ponta da matriz e o hash SHA-256 da
correção. Isso prova essa composição específica; mudanças nos pré-requisitos exigem
atualizar os pins, conciliar e repetir todos os gates antes de declarar a base integrada.

Com os quatro objetos Git disponíveis e PostgreSQL/Redis descartáveis já ativos:

```sh
python scripts/prepare_integrity_integration.py /tmp/issue112-integration
pip install -e '/tmp/issue112-integration[dev]'
cd /tmp/issue112-integration
export TEST_DATABASE_URL=postgresql+asyncpg://bancaemdia:test-password@localhost:5432/bancaemdia
export REDIS_URL=redis://localhost:6379/0
export CELERY_BROKER_URL=redis://localhost:6379/0
export CELERY_RESULT_BACKEND=redis://localhost:6379/1
pytest tests/integration/coleta/ tests/integration/test_troca_titular.py \
  tests/convergence/financial_acceptance.py tests/cli/test_reconciliar_casa_telegram.py \
  tests/integrity/collection_acceptance.py tests/integrity/accounts_races_acceptance.py \
  tests/integrity/security_acceptance.py -n 0 --show-capture=no --junitxml=integrity-results.xml
```

Depois, execute `scripts/check_integrity_junit.py` do checkout da #112 apontando para
o XML gerado. O inventário `scripts/integrity/required-cases.json` exige os **155 nomes
exatos**, inclusive parâmetros: 104 casos herdados e 51 novos. Erro, falha, skip,
nome ausente, inesperado ou duplicado fazem o gate falhar. A suíte dependente usa
nomes `*_acceptance.py` e é selecionada explicitamente no job obrigatório; a suíte
normal da main continua executando integralmente. Ausência de serviço ou módulo
pré-requisito é erro, nunca skip. Docker Build depende das três execuções da matriz.

## Correção executável de integração

`scripts/integrity/fixes.patch` corrige `workers/coleta_v2.py` após a composição do #166.
O worker exigia referência explícita mesmo quando a data do jogo tinha um único uso
válido. Agora utiliza `account_for_state`, o resolver compartilhado, com
`parsed.comeca_em`. Referência inválida não recebe fallback. Zero/ambiguidade permanecem
`needs_review`; uma conta default inferida não é gravada como referência explícita.

A referência explícita válida identifica a conta que realmente fez a bet no multicontas,
inclusive fora de seu intervalo padrão ou já limitada/inativa. Atribuição default usa
a **data do jogo**, nunca colocação/captura. Ocorrência continua determinando o corte de
sessão e a ordem da fonte; esses relógios têm funções diferentes. Isso aplica a instrução
do proprietário e substitui o texto antigo de validade temporal explícita da #108/#112.

O arquivo de produção não existe na main atual. Por isso a correção está apresentada
como patch obrigatório, auditado no diff e aplicado automaticamente pelo preparador.
Na integração dos PRs, o administrador deve aplicar a mesma correção ao arquivo já
integrado (`git apply --check scripts/integrity/fixes.patch`, depois `git apply`),
conciliar a ponta Alembic e reexecutar a matriz na base resultante. Não é uma correção
oculta do ambiente nem evidência de código já entregue em produção.

## Requisitos e provas

| Requisito | Caminho real e prova |
| --- | --- |
| Código de pareamento consumido uma vez | 10 exchanges HTTP, uma instalação/token, casos herdados `test_pairing` |
| Rotate/revoke isolados | Duas instalações; códigos/segredos só como hashes; lifecycle herdado |
| Revogação disputando coleta | `security_acceptance`: barreiras e `pg_blocking_pids`; revogação vence → 401 com challenge Collection/zero entregas; coleta autenticada vence → commit antes de rotate/revoke retornar; credencial antiga sempre recusada depois (v1/status herdados: 403) |
| Isolamento e menor privilégio | HTTP com token/JWT reais; sessões/jobs/contas de outro tenant; RLS SELECT/UPDATE zero linhas; worker não processa job alheio; papel NOBYPASSRLS/não superuser |
| Duplicatas no batch, outros batches e retries | 10 requests com `[item,item]`, ACK estável, 10 workers, uma entrega/linhagem/aposta Casa/relação; replay completo em três ordens |
| ACK parcial e resposta perdida | Accepted/rejected terminais não são reenviados; apenas ACK perdido é recuperado pelo mesmo ID; accepted subset mantém hash de auditoria |
| Malformado/tamanho | JSON inválido, 101 itens e corpo >1 MiB recusados; prefixo válido não cria entrega/fato; limite diário e tamanho de item herdados |
| Hash e proveniência | Todos os envelopes persistidos têm hash independente recalculado e origem observada; replay preserva hash do conjunto bruto; adulteração e segredo herdados |
| Lifecycle | Win/Void/Cashout em open-first/settled-first/racing; settled duplicado e open obsoleto; uma stake, retorno/exposição e lucro exatos no extrato |
| Sessão | Ocorrência -1s/0/+1s do corte com captura tardia; ocorrência ausente/não confiável vira revisão; resume/reconnect imutáveis e propriedade por instalação herdados |
| Conta | Sem ref com uso único do dia do jogo; ref histórica real precede; zero uso e duas identidades sem uso não escolhem primeira; ref de usuário/casa errada/ID inexistente não recebe fallback |
| Ambiguidade temporal | PostgreSQL recusa sobreposição via exclusion constraint; resolver puro recusa duas contas simultâneas e prova início inclusivo/fim exclusivo sem retirar constraints para fabricar estado inválido |
| Ambas as direções e workers | Casa/TG/chegada simultânea; 10 workers no mesmo par revisado; duas Casa/um TG e duas TG/uma Casa mantêm revisão e só uma ponta pode ser tomada |
| Falhas transacionais | Exceção após materialização, geração de candidatos e eventos de consolidação; comparação de apostas/relação/eventos/candidatos prova rollback conjunto; inbox permanece pending; retry + replay convergem |
| Interleavings | Oito seeds × três execuções com offset de seed; duplicação/open/settled/TG concorrentes com atrasos pequenos, determinísticos e reproduzíveis |
| Redis/broker | Publicação Celery pelo transporte Redis real, perda/duplicação do hint, inbox PostgreSQL durável; reinício em processo novo e indisponibilidade de broker herdados |
| CLI histórica | Dry-run após consolidação online não altera linhas; apply/reapply com hash aprovado reutilizam fato/auditoria; replay mantém o mesmo resultado |
| Invariante transversal | Identidade Casa única; endpoints ativos únicos; duas auditorias por par; fontes e contexto preservados; stake/retorno/exposição e lucro do extrato; igualdade das projeções, evidência/contexto e hashes após replay |

O oráculo financeiro é independente: stake R$100,00, odd 2; Win retorna R$200,00
e lucro R$100,00; Void retorna R$100,00 e lucro zero; Cashout retorna R$125,00 e
lucro R$25,00. Pending conserva exposição R$100,00 e não entra no extrato liquidado.
Sem decisão segura, fontes distintas continuam contadas e em revisão; o teste não
reduz artificialmente totais de pares prováveis.

## Diagnósticos e limites

Cada caso novo publica contagens persistidas do próprio tenant e, quando disponível,
valores esperados/observados do invariante. Falhas incluem fase, nome e seed; nenhum
envelope bruto, token, código, URL de banco ou mídia é publicado. Os artefatos incluem
JUnit, manifest de SHAs e JSONs de diagnóstico. Serviço indisponível produz falha explícita.

Os testes usam exclusivamente dados sintéticos, JWTs locais e dublês de Telegram/IA.
PostgreSQL, locks/RLS/constraints, HTTP ASGI e o transporte Redis são reais. Isso não
comprova extensão instalada, provedor produtivo, staging, réplica hot standby ou deploy.
Não provisiona serviços, não reconcilia usuários reais e não faz merge dos PRs.

A entrega técnica da #112 ficará `Esperando lançamento` enquanto esses pré-requisitos
continuarem abertos. Responsável pela integração: administrador do repositório.
Próximo passo: integrar dependências e a correção explícita, repetir gates na base
integrada e só então adotar a matriz diretamente sobre essa base.
