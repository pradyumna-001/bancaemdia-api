# XLSX — prévia, remapeamento e confirmação

Backend #196 desbloqueia frontend #26/#50, conforme ADR-019 e a decisão do titular. Este contrato é candidato, sem merge/deploy. Não altera frontend/extensão. Confirmar significa aprovar os **dados e alterações**; nenhum preço, estimativa de custo, consentimento de gasto ou autorização financeira de processamento é publicado. Limites internos pertencem ao backend.

## Operações e acesso

Prefixo `/api/v1/planilhas`. Todas exigem usuário ativo autenticado pelo sistema existente; cookie HttpOnly, Origin exata e X-CSRF-Token nas mutations. CSRF/token não ficam em storage. Bearer só no modo legado habilitado pelo servidor.

| Método / caminho | Corpo / resultado | Matriz comercial |
| --- | --- | --- |
| GET `/campos` | catálogo de campos, labels, tipos, opções e limites | FULL_WRITE/READ_ONLY |
| PUT `/fontes/{source_id}` | `{nome}`; registra identidade explícita estável | FULL_WRITE |
| GET `/fontes` | página de fontes próprias, total, page/page_size | FULL_WRITE/READ_ONLY |
| GET `/fontes/{source_id}` | reconcilia o UUID conhecido antes do PUT | FULL_WRITE/READ_ONLY |
| POST `/fontes/{source_id}/previas/{preview_id}` | multipart exatamente um `arquivo` XLSX; 201 com prévia real | FULL_WRITE |
| GET `/previas/{preview_id}` | referência/hash/versão/estado/contagens/abas/cabeçalhos | FULL_WRITE/READ_ONLY |
| GET `/previas/{preview_id}/linhas` | página de valores originais, normalizados, erros e consequências | FULL_WRITE/READ_ONLY |
| POST `/previas/{preview_id}/remapear/{new_preview_id}` | `{expected_revision,sheet,mapping}`; 201 com nova versão | FULL_WRITE |
| POST `/confirmacoes/{confirmation_id}` | `{preview_id,preview_hash}`; resultado atômico real | FULL_WRITE |
| GET `/confirmacoes/{confirmation_id}` | reconciliação de aplicação, inclusive resposta perdida | FULL_WRITE/READ_ONLY |

UUIDs são conhecidos e persistidos pelo cliente **antes** de cada mutation, sob o usuário da sessão. A consulta de fontes permite selecionar/reutilizar a mesma fonte, sem depender de nome de arquivo, hash ou UUID novo a cada upload. Apenas page/page_size são query params; page>=1, page_size 1–100, padrão 50. Não truncar listas a partir de contagem: total informa todos os registros e data informa somente a página.

Nova prévia/remapeamento exigem FULL_WRITE porque persistem material privado de importação; o verbo POST sozinho não decide a matriz. Prévia nunca escreve aposta, evento, movimento, saldo, consolidação ou assinatura. Leituras de material aceito permanecem em READ_ONLY. Confirmação revalida FULL_WRITE, inclusive no guard SQL. 401 é sessão; 402 é restrição comercial. Pagar não repete a intenção: exige ação explícita e referência ainda válida. Abortar observação/logout/refresh não cancela nem autoriza replay.

## Identidade da fonte e da linha

Registro da fonte é uma ação explícita para uma **nova fonte do modelo com IDs estáveis**. `origem_id=web-xlsx:{source_id}` é retornado pelo servidor, imutável e diferente da identidade legada aba/linha. Nome é somente label, não identidade. A fonte e seu contador de aplicações permanecem até exclusão da conta; limite de 1.000 fontes próprias. PUT repetido com mesmo nome retorna a fonte; conteúdo incompatível retorna identity_conflict.

O modelo novo exige `linha_id` textual, único entre as linhas selecionadas da fonte e estável durante toda a vida da aposta; 1–128 caracteres ASCII `[A-Za-z0-9][A-Za-z0-9._:-]*`. Pode ser um ID original persistente ou um ID criado uma única vez na própria planilha. **Não gerar IDs por posição, nome de aba, upload ou hash de dinheiro.** A chave financeira é `s:SHA256(origem_id + ':id:' + linha_id)`, calculada no backend. ID repetido na aba é uma recusa, sem aplicação parcial.

