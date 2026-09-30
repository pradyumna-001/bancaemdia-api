# Catálogo de casas por marca e domínio exato (#113)

O catálogo é independente de `casas` e dos seis leitores herdados. Uma empresa pode operar
várias marcas; cada marca e hostname é uma integração distinta. Nenhuma autorização regulatória
ou confirmação de acesso transforma um leitor em suportado. Novos registros entram como
`nao_avaliado`, com rollout `disabled`. Não há alteração de contas, apostas ou cálculo financeiro.

## Fronteira e pré-requisito

O PR de produto parte diretamente da main. A autenticação de instalações, pareamento, rotação,
expiração, inativação do proprietário e revogação pertencem à #107 / PR #163, SHA
`a824c8e85638ebb3823990886e9514aa07f424cc`. O catálogo chama o mesmo repositório desse PR;
ausência do módulo resulta em 503, sem aceitar tokens legados. A montagem obrigatória na CI
usa `scripts/prepare_catalog_integration.py`, preserva as revisões publicadas e acrescenta somente
uma revisão de merge descartável `c113integration2026` entre `c107pair2026` e `c113catalog2026`.
O administrador deve integrar #163 e conciliar essas pontas antes do lançamento; não mergear
a árvore descartável como produto. Se o SHA ou a interface mudar, repetir a composição e a CI.

Manifesto, verificações criptográficas no navegador, grants/revogações de permissões e UI são
responsabilidade do novo backlog da extensão. Esta entrega não modifica aquele repositório.

## Fontes regulatórias e atualização

- Página SPA/MF de autorizações nacionais:
  <https://www.gov.br/fazenda/pt-br/composicao/orgaos/secretaria-de-premios-e-apostas/transparencia-ativa-processos-de-autorizacao-de-apostas-de-quota-fixa/empresas-autorizadas>.
- Excel: seguir o link rotulado **Baixar Arquivo Excel** nessa página, atualmente
  <https://www.gov.br/fazenda/pt-br/composicao/orgaos/secretaria-de-premios-e-apostas/transparencia-ativa-processos-de-autorizacao-de-apostas-de-quota-fixa/planilha-de-autorizacoes.xlsx>.
  Links sem rótulo apontam também para um XLSX inexistente; não se usam esses links.
- Autorizações por determinação judicial, fonte separada:
  <https://www.gov.br/fazenda/pt-br/composicao/orgaos/secretaria-de-premios-e-apostas/transparencia-ativa-processos-de-autorizacao-de-apostas-de-quota-fixa/autorizadas-por-determinacao-judicial>.
- A consulta SIGAP de **solicitantes**, <https://sigap.fazenda.gov.br/consulta-publica/lista-operacoes-aqf>,
  não é uma relação de autorizadas. `kind=applicants` somente admite `situation=solicitante`;
  acesso manual somente admite `acesso_confirmado`. Os conjuntos administrativos de evidências
  federais/judiciais válidas excluem solicitantes e acesso manual.
- [Matriz estadual e DF](../catalogo/fontes-estaduais.json): todas as 27 jurisdições têm URL
  oficial consultada, data, evidência, status e próxima conferência. Quando houve resposta,
  há hash dos bytes e HTTP status; falha de conexão fica registrada como indisponibilidade.
  Um portal acessível não significa uma lista de autorizações configurada, nem ausência de licenças.

A consulta inicial de 30/09/2026 encontrou 185 pares na página nacional, os mesmos 185 no Excel,
seis pares na lista judicial e três marcas nacionais com domínio “a definir”. Esses três casos
permanecem em `unresolved_domains` e nos snapshots brutos, sem inventar hosts. São observações
datadas, não uma promessa de permanência dessas listas. As fontes estaduais ainda não têm feed
de autorização revisado configurado: 21 portais responderam e seis ficaram indisponíveis na consulta.
Essa lacuna permanece explícita nos totais; não se preenche com bets federais ou links deduzidos.

Reconsultar as fontes federais a cada sete dias (validade da observação: sete dias), revisar
mudanças de formato e reconsultar cada URL estadual até `recheck_at`. Para configurar uma fonte
estadual, obter a tabela oficial que relaciona marca e domínio, preservar os bytes e revisar
o mapeamento. Os parsers HTML, CSV e XLSX recusam listas vazias/captcha e cardinalidades ambíguas;
não inferem marca pelo domínio. Importar um documento `Source` revisado com `kind=state`, UF,
URL oficial, situação por registro, horários com fuso, validade e SHA-256 dos bytes. O `source_key`
da fonte permanece estável e distinto do registro de disponibilidade `state-registry-<uf>`.
Suspensas, revogadas e expiradas são observações auditáveis; nunca substituir a evidência por exclusão.

As URLs das fontes não contêm query, credenciais, fragmento ou portas alternativas. Downloads
federais só seguem HTTPS no host `www.gov.br`, em até seis passos, com limite de 10 MiB e timeout.
Os caminhos originais, redirects e snapshots datados são guardados no diretório operacional
escolhido; não executar snapshots contra páginas autenticadas de usuários.

