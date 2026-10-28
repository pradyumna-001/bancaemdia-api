# Painel: grupos de origem e diário completo (#188)

Contrato para frontend #20/#50. ADRs 002 (agregação no banco), 004 (compatibilidade),
007/009 (transação e consistência), 018/020 (escopo) e 025 (sessão OIDC).

## Integração e dependências

A branch parte diretamente de `main` b916f54331f14cf47a3800324bd61d8638043c06.
Os seletores, grupos explícitos, comandos auditados, migração `s183filter2026`,
resumo filtrado e testes da PR #184, SHA 9b42a3579518a84d348d272afbaeff88f9119825,
são pré-requisitos herdados incluídos nesta árvore para que ela execute sozinha.
Também são herdadas suas otimizações de coleta, necessárias aos gates existentes.
O administrador deve reconciliar esse trecho comum ao integrar #184 e esta PR.
Não há base em uma branch intermediária, merge administrativo ou dependência
da projeção de lista da #187. A #187 é relacionada à conclusão da tela de apostas.

Escopo próprio: diário limitado, hierarquia paginada, cursores privados, XLSX
detalhado, contrato, documentação e aceite obrigatório. A evolução antiga e seus
buckets semanais/mensais conservam o contrato. Atribuição de conta pelo dia do jogo
e a exceção explícita de multicontas continuam nas regras existentes de projeção.

## Leituras

| GET | Uso | Paginação |
| --- | --- | --- |
| `/api/v1/painel/filtrado/grupos` | Grupo → tipsters e totais completos | `page_size` 1–100, padrão 50, `cursor` |
| `/api/v1/painel/filtrado/diario` | Lucro de cada dia civil, em todos os períodos | `dias_por_pagina` 1–366, padrão 90, `cursor` ou `inicio_dia` |
| `/api/v1/painel/filtrado/detalhado/export` | XLSX reconciliável, sem paginação de apresentação | Até 100000 dias e 100000 pares; excedente é 422 |

Todas usam exatamente os seletores de `/api/v1/apostas` e `/painel/filtrado`:
`periodo=7d|30d|90d|1y|all`, ou `desde` inclusivo/`ate` exclusivo;
`casa_id`, `tipster_id`, `mercado_id`, `competicao_id`, `titular_id`,
`conta_casa_id`, `grupo_id`, `banca_id`, `estado`, `origem`, `revisao_grave`,
`visibilidade=ativas|apagadas|todas`, legado `incluir_apagadas`.
Não combinar período com limites, nem visibilidade conflitante com o legado.
IDs de catálogo são strings decimais de BIGINT positivo. Filtros são ecoados na
resposta já normalizados, inclusive os limites efetivos do período.

Critério real: **data_aposta**, fuso fixo **America/Sao_Paulo**, coerente com #184.
Não é liquidação, criação nem data_jogo. Preferências de fuso não alteram essa
projeção. O critério de atribuição temporal da conta é independente e continua
pelo jogo. Timestamp legado sem offset significa UTC. Os dias aparecem em ordem
crescente, inclusive os vazios; limites horários selecionam apenas fatos dentro
do intervalo, mesmo nas duas datas civis parcialmente abrangidas.

Sem janela, o histórico vai do primeiro fato datado até o dia mais recente ou
hoje, inclusive. `all` usa a janela civil legada até amanhã. Fatos sem data entram
somente quando os seletores não impõem limites; são publicados em `sem_data`,
e nunca inventamos uma data para fazê-los caber no gráfico. Histórico sem fatos
datados tem intervalo nulo; janela explícita com zero fatos devolve os dias vazios.
Limites civis aceitam anos 2–9998; extremos fora desse intervalo retornam 422.

Cada resposta usa um snapshot PostgreSQL REPEATABLE READ. `snapshot_em` é o início
da transação; `respondido_em` é a emissão. `fonte_dados=apostas_ao_vivo`.
`fresh=true` escolhe o primário: não atualiza MV. Respostas distintas, inclusive
páginas distintas, são snapshots distintos. Havendo edição durante navegação,
refaça todas as seções; se os limites mudarem, o cursor diário retorna 422.
Não misture snapshots antigos para apresentar uma reconciliação atual.