Renomear aba ou reordenar linhas mantém as mesmas apostas; inserir uma linha com novo ID cria nova aposta; remover uma linha do arquivo não exclui/restaura a aposta existente. Alterar `linha_id` significa novo vínculo/possível nova aposta, nunca correção de posição. Reutilizar um ID para outra aposta não é uma forma de migrar dados. A versão `atualizada_em` mantém a regra existente: mais recente atualiza, igual/mais antiga ignora, sem evento novo. Alterar conteúdo sem avançar essa versão é ignorado explicitamente.

POST `/api/v1/apostas/importar-planilha` continua sendo importação **direta**, com a identidade legada origem+aba+linha; não é chamado por esta jornada. A nova API não adota/migra silenciosamente fontes legadas, cujos registros não guardam um catálogo recuperável de origem/IDs de linha. Migrar uma fonte já importada requer reconciliação explícita dos vínculos pelo administrador; criar uma fonte nova para o mesmo histórico pode duplicar apostas. A SPA deve apresentar essa distinção e não oferecer novo cadastro como recuperação de rede. A operação direta recusa mistura com o namespace de uma fonte nova já registrada pelo mesmo dono. As garantias de IDs estáveis valem dentro da fonte selecionada; não há dedupe semântico prometido entre fontes arbitrárias.

## Leitura, mapeamento e versões

Apenas XLSX OOXML real do modelo em unidades da main, com unidade vigente em BRL. Moedas/financeiro USDT da PR #185 não fazem parte desta candidata. XML usa parser hardened, proíbe DTD/entities/external em toda codificação antes de abrir o workbook. Não suporta XLS/CSV/export externo nem executa fórmulas/macros/links. `GET /campos` publica o catálogo autoritativo: linha_id, casa, data_aposta, odd, stake_unidades e atualizada_em obrigatórios; evento, descricao, mercado_bruto, freebet, data_jogo e conta_casa_ref opcionais. Datas ISO 8601 e células de data são interpretadas pelo servidor; sem offset usam America/Sao_Paulo. Dinheiro/odd aceita número real ou decimal brasileiro com vírgula; stake é **unidades**, não reais. Freebet aceita booleano/0/1/sim/não/true/false; null/ausente significa false conforme o modelo existente.

Cabeçalhos únicos conhecidos recebem mapeamento inicial por nome. Cabeçalho duplicado não escolhe arbitrariamente uma coluna: os campos ficam sem mapeamento e aparecem missing_mapping. Mapping usa campo -> índice de coluna **1-based**, nunca label ambígua; colunas trocadas podem ser corrigidas, campos desconhecidos/fora do cabeçalho/mesma coluna para dois campos recebem invalid_mapping/duplicate_mapping. Valores originais continuam disponíveis mesmo inválidos. Correção de célula requer editar o XLSX e enviar nova prévia; não há endpoint de edição de célula.

Cada prévia seleciona exatamente **uma aba**, inicialmente a primeira; sheets informa cabeçalhos e total de linhas de todas as abas, total_rows/counts referem-se à aba selecionada. Não afirmar que todas as abas foram importadas. Remapear permite escolher outra aba e produz nova referência/hash/revision. Novo upload aceito sob a mesma fonte também invalida a prévia anterior; arquivo recusado não cria uma versão. Remapear com o mesmo mapping e novo UUID permite revalidar após alteração concorrente. A referência antiga nunca é reativada por replay.

Prévia é síncrona e limitada, sem worker/fila/estados de upload Telegram. State ready significa validada naquele snapshot; invalid publica recusas por linha/campo; obsolete indica fonte/arquivo/mapping/fatos alterados. Rows publica consequence create/update/ignore/refuse; counts traz criadas/atualizadas/ignoradas/recusadas/revisoes, sem inferência financeira na SPA. `stake_centavos`, `valor_aposta_centavos`, `conta_casa_id` e review_reason são calculados no domínio. Null inválido/desconhecido não vira zero; freebet tem stake financeiro zero pelo domínio e valor da aposta preservado. Conta padrão usa **dia do jogo**; referência explícita de multicontas mantém a conta real. Ausência/ambiguidade de conta encaminha à Revisão real na aplicação, sem inventar EM_USO ou conta pela casa.

