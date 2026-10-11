# Lista operacional de Apostas (#186)

`GET /api/v1/apostas` devolve os textos da mesma razão de eventos usada por
`GET /api/v1/apostas/{chave}`. O servidor dobra os eventos pelo projetor canônico
`projetar`, na ordem de ID. As correções posteriores aparecem em `casa`, `evento`,
`descricao` e `mercado` (este último corresponde a `mercado_bruto`). O PATCH
aceita `mercado_bruto: string | null` pela allowlist de correção do domínio,
com a mesma validação de texto dos demais campos. Ausência ou limpeza explícita
permanece `null`; nenhum texto é inventado a partir de nomes de catálogos.

Os números, origem, freebet, revisão e exclusão continuam vindo da materialização
canônica. A lista não calcula uma segunda versão das finanças a partir do histórico.
Retorno/lucro desconhecido permanece `null`. A SPA apresenta os valores já
calculados pelo servidor, sem consultas de detalhe por linha.

## Contexto autorizado

Os GETs da lista e do detalhe acrescentam dois campos opcionais e anuláveis em
`BetResponse`. Os IDs novos são strings decimais exatas de BIGINT positivo;
não passar por `Number`, inclusive em URL/filtros. Os campos antigos mantêm seus
tipos por compatibilidade.

| Campo | Conteúdo / ausência |
| --- | --- |
| `conta_contexto` | `null` se conta não atribuída, inexistente ou inacessível ao proprietário |
| `.id` | ID exato da conta **gravada na aposta** |
| `.casa_id` | ID exato da casa dessa conta; casa não é conta, titular nem banca |
| `.apelido` | Apelido atual do cadastro; apelido legado vazio é `null` |
| `.ativa`, `.estado` | Situação atual da conta, sem excluir contas inativas da projeção |
| `.titular` | `null` quando a conta não possui titular autorizado/existente |
| `.titular.id`, `.nome`, `.arquivado` | ID exato, nome atual e situação atual do titular associado à **conta gravada** |
| `banca_contexto` | `null` se a referência de banca gravada na aposta estiver ausente/inacessível |
| `.id`, `.nome` | ID exato e nome atual da banca gravada na aposta; não substitui pela banca atual da conta |

Identidade histórica e rótulo atual são conceitos distintos: a atribuição da
conta pertence à escrita/materialização, usando o instante do **jogo** e os
intervalos canônicos de uso; a referência explícita de multicontas preserva a
conta que realizou a aposta. Um GET nunca escolhe a primeira conta ativa, nunca
usa o titular em uso agora e nunca reatribui uma aposta antiga. Trocar a conta
em uso mantém as referências históricas já gravadas. Alterar a data do jogo
por PATCH segue o resolvedor existente de atribuição. Ausência de data e de
referência explícita mantém a conta sem atribuição.

Renomear uma conta ou titular muda o rótulo no próximo snapshot visível, sem
trocar o ID da aposta. Um titular anterior ou arquivado continua legível na conta
histórica autorizada. Não existe snapshot histórico de apelido/nome neste modelo;
esses nomes não afirmam o texto usado no dia do jogo. Se a conta legada não
possuir titular, ele fica `null`. Respostas de POST/PATCH/DELETE/restauração
incluem os novos campos como `null`, pois não hidratam os cadastros; os GETs
publicados hidratam esse contexto.

## Página, consultas e frescor

Seleção, ordenação, total e paginação pertencem a `ApostaRepo.list_page`. A
projeção acontece **depois** da seleção e conserva cada linha, inclusive sem
histórico/texto, em revisão ou apagada quando solicitada. Não filtra pela
presença dos novos campos. Limite de página continua 100.

Uma página não vazia executa três consultas de dados: seleção com total por
window, eventos somente das chaves da página, e referências autorizadas somente
dos IDs da página (conta/titular/banca por LEFT JOIN). Com SET LOCAL da RLS e
leitura do usuário, são seis consultas no teste HTTP no primário (a RLS é
configurada no `after_begin` e no `_open` existentes). Esse número é constante
para 1 ou 100 linhas, independente do número de eventos. Não há N+1, chamada
HTTP de detalhe por linha nem leitura de todo o histórico do usuário. Página
vazia não faz consultas de eventos ou cadastros; página além do fim mantém o
COUNT complementar preexistente para retornar o total correto. O volume/custo
dos eventos cresce com os históricos das apostas presentes na página, não com
todas as apostas do proprietário. Não há cache adicional nem trabalho em background.

Lista e detalhe usam o mesmo `get_db_snapshot`/autenticação de snapshot:
REPEATABLE READ mantém linha, total, textos e referências coerentes mesmo se
uma correção concorrente confirmar entre as consultas. Mantêm o roteamento
primário/réplica existente; o snapshot observa o estado disponível nessa conexão,
portanto uma réplica pode atrasar. Escritas não são promovidas pela leitura.
Sessão cookie, GET sob READ_ONLY, proteção CSRF de mutações e RLS permanecem
obrigatórios. Todas as tabelas privadas têm também predicados explícitos de
proprietário. Referências legadas inválidas não produzem nomes de outro usuário.

## Coordenação e integração do site

A #186 é independente e baseada diretamente na main. A #184 acrescenta filtros
completos (`visibilidade=ativas|apagadas|todas`, grupo/banca), catálogos e equivalência
lista/resumo/métricas/XLSX. Esses requisitos do site dependem da integração da
#184; a #186 não os simula nem duplica sua implementação. A alteração de projeção
fica após a seleção e deve preservar exatamente a população de #184. A prova
de composição temporária com a candidata #184 é descrita no PR; ela não é um
segundo contrato autorizado. #185/USDT não participa deste endpoint.

Gerar o contrato da árvore completa com `python scripts/generate_openapi.py` e
verificar com `--check`. Os schemas oficiais estão em
`tests/contract/schemas/openapi.json`, sem overlays de schemas do cliente.
O PR informa SHA completo da candidata e SHA-256 dos bytes oficiais do Git.
Uma PR candidata não significa integração/deploy: o administrador decide o merge.
O frontend #18 regenera tipos da versão backend autorizada ou integrada, declara
a dependência de #184 e da candidata #186 e completa a lista sem buscar detalhes
por linha ou calcular finanças no navegador.

## Provas reproduzíveis

`pytest tests/integration/test_bet_page_projection.py -n 0` usa PostgreSQL
migrado descartável, credencial comum com RLS e credencial restrita de identidade.
Somente o emissor externo é simulado; cookie assinado/criptografado, CSRF,
READ_ONLY, escrita HTTP, materialização, projeção e consultas são reais.
O ambiente CI executa também a jornada dedicada de identidade com emissor real.
O arquivo cobre correção dos quatro textos por allowlist, legado sem histórico,
freebet, revisão, exclusão/restauração, paginação/total, consultas constantes,
troca temporal, multicontas, rótulos renomeados/inativos/arquivados, referência de
banca independente, BIGINT maior que 2^53, proprietário estrangeiro, referência
privada inválida, READ_ONLY/CSRF/autenticação e commit concorrente no snapshot.
`TEST_DATABASE_URL` pode apontar a um PostgreSQL descartável em vez de Docker;
o teste nunca pode usar banco real de usuário. A suíte geral e os gates de
compatibilidade/segurança/performance continuam obrigatórios, sem redução.