## Finanças e reconciliação

`moeda=BRL`, `escopo=apostas_legadas_brl`. Este contrato não mistura o ledger USDT
da #115/PR #185 com centavos BRL. Quantidades contam a população selecionada,
inclusive fontes excluídas do financeiro; `fontes_excluidas_financeiro` as informa.
A consolidação existente impede somar duas vezes Casa/Telegram. Os agregadores
existentes calculam stake, retorno e retorno menos custo. Freebet mantém valor
de face em `base_roi_centavos`, com o custo efetivo já decidido pelo servidor.
Void, meio green/red, cashout e revisão seguem as regras canônicas.

Valores monetários são **strings inteiras assinadas em centavos**; ratios são
strings decimais. Use `BigInt`/decimal para formatar; nunca `Number` para IDs ou
dinheiro. O cliente só formata e calcula geometria.

| `estado_dado` | Semântica de `lucro_centavos` |
| --- | --- |
| `sem_atividade` | Nenhum fato; `"0"`, contagem 0 |
| `confirmado` | Sem pendência, revisão ou resultado desconhecido; pode ser `"0"` |
| `pendente` | Só parcela pendente, sem liquidação conhecida; null |
| `desconhecido` | Revisão ou resultado ausente sem liquidação conhecida; null |
| `fora_do_financeiro` | Só fontes contextuais excluídas; null, parte conhecida `"0"` |
| `parcial` | Liquidações conhecidas junto de alguma parcela incompleta; null |

`lucro_conhecido_centavos` e `retorno_conhecido_centavos` sempre preservam a parte
conhecida. `lucro_centavos`, `retorno_centavos` e `roi` ficam null se o total está
incompleto. `pendentes_financeiras` e `revisoes_financeiras` contam apenas fontes
elegíveis: pendência/revisão de uma fonte contextual excluída não invalida o
resultado da aposta canônica. Uma parcela exclusivamente contextual fica
`fora_do_financeiro`, com totais financeiros null e parte conhecida `"0"`.
Não converter null em zero. Essa política é mais explícita que o
resumo legado da #184, que publica lucro liquidado apesar de pendentes: sua parte
conhecida reconcilia com o resumo quando não há resultados ausentes. Contagens e
população são iguais. `total_periodo` completo acompanha cada página; não somar
totais de páginas. No diário, `subtotal_intervalo` é só a janela devolvida.
Todos os dias mais `sem_data` reconciliam contagens e parte conhecida com o total.
Um total completo só reconcilia numericamente quando nenhuma parcela é incompleta.

Saldo all-time é outra capacidade de `/painel`, com escopo de contas e saldo
conhecido/desconhecido existentes. Não usá-lo como lucro do recorte, nem atribuir
um saldo global ao filtro atual. O diário não fabrica saldo inicial ou acumulado.
MVs do painel legado podem estar atrasadas: sua fonte e frescor permanecem os
publicados no contrato legado; a fonte nova é ao vivo.

## Grupo → filhos

Atribuição parte **do vínculo explícito da aposta canônica**, não do cadastro do
tipster, similaridade de nomes ou grupos associados a outra fonte. Cada fato
ocupa uma única parcela: `atribuida` (exatamente um vínculo válido), `sem_grupo`
(nenhum), `ambigua` (vários) ou `nao_resolvida` (referência não resolvida).
As últimas três parcelas têm ID/nome/ativo nulos; sua chave inclui `atribuicao`.
Tipster tem `atribuicao=atribuido|sem_tipster|nao_resolvido`; ID/nome nulos não
significam zero apostas. Não esconder essas parcelas na tela.