## Confirmação, obsolescência e recuperação

A confirmação usa referência/hash da prévia, não recebe bytes novamente. O backend valida dono, prazo, fonte/current_preview, source_version, hash e todas as linhas. Recalcula o plano sobre fatos e entradas temporais do **primário**, protege as chaves do materializador e aplica sob transação SERIALIZABLE. Alteração de aposta/evento/conta/unidade/uso/revisão/consolidação/movimento invalida o snapshot conservadoramente; 409 preview_obsolete exige nova prévia/revalidação, sem aplicação parcial. A serialização também recusa conflitos durante a aplicação. Toda linha deve ser válida: qualquer refuse impede a transação inteira; Revisão de conta é um resultado válido de domínio.

`revision` cresce por referência da fonte; `source_version` cresce somente após aplicação durável, mesmo se todas as linhas forem ignoradas. Intenção de confirmação é usuário+confirmation_id+preview_id+preview_hash. Após commit, mesmo UUID/pedido devolve resultado durável, sem recriar eventos/apostas/revisões, enquanto válido e com escrita permitida; conteúdo incompatível recebe identity_conflict. Outra intenção não reaplica uma prévia já consumida: a source_version mudou. Concorrência pode produzir 409 por serialização; isso exige **GET do UUID original**, nunca retry automático da mutation.

Fluxo de resposta perdida: persistir IDs sem credenciais antes da ação; POST uma vez; em timeout/503/409 ambíguo consultar GET `/confirmacoes/{confirmation_id}`. completed contém resultado real, counts e rows (linha_id, aba, número, outcome created/updated/ignored, bet_key e review_ids próprios). 404 significa ausência/inacessibilidade e não autoriza replay automático. 410 significa expiração; não criar uma fonte nova para recuperar. Uma nova ação explícita pode reapresentar o mesmo pedido enquanto válido; não inventar resultado por HTTP 200/201, tamanho de lista ou ausência de erro. Resultados financeiros vêm do materializador, não de contagens de prévia.

Agregados/materialized views têm frescor **eventual**, conforme rotina existente. Refetch não promete refresh forçado. O resultado durável e as apostas/eventos do primário comprovam a aplicação; não confundir candidata/sandbox com publicação produtiva.

## Limites, privacidade, erros e retenção

5.000.000 bytes por XLSX (mais margem multipart da infraestrutura), 10 abas, 1.000 linhas de dados **no total do workbook**, 64 colunas, 64.000 células armazenadas, 20 MiB descompactados, ratio máximo 100 por entrada ZIP, 2.000 entradas, 1.000 caracteres por célula. Validação de coordenadas/quantidade antes de iterar impede dimensões forjadas de criar ranges ilimitados. Orçamento de análise é 10s, conferido nas fases/iterações limitadas; não é promessa de latência HTTP. Orçamento total da operação é 30s; timeout responde 503 e exige GET da intenção, pois resposta perdida durante commit não prova rollback. SQL mantém statement timeout existente. 20 prévias/remapeamentos por dia UTC por usuário, configurável positivamente via SHEET_DAILY_PREVIEW_LIMIT; replay de identidade já aceita não consome nova admissão. Controle interno sem preço/consentimento.

400 invalid_multipart; 413 limites de corpo/expansão/estrutura/tempo/célula; 422 empty_xlsx/invalid_xlsx/unsafe_xlsx/invalid_import_request/sheet_not_found/preview_has_errors; erro por campo missing_mapping/invalid_mapping/duplicate_mapping/duplicate_row_id/invalid_value/formula_or_cell_error/invalid_account_reference/financial_value_out_of_range; 404 import_not_found; 409 identity_conflict/preview_obsolete; 410 import_expired; 429 preview_quota com Retry-After conservador 86400s; 503 import_unavailable com Retry-After 5s. Rate limit genérico mantém seu envelope existente. 401/402/403 usam sessão/billing/CSRF existentes. Erros não publicam SQL/stack/provider/dump de input. Textos opcionais que poderiam se tornar fórmula em exportação são recusados, preservando o valor bruto na prévia; as exportações existentes também escapam fórmulas.