## Operador e comandos

Aplicar `c113catalog2026` com as credenciais de migration. A aplicação deve usar um papel
`NOBYPASSRLS` que não seja proprietário das tabelas. Um DBA/proprietário do banco concede
explicitamente o papel de operador a um usuário interno já existente:

```sql
INSERT INTO catalogo_operadores(usuario_id) VALUES (<usuario_id_interno>);
```

Esse privilégio não vem de e-mail, claim customizada, assinatura ou parâmetro HTTP. A própria
aplicação não pode inserir/alterar operadores. A RLS só permite ao usuário ler sua concessão.
O proprietário do banco pode remover a concessão; usuário inativo também perde acesso.
Fontes, evidências e suporte globais ficam sob RLS de operador. Confirmações ficam sob RLS
do usuário e podem ser lidas por operadores para a campanha. Publicações contêm somente dados
técnicos e são legíveis no banco por usuários internos ativos; o endpoint exige a instalação.

```bash
# Preview de aquisição, sem banco: baseline vazio explicitamente indicado no relatório.
python scripts/sincronizar_catalogo_casas.py --federal --offline \
  --snapshots /auditoria/catalogo/snapshots --report /auditoria/catalogo/preview.json

# Diff contra o estado persistido, sem commit ou publicação.
python scripts/sincronizar_catalogo_casas.py --usuario-id 42 --federal --manual \
  --snapshots /auditoria/catalogo/snapshots --report /auditoria/catalogo/diff.json

# Aplicar em uma transação, após revisar o relatório e com papel de operador.
python scripts/sincronizar_catalogo_casas.py --usuario-id 42 --federal --manual --apply \
  --snapshots /auditoria/catalogo/snapshots --report /auditoria/catalogo/aplicado.json

# Fonte estadual revisada, com prova bruta de hash correspondente.
python scripts/sincronizar_catalogo_casas.py --usuario-id 42 --source /auditoria/pr.json \
  --raw /auditoria/pr.snapshot --apply --snapshots /auditoria/catalogo/snapshots \
  --report /auditoria/catalogo/pr-aplicado.json
```

O dry run contra banco lê sob lock transacional e termina com rollback. Mostra `added`, `changed`,
`removed_from_source` e `manual_unchanged` por fonte. Não apaga entradas, confirmações ou snapshots.
Retry do mesmo documento/data/hash não duplica registros. Nova consulta datada dos mesmos bytes
guarda nova observação sem perder a anterior. Sincronizações e publicação usam o mesmo advisory
lock; rollback desfaz entradas, evidências e auditoria juntos. Os hashes são dos bytes consultados,
não apenas dos registros extraídos. Mudanças regulatórias não escrevem o campo técnico.

## Confirmação manual e operação técnica

`POST /api/v1/catalogo/candidatos`, com Bearer JWT do usuário, recebe `brand`, `hostname`,
`access_confirmed=true`, `confirmed_at` com fuso (não futuro) e `evidence_sha256`. O usuário
guarda a prova original em seu ambiente; o servidor recebe somente o hash, sem cookies,
headers, credenciais, screenshot pessoal ou URL autenticada. Campos extras são recusados.
A confirmação é idempotente; o operador inclui esses candidatos usando `--manual`.

`GET /api/v1/admin/casas` e `/api/v1/admin/casas/export` exigem JWT e concessão de operador.
Ambos usam o primário, registram a leitura/exportação e retornam JSON com marca/host, evidências,
suporte, fontes, conjuntos informativos federal/judicial e totais das 27 jurisdições.
Não expõem tokens, provas brutas pessoais ou credenciais de captura.

O suporte pode ser alterado **explicitamente** com `--technical arquivo.json --entry-id ID --apply`.
Valores: `nao_avaliado`, `precisa_captura`, `em_desenvolvimento`, `suportado`, `bloqueado_externo`,
`regressao`. `suportado` exige adapter, versão e hash da captura de aceite; enabled/canary exigem
suporte comprovado. A promoção depende da campanha/aceite, não dos seis parsers antigos.
O CLI registra antes/depois e ator. Para redirects, registrar a cadeia HTTPS observada (sem segredos),
usar o hostname final como entrada e preservar os hosts anteriores em `aliases`; nenhuma alias
vira wildcard. Host antigo permanece auditável, com rollout disabled/revoked após decisão explícita.

## Assinatura, contrato e cache

O operador publica usando chave Ed25519 montada como arquivo secreto, fornecida pela operação,
sem gerar ou commitar chave privada nesta entrega:

```bash
python scripts/sincronizar_catalogo_casas.py --usuario-id 42 --apply --publish \
  --signing-key-file /run/secrets/catalog-ed25519.pem --key-id production-2026-01 \
  --minimum-client-version 1.0.0 --next-root /config/catalog-next-public.json \
  --snapshots /auditoria/catalogo/snapshots --report /auditoria/catalogo/publicado.json
```