Rótulos vêm do catálogo **atual**, expressamente `rotulos=catalogo_atual`; renomear
não muda identidade ou origem. Grupo arquivado conserva fatos e `ativo=false`.
Os comandos explícitos de associação são auditados; alterar o cadastro não
reescreve vínculos históricos automaticamente. Nomes iguais entre entidades ou
tenants não são chaves; o cadastro já proíbe duplicata exata de nome dentro do
mesmo catálogo/tenant. Identidade alheia e identidade inexistente produzem a
mesma população vazia, sem expor nome ou existência.

A página limita **pares grupo-tipster**, ordenados por categoria de atribuição,
BIGINT do grupo e do filho e atribuição do filho. Um grupo pode reaparecer em
páginas adjacentes: sua `metricas` e `total_filhos` sempre abrangem todos os filhos
do recorte. Faça upsert pela identidade do grupo e acrescente filhos por identidade;
não some novamente a métrica repetida. `total_grupos`/`total_filhos` são completos.
Não há consulta por filho ou grupo no navegador.

Cursores são cifrados/autenticados, expiram em 24h e vinculam usuário, capacidade
e seleção. `PAINEL_CURSOR_SECRET` opcional permite segredo privado dedicado;
ausente deriva por HMAC uma chave separada por propósito do segredo privado
obrigatório existente `COLETA_TOKEN_SECRET`. Workers/reinícios com a mesma
configuração preservam navegação. Não reutilizar JWT público como segredo.
A chave derivada fica em `lru_cache` por processo: alterar a configuração exige
reiniciar todos os workers para adotar o novo segredo. Antes disso, o worker
antigo aceita seus cursores até a expiração de 24h. Depois do reinício, cursores
da chave antiga retornam 422; durante reinício gradual pode haver rejeição por
workers com chaves diferentes. Recomeçar a navegação após concluir a rotação.

## Exportação e erros

XLSX contém Contexto, Resumo, Grupos (uma linha por grupo), Tipsters por grupo e
Diario (todos os dias e eventual linha sem data). Cada aba financeira reconcilia
com Resumo. IDs/dinheiro são células de texto exato, inclusive negativos; rótulos
que poderiam ser fórmulas continuam escapados. SQL diário é dividido em janelas
de até 366 dias, filhos por stream. Não há truncamento silencioso: excesso exige
refinar filtros (422). Respostas e erros: `Cache-Control: private, no-store`,
`Vary: Authorization, Cookie`; XLSX também informa `X-Snapshot-Em`.

O stream de grupos ordena categoria e BIGINT de grupo; filhos usam essas mesmas
chaves, BIGINT do tipster e atribuição do tipster, incluindo categorias sem ID.
O diário da exportação executa até `ceil(dias/366)` consultas limitadas (274 no
máximo de 100000 dias), além das consultas fixas de contexto/resumo/sem data.
Monitorar latência e número de consultas por exportação; não ampliar limites nem
substituir snapshot consistente por cache de resultados financeiros.

## Revisão administrativa de 20/10/2026

Integrar #184 antes de #189 e reconciliar o trecho herdado a partir da main real,
repetindo filtros, diário/hierarquia, contratos e todos os checks. A branch segue
diretamente contra main, sem empilhar. Inclui também as cinco correções da revisão
do #184: catálogo histórico de casa inativa, criação de grupo sempre ativo,
resposta dos vínculos persistidos em ordem canônica, filtros explícitos e guarda
de página sem linhas. Os 52 aceites de filtros anteriores continuam, acrescidos
dos quatro novos; os 37 de diário/hierarquia continuam obrigatórios.

Sem `ate`, o diário inclui o dia civil atual em America/Sao_Paulo. Ele ainda está
em andamento; não representa fechamento do dia. Essa inclusão preserva o painel
atual e não muda o relógio de atribuição de contas (jogo). O aceite de `all`
verifica a presença do dia atual; datas futuras observadas conservam seus fatos.

