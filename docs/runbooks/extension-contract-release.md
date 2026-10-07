# Publicação do contrato da coleta

Escopo: API e artefatos canônicos. Não distribui extensão nem altera seu repositório.

## Gerar e verificar

```sh
python scripts/generate_openapi.py
python scripts/generate_collection_contract.py
python scripts/generate_openapi.py --check
python scripts/generate_collection_contract.py --check
pytest tests/contract/ -n 0
pytest tests/integration/coleta/ tests/integration/test_coleta_db.py tests/security/test_coleta_tokens.py -n 0 --junitxml=pairing-results.xml
```

O segundo conjunto exige PostgreSQL 16 real via TEST_DATABASE_URL descartável ou
Docker/Testcontainers. Ausência de infraestrutura falha; não gera skip nos
cenários obrigatórios. A CI verifica schemas, cenários HTTP, RLS restrito, um
processo novo de worker, migrations, scanners, cobertura e Docker.

Fixar o commit e os SHA-256 publicados no consumo do contrato. Comparar o arquivo
exato (UTF-8 com LF final) com artifact_sha256. Não usar números de versão do
browser ou dados do cliente como prova de integridade do artefato.

## Banco, processos e implantação

c108v22026 segue c107pair2026 do PR #163. Aplicar migrations no primário com role
administrativa que enxerga RLS; a API/worker usam a role restrita. Não transferir
coleta_pending_deliveries para um proprietário sem a visibilidade administrativa
necessária. A função SECURITY DEFINER expõe apenas até 500 pares de IDs de roteamento,
com search_path fixado e EXECUTE revogado de PUBLIC. Se a role for criada depois:

```sql
GRANT EXECUTE ON FUNCTION coleta_pending_deliveries(integer) TO bancaemdia_app;
```

Conceder também os grants usuais de tabelas/sequências sob as policies RLS. Manter
API, worker materialization e Beat ativos. A tarefa materialization.collection_v2
é roteada à fila materialization; a entrada collection-v2-inbox é acrescentada à
configuração Beat preservando as demais. O aviso feito pela API é apenas redução
de latência; o banco é a fonte de trabalho.

Antes de liberar clientes v2, verificar com dados sintéticos: abrir sessão,
enviar lote misto, obter ACK, consultar status terminal, repetir o mesmo item,
confirmar uma aposta/um evento de criação e comparar hashes publicados. Não
colocar token em URL, comando de diagnóstico, log ou artefato de CI.

Monitorar coleta_submission_total{contract,result} e
coleta_terminal_total{contract,status}. O backlog é observável por status/criada_em
na inbox com consulta administrativa; não transformar IDs em labels de métricas.
Falha de broker: restaurar broker/Beat/worker; a inbox continua pendente.
Falha de banco: restaurar disponibilidade e observar novo poll.
failed/needs_review: investigar com IDs e motivos, sem imprimir envelopes.
Antes de reprocessar um failed após corrigir o parser, usar procedimento
administrativo revisado: manter a mesma entrega/evento e seu ACK, resetar
tentativas/status para pending e preservar o payload original. Nunca emitir
outro evento para mascarar uma falha. O reprocessamento financeiro permanece
idempotente pela chave de negócio.

## Rollback e evolução

O downgrade destrutivo é recusado quando existem sessões ou entregas. Preferir
rollback de aplicação compatível com o schema expandido; não apagar a inbox
para fazer a migration passar. A reversão vazia e o upgrade estão testados em
banco isolado. Instalações e histórico anteriores permanecem preservados.

Em um problema de rollout v2, interromper a adoção de novos clientes v2 e manter
o último serviço v2 funcional (rotas de ACK/status e worker) para drenar entregas
aceitas; clientes v1 continuam nas rotas legadas. Não voltar toda a aplicação
a um binário que desconhece uma inbox ativa, nem republicar hash antigo sobre
schema novo. Não emitir ACK de algo que um rollback perderia.

Major seguinte: publicar N+1 junto de N, registrar contratos/SHAs dos dois lados,
anunciar data com pelo menos 90 dias e só retirar N-1 após revisão administrativa.
Nesta versão não foi anunciada retirada do v1.

## Dependências de integração

Base técnica: PR #163, HEAD 85f4a3941ccb01ecf4af3b3823b402888146e458; seu merge cabe
ao administrador. A #108 está preparada sobre ele e não copia outras cadeias.
Na convergência com #134/#135, preservar a conta explícita validada pelo horário
de colocação (nunca usar fallback para primeira conta). Com billing, preservar
seu gate de escrita também na admissão e processamento da inbox. Com privacidade,
incluir sessões e entregas nas regras de exclusão/retenção da conta. Esses PRs
não estão na base desta entrega, e seus escopos não são declarados implementados.

Na parte 4, h4review2026 preserva a convergência publicada; j168base2026/j4main2026 acrescentam a main aceita, incluindo identidade OIDC. A ponta atual é j4main2026. A CI valida diretamente a árvore de produto com pareamento, contas #177, catálogo e quarentena; sem patch externo ou revisão descartável.
