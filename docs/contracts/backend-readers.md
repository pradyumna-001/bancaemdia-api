# Contrato de leitores do backend — #114

O backend recebe evidência sanitizada e produz apostas canônicas. O transporte do navegador pertence ao backlog da extensão; o navegador não calcula stake, retorno, resultados ou atribuição financeira. Este contrato interno é aditivo: não acrescenta endpoint HTTP nem altera o coletor legado. A migração dos seis leitores herdados e a prova de materialização persistida são a #115, com capturas reais recentes revisadas. A registry de produção começa vazia, por falta dessa evidência.

## Envelope e seleção do leitor

`ReaderEnvelope` versão 1 exige `brand`, `hostname` final exato normalizado (IDNA, minúsculas), `source`, `captured_at` com fuso, `raw_schema_version`, `content_type=application/json`, `payload_text` e `content_hash` SHA-256 dos bytes UTF-8 do texto. O limite é 128 KiB; o chamador deve limitar o corpo HTTP antes de decodificar. JSON repetido, números não finitos, estruturas excessivas e dados pessoais/credenciais são recusados. O texto preserva precisão decimal; não passa por conversão intermediária em float.

`source` contém `channel=fetch|xhr|websocket`, caminho `endpoint` sem query/fragmento, `direction=observed_response|received_frame` e, somente para WebSocket recebido, `frame_signature` revisada. Não há envio de frames, captura ativa nem URL de handshake. Reconstrução de streams reais pertence à #116.

Uma `ReaderRegistration` vincula marca, domínio, versão do envelope, versão do schema bruto, tipo de conteúdo, canal, caminho e assinatura exatos. Duplicação dessa chave falha ao construir a registry. Nenhum prefixo, wildcard, provedor, substring, primeiro resultado ou leitor padrão é usado. Domínios desconhecidos falham com `wrong_host`; versões/origens desconhecidas falham com `schema_drift`.

## Saída e identidade

Cada `CanonicalBet` contém identidade externa da aposta, marca, domínio, horário de registro e revisão da fonte, estado, stake em centavos inteiros, odd Decimal, retorno autoritativo e seleções com identidade, nome, início com fuso, descrição, mercado/odd/resultado opcionais. O retorno final é obrigatório em liquidações; uma aposta aberta não afirma retorno final. Não se infere ganho multiplicando stake por odd, nem se arredonda centavo fracionário. Campos extras e resultados não suportados falham explicitamente.

`game_at` é o primeiro início de jogo das seleções em UTC. A atribuição temporal de conta e o dia financeiro usam o jogo, conforme decisão do usuário; `placed_at` preserva apenas o registro da fonte. Multicontas deve transportar e validar a conta que efetivamente fez a bet no contrato autorizado de ingestão. Identificadores de contas de sessão da casa não entram em fixtures. Nenhum leitor escolhe uma conta pelo menor ID, pelo momento de ingestão ou pelo momento de registro da bet.

`identity_hash` usa marca + domínio + identidade externa. `content_hash` usa bytes da captura, enquanto `canonical_hash` usa a saída canônica ordenada e normalizada. Novo horário de captura não altera identidade ou conteúdo canônico; open → settled muda conteúdo e mantém identidade. `GoldenReplay` é um oráculo do contrato em memória: repetição é `noop`, revisão posterior é `updated`, conflito/versão antiga é recusado. Isso não substitui constraints, transações, eventos e leituras financeiras da materialização, a validar na #115.

Erros públicos possuem somente `error_code` e razão enumerada: `schema_drift`, `unsupported_market`, `incomplete_payload`, `wrong_host`, `unsafe_payload`. Falhas inesperadas do parser viram erro de schema, sem devolver texto da exceção ou valores capturados. Um reader nunca retorna vazio como sinal de sucesso.

## Quarentena transacional

`services.reader_capture.parse_or_quarantine` exige sessão autenticada com `app.current_user_id` igual ao usuário ativo informado. O usuário nunca vem do envelope. Sucesso devolve a saída canônica; erro registra `reader_quarantine` na transação do chamador, sem commit interno e sem escrever apostas/eventos/projeções financeiras. Captura insegura armazena apenas digest irreversível, código e razão. Outros erros preservam envelope somente quando passa por toda a sanitização; tipo desconhecido não retém corpo.

A chave única usuário + digest + código + razão torna retry concorrente idempotente. A tabela tem RLS forçada com políticas SELECT/INSERT do proprietário; a aplicação não atualiza nem apaga evidência. Retenção legítima exige procedimento administrativo privilegiado. Métrica `reader_capture_outcomes_total{outcome,error_code}` e log `reader_capture_quarantined` usam apenas valores enumerados, nunca ticket, usuário, token, hostname ou conteúdo como label.

Migration `c114reader2026` segue a main `a9d6e3f1c210`, sem reescrever revisões publicadas. Downgrade só remove tabela vazia; havendo evidência, recusa. Rollback operacional: voltar a aplicação e conservar schema/evidência. Antes de integrar PRs que criem outras pontas Alembic, o administrador/DBA deve reconciliar a árvore e repetir acceptance na composição; os resultados de cada branch não comprovam a composição.

## Adicionar um domínio exato