401: sessão ausente/inválida/expirada; 422: limites, filtros, cursor inválido ou
de outro recorte; 429: limite existente de API/exportação, observar Retry-After;
503: leitura temporariamente indisponível, nunca lista vazia fictícia.
Exemplos: `{"detail":"cursor inválido, expirado ou de outro recorte"}`,
`{"detail":"projeção temporariamente indisponível"}`.
READ_ONLY permite GET e exportação; 402 é bloqueio comercial de escrita,
nunca substitui 401. Sessão cookie mantém CORS exato, HttpOnly e CSRF existentes.

## Prova e consumo pelo frontend

O arquivo inteiro `tests/contract/schemas/openapi.json` é gerado desta única
árvore por `python scripts/generate_openapi.py`; `--check` comprova o snapshot.
O SHA do commit e SHA-256 do documento estão na PR. Após o merge administrativo,
gere novamente tipos **do documento inteiro do SHA integrado**, por exemplo
`npx openapi-typescript caminho/openapi.json -o src/api/schema.d.ts`, usando a
versão já fixada no frontend. Não unir à mão documentos de #184/#187. Revalide
o documento integrado com as três capacidades e os filtros, inclusive as
alterações relacionadas efetivamente aceitas pelo administrador.

Use o mesmo objeto de filtros para resumo, grupos, diário e XLSX. Navegue cursores
sem recalcular limites de período entre páginas. Na troca de filtros cancele
leituras antigas e reinicie páginas. Os exemplos JSON completos neste diretório
(`painel-detalhado-grupos.example.json`, `painel-detalhado-diario.example.json`)
são documentação sintética validável, não captura real nem build público.

Prova automatizada: em banco descartável PostgreSQL 16 migrado, execute
`pytest -n 0 tests/integration/test_site_filters_db.py` e
`pytest -n 0 tests/integration/test_painel_detalhado_db.py --junitxml=panel-detail-results.xml`,
com TEST_DATABASE_URL desse banco; depois `python scripts/check_panel_detail_results.py panel-detail-results.xml`.
O gate exige inventário integral e zero skips, exercita assinatura real, RLS,
datas, hierarquia, filtros, Excel, limites, READ_ONLY e timeout real de SQL.
Contagem de consultas e duração efetivamente exercitada vão nas propriedades JUnit.

Jornada de sessão: subir somente o ambiente descartável de `tests/identity/docker-compose.yml`
conforme suas instruções e executar `pytest -n 0 tests/identity/journey.py tests/integration/test_identity_db.py`.
Ela usa emissor OIDC, e-mail, navegador, API e banco reais; lê as capacidades com
cookie HttpOnly em viewports desktop 1440×900 e mobile 390×844, inclusive outro
usuário. Isso comprova transporte/API; não afirma que a UI frontend #20 está pronta.

Para prova do site, após integração administrativa, apontar seu ambiente privado
de homologação à API migrada e ao emissor já configurado; login hospedado normal,
sem token em localStorage. Em desktop e celular: criar no ambiente de teste
fatos revisáveis com dias conhecidos/pending, associar grupos explicitamente,
arquivar/renomear um grupo, abrir todos os períodos, visitar próxima/anterior,
trocar cada filtro, baixar XLSX, confirmar sinal/valor/null e totais. Repetir com
um segundo usuário e conta READ_ONLY; logout deve resultar em 401. Registrar SHA
do frontend/API/schema, horário, filtros e evidência privada, sem divulgar cookies,
emails ou dados de apostas. Dados sintéticos de aceite não viram coleta pública
nem habilitam leitores/assinaturas/produção. Nenhum provisionamento é necessário
para implementar e revisar esta entrega; ativação permanece com o administrador.

Hooks: executar no ambiente de desenvolvimento instalado com `pip install -e ".[dev]"`.
O MyPy local usa esse ambiente completo e continua estrito para cada arquivo
Python alterado, incluindo testes; suas fronteiras de mocks recebem tipagem explícita.
A CI mantém o MyPy estrito da aplicação e todos os gates existentes.
