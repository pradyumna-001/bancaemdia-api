# Matriz de integridade da coleta (#112)

O gate obrigatório executa a aplicação composta inteira três vezes em jobs paralelos,
cada qual com PostgreSQL 16 e Redis 7 exclusivos. As requisições e workers disputam
transações reais dentro de cada cenário. Os casos de banco são sequenciais dentro
de cada job: testes herdados de quotas, migrations e catálogo alteram estado global.
Isso evita interferência entre fixtures sem eliminar nenhuma corrida do produto.

## Base e reprodução

A parte 5 testa o próprio código de produto entregue diretamente à main.
Ela incorpora as partes 1/2/4 do PR #179 e as origens #165/#166/#169/#170.
Não há montagem de código fora da branch, correção aplicada só em ensaio nem
necessidade de aplicar patches depois do merge. `h5review2026` é a ponta completa.

```sh
pip install -e '.[dev]'
export TEST_DATABASE_URL=postgresql+asyncpg://.../banco_descartavel
export REDIS_URL=redis://localhost:6379/0
export CELERY_BROKER_URL=$REDIS_URL
export CELERY_RESULT_BACKEND=redis://localhost:6379/1
pytest tests/integration/coleta/ tests/integration/test_troca_titular.py \
  tests/convergence/financial_acceptance.py tests/cli/test_reconciliar_casa_telegram.py \
  tests/integrity/collection_acceptance.py tests/integrity/accounts_races_acceptance.py \
  tests/integrity/security_acceptance.py -n 0 --show-capture=no --junitxml=integrity-results.xml
python scripts/check_integrity_junit.py integrity-results.xml
```

O inventário exige 161 casos (todos os 155 originais e seis regressões herdadas da parte 4), incluindo parâmetros. Falha,
erro, skip, ausência, duplicação ou caso inesperado reprovam o gate. Cada um dos
três jobs usa seus próprios PostgreSQL/Redis; Docker Build depende dos três.
Além da matriz, os gates de CLI histórica, matching/consolidação, contas/billing,
catálogo, contratos de coleta e segurança rodam sobre o mesmo HEAD.

O resolver compartilhado usa `parsed.comeca_em`/`data_jogo` e `usos_conta_casa`.
Referência explícita validada identifica a conta que realmente fez a bet,
mesmo histórica/inativa; referência inválida não recebe fallback. Ocorrência
continua determinando matching e cortes da sessão, nunca a conta padrão.

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

A integração de código desta parte fica **Esperando revisão** depois que todos
os checks aplicáveis do HEAD estiverem verdes. Ativação de produção depende do
lançamento e não é simulada por esta evidência. O pacote humano de fixtures da
#114 continua fora do Git e aguardando o administrador; esta matriz usa apenas
contratos sintéticos independentes já publicados, sem aprovar leitores produtivos.