1. Confirmar domínio final e origem mínima com captura autorizada. Sanitizar localmente, mantendo somente campos necessários à leitura. Nunca pedir senha, cookie, bearer, HAR completo, perfil, sessão ou identificador de conta/pessoa. Não apresentar exemplo sintético como captura real.
2. Implementar `BackendReader.parse(envelope)` com versões explícitas, Decimal e saída `CanonicalBet`. Registrar cada domínio/origem exatos independentemente; reuso de plataforma requer evidência por domínio.
3. Preparar fora do Git um diretório com `manifest.json` e, para cada caso, `<nome>.raw.json`, `<nome>.envelope.json`, `<nome>.golden.json`. O envelope preserva exatamente o texto bruto e seu hash. Escrever golden independentemente do parser. Cobrir os estados observados, simples/múltipla, moeda, opcionais, replay/atualizações e amostra explicitamente não suportada. Declarar ausências observáveis.
4. Manifesto: `fixture_set_version` e `reader_version` semver, `reader_id`, `brand`, `hostname`, `raw_schema_version`, `evidence_kind=synthetic|sanitized_real|legacy`, `last_real_capture_at` (fuso obrigatório somente para real, null para demais), lista `fixtures` com nome, estado coberto e hashes SHA-256 dos três arquivos. `bundle_digest` calcula SHA-256 do JSON canônico do manifesto excluindo apenas `review`.
5. Pedir a um humano autorizado que revise o pacote completo fora do histórico Git. Ele deve publicar comentário nesta issue/PR no GitHub com a frase `Approved reader fixture bundle SHA256: <digest>`. Não usar bot ou aprovar em nome do revisor. Preencher `review.status=approved`, `review.reviewer=<login humano>`, `review.reference=<URL do comentário>` e `review.bundle_sha256=<digest>` somente depois da aprovação. O scanner verifica identidade humana, associação OWNER/COLLABORATOR/MEMBER e atestado exato pela API GitHub; precisa de `gh` autenticado ou `GH_TOKEN` na CI.
6. Rodar o gate **antes do primeiro git add/commit**: `python scripts/validate_reader_fixtures.py --base-ref origin/main --report reader-capabilities.json`. Instalar os hooks locais com `pre-commit install`; o hook também verifica antes de commit. A CI é defesa adicional, não pode apagar um segredo já publicado no histórico. Qualquer alteração de bytes/envelope/golden invalida a aprovação e requer novo digest, versão maior e revisão humana explícita. Não editar fixtures legadas para contornar o manifesto. `.gitattributes` mantém os bytes dos pacotes revisados iguais no Windows/Linux.
7. Rodar testes de contrato e acceptance PostgreSQL. Acrescentar registro de produção somente com evidência adequada; o exemplo `synthetic_reference` atende exclusivamente `reader.example.invalid` e não prova suporte de casa real.

## Versão da API e dependências

O wire contract do PR #164 / #108 usa payload estruturado e versão de coleta própria; não é o `ReaderEnvelope` interno. Uma ponte deve preservar bytes/decimais, schema bruto, domínio final e origem, com versão coordenada API/extensão, compatibilidade atual/anterior e ACK/retry conforme #108. Não mudar silenciosamente a versão 2 existente nem chamar o leitor novo a partir de materialização legada sem prova completa. Os PRs #163/#164/#166/#170/#171 permanecem independentes; este PR nasce diretamente da main. O catálogo pode consumir o relatório futuramente, mas não pode promover suporte técnico com base em exemplos sintéticos.

## Evidência e pendência humana

CI exige lint, formato, mypy, suíte/cobertura, OpenAPI, scan de secrets/PII e revisão de fixtures, auditoria de dependências/código, acceptance PostgreSQL 16 sem skips e Docker. Os testes de algoritmo criam entradas sintéticas transitórias; as simulações do gate não são aprovação humana nem fixtures de integração revisadas.

Os 16 cenários obrigatórios de quarentena usam banco descartável separado da regressão herdada: esta contém downgrades históricos, que não podem apagar a evidência criada pela acceptance nova. A regressão herdada mantém exatamente os quatro skips históricos declarados (três exigem hot standby real, não utilizado pelo contrato #114, e um é o placeholder antigo de conferir_numeros). O gate recusa qualquer skip na acceptance dos leitores e qualquer skip adicional na regressão. Nenhum skip foi introduzido para a #114.

O PostgreSQL descartável recebe senha aleatória de 256 bits a cada execução, mascarada antes do uso e transmitida ao container por variável de ambiente. A porta só é publicada em loopback; autenticação e RLS permanecem exigidas. O container é removido ao final. Nenhuma credencial fixa de banco é adicionada ao workflow. O job k6 de staging exige endereço/credenciais do ambiente externo; sem STAGING_BASE_URL continua explicitamente inaplicável, enquanto a validação local dos cenários permanece obrigatória.

`reader-capabilities.json` distingue sets revisados e amostras legadas: quantidade, estados, última captura real, versões, pass/fail e razão de drift. Zero sets revisados é exposto como `no_human_reviewed_fixture_sets`, sem alegação de cobertura. Amostras legadas não possuem evidência de domínio/versão/revisão atual e permanecem com `production_support_proven=false`.

O usuário determinou **aguardar revisão do administrador** para o pacote candidato de 11 fixtures da #114. Ele fica fora do Git até aprovação exata. A infraestrutura/testes independentes podem ser revisados; o aceite completo de fixtures da #114 permanece pendente dessa decisão e da inclusão revisada. Não encerrar a issue nem declarar suporte real às casas.

Nesta campanha o revisor autorizado é `pradyumna-001`, proprietário/administrador. A conta de automação `wfcgit-hub` não pode atestar revisão, mesmo que a API GitHub classifique seu token como `User`. Acrescentar outro revisor exige alteração explícita da política e revisão administrativa; não é campo controlado pela fixture.