`--next-root` é opcional e contém somente `key_id`, `algorithm=Ed25519`, `public_key` em base64url.
ID seguinte deve ser distinto. Primeiro distribui-se a raiz seguinte em uma publicação assinada
pela raiz confiável atual; só então passa-se a assinar com a seguinte. A primeira confiança deve
ser provisionada/pinada fora do próprio catálogo (configuração revisada da extensão). Uma raiz
incluída numa resposta não autenticada não cria confiança. Chaves e catálogos não se compartilham
entre ambientes; o ambiente é assinado e a API recusa um ambiente solicitado diferente do runtime.

`GET /api/v1/coleta/catalogo?client_version=1.0.0&environment=<APP_ENV>&known_version=N`
usa `X-Coleta-Token` da instalação pareada. Resposta: `{payload, signature}`. A assinatura cobre
o payload completo: versão monotônica durável, contrato, ambiente, issued/expires, key_id,
algorithm, raízes atual/seguinte, mínimo do cliente e entradas exatas com adapter/schema/version,
suporte e rollout. JSON usa UTF-8, ordenação de propriedades ASCII, inteiros seguros, sem floats,
sem espaços; assinatura e chaves públicas são base64url sem padding. ETag é SHA-256 dos bytes
canônicos do envelope inteiro. Não há identidade do usuário, contas, credenciais, situação
regulatória, permissão de navegador ou endosso legal na projeção.

Publicação vale uma hora. Reassinar/publicar antes de expirar; publicação é imutável e a sequência
nunca reutiliza um número, inclusive após rollback. 403: instalação inválida/revogada/expirada;
409: ambiente divergente ou `known_version` maior que a atual; 426: cliente abaixo do mínimo;
503: publicação expirada/ausente ou #107 não integrado. O mínimo de cada adapter continua no
payload assinado para o cliente desabilitar adapters incompatíveis. Tombstones de hosts revogados
permanecem explícitos na publicação seguinte. Cache `private, max-age<=300, must-revalidate`,
`Vary: X-Coleta-Token`; 304 tem corpo vazio e só ocorre **depois** de autenticação, compatibilidade,
monotonicidade e validade. Rotação/revogação não pode ser contornada por ETag.

Contrato de last-known-good: no máximo 86.400 segundos desde `issued_at`, assinatura previamente
verificada, mesmo ambiente e nenhuma redução da maior versão conhecida. Não permite novos hosts
ou reabilitar hosts já revogados. Não estende a validade indefinidamente e não autoriza operações
financeiras nem novas permissões do navegador. A implementação desse comportamento na extensão
é separada; o backend publica os limites e recusa sua própria publicação expirada.

## Migração, rollback e validação

Sete tabelas novas, sem reescrever IDs existentes. Snapshots, publicações, confirmações e auditoria
são append-only inclusive contra DELETE/UPDATE pelo proprietário (triggers). Downgrade com dados
de auditoria é recusado; rollback operacional é da aplicação, mantendo esquema, snapshots e chaves
públicas para verificação histórica. Downgrade em banco vazio é possível. Concessões administrativas
pertencem ao DBA; não adicionar concessões amplas para resolver falhas de RLS.

CI exige lint/formato, tipagem estrita, suíte completa com 80% de cobertura, contrato OpenAPI/oasdiff,
segurança de dependências/código, Docker e duas aceitações PostgreSQL 16: main e montagem #107.
As aceitações novas recusam banco ausente e JUnit com skips/falhas/casos obrigatórios ausentes.
Há provas de dry run/persistência/rollback/idempotência/concorrência real, separação de fontes,
RLS/autoelevação/confirmacões, auditoria append-only, assinatura/rotação, pareamento HTTP real,
ETag, expiração, downgrade, ambientes e host revogado. Não se usa conta de bet, bot, IA paga,
cloud ou dado pessoal real nesses testes. O preview oficial não substitui essas provas de banco.

| Requisito | Implementação | Evidência |
| --- | --- | --- |
| Marca × hostname, redirects | Observation/CasaDominio, aliases e cadeia exata | unit + remoção/mudança PostgreSQL |
| Legal separado de técnico | Source/Snapshot/evidencias; Technical independente | classes federal/judicial/state/applicants e estados legais |
| SPA nacional/Excel/judicial | parsers e CLI, bytes/SHA/data, fontes separadas | preview oficial + parsers sintéticos + sync real |
| 27 jurisdições | matriz datada e registros visíveis | validação da matriz + totais/export RLS |
| Manual, sem segredos | confirmação por usuário e hash | HTTP/JWT, retry, extra-field refusal, RLS |
| Idempotência/diff/história | lock comum, snapshot append-only, remoção informativa | retry, concorrência, rollback, triggers |
| Admin auditado | concessão DB, RLS, primário e audit | HTTP autorizado/negado, sem autoelevação |
| Projeção autenticada | repositório #107 + envelope imutável | parear/rotacionar/revogar pela API real |
| Assinatura/cache/versões | Ed25519/canonical/ETag/raízes/floors | adulteração, rotação, downgrade, stale, cross-env |
| Nenhuma permissão/endosso | payload estritamente técnico | schema/contratos e ausência de dados de usuário |