Arquivo original não é persistido nem publicado: apenas hash e células privadas cifradas, sob chave derivada separada do segredo do servidor e tabelas forced RLS. Nada de URL assinada/storage reference/browser token. Prévia/células expiram em 24h da primeira aceitação; remapear não prolonga esse prazo. Resultado detalhado de reconciliação expira em 90 dias. GET recusa imediatamente após prazo; purga física usa `SELECT sheet_import_purge()` com role de manutenção (PUBLIC sem permissão), integrada ao procedimento periódico do operador; executar pelo menos diariamente. A função só apaga material expirado, inclusive em READ_ONLY, e não edita finanças. Metadados mínimos/hash/identidades e fonte/versionamento permanecem até exclusão da conta, preservando não repetição após expiração. Exclusão autenticada da conta remove material de importação via rotina existente; logs/telemetria nunca recebem células/bytes.

## Dependências, contrato e prova

Branch diretamente sobre main b916f54331f14cf47a3800324bd61d8638043c06. Código próprio desta issue; reutiliza o materializador, validação temporal, revisão e guards já integrados na main. Reaproveita infraestrutura de desempenho já validada na #191 (`552c4ffe3924df3eac2caa50ea4f5df4c553b816`): consultas SQL parametrizadas/compilação, billing com relógio e permissão vivos, autenticação de instalação com lock/revogação preservados, eliminação de consulta repetida, streaming/contexto ASGI, benchmark isolado de 16k apostas e diagnósticos numéricos de carga. Usa a versão recortada da #193 (`71f1a1acba17f2b99e8dcd55c25c672c98f43967`) de `aposta_repo.py` e `middleware/router.py`, para preservar somente esses ajustes compartilhados. Não herda filtros/Hipotética/foto/schema dessas candidatas; não exige seus merges para testar a árvore. NOWAIT, gates e thresholds permanecem intactos. Arquivos reaproveitados estão enumerados na PR. Administrador deve reconciliar heads de migração, roteador/contrato e mudanças compartilhadas ao integrar candidatos; uma única aplicação integrada deve regenerar OpenAPI. Nenhum schema de branch foi combinado manualmente.

OpenAPI inteiro gerado em `tests/contract/schemas/openapi.json`, referência `docs/API.md`. SHA exato/hash SHA-256 versionado ficam na PR. Exemplo mínimo real: `tests/fixtures/sheet_import/synthetic-import.xlsx`; gerador `python scripts/build_sheet_import_example.py` (dados inteiramente sintéticos).

```sh
pip install -e '.[dev]'
# PostgreSQL 16 descartável; fixtures criam role real NOBYPASSRLS e migram a árvore.
TEST_DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/disposable \
pytest tests/integration/test_sheet_import_db.py -n 0 --no-cov --junitxml=sheet-import-results.xml
python scripts/check_sheet_import_results.py sheet-import-results.xml
python scripts/generate_openapi.py --check
```

Gate exige 51 casos obrigatórios sem faltantes/duplicações/falhas/skips: API/PG/RLS/materializador reais, snapshot sem escrita em prévia/remapeamento, dados inválidos preservados, remapeamento, confirmação/reimportação/obsolescência/expiração/concorrência/resposta perdida real, 401/402/cookie/CSRF/revogação, isolamento, freebet/null/decimais, dia do jogo/multicontas, legado, limites/estrutura maliciosa, 429/503 e retenção. Nenhuma chamada paga, serviço produtivo ou resultado financeiro pronto foi usado. Gates anteriores/cobertura/segurança/carga continuam exigidos no SHA final. A PR fecha #196; não fecha a tela frontend #26.
