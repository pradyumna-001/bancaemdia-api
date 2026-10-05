# Análises avançadas do painel

`GET /api/v1/painel/analises` usa os mesmos filtros e a mesma janela civil de
`GET /api/v1/painel`. Lê `painel.apostas_metricas`, a fonte financeira canônica
do painel, pelo wrapper privado `public.painel_analises_apostas`. A leitura
ocorre no snapshot da réplica, sem refresh de materialized view. O campo
`fonte_dados` identifica essa leitura ao vivo; `atualizado_em` e
`idade_mv_segundos` descrevem **somente** as séries materializadas da #30.
Logo, uma atualização recente pode aparecer na análise antes de aparecer no
resumo materializado.

Todos os recortes incluem apostas selecionadas sem revisão grave, inclusive
pendentes e anuladas. Essas duas classes somam zero nas contribuições
financeiras. Cada recorte particiona a população inteira e pode ser somado
pelos campos inteiros de `total_filtrado`. Odds ausentes ou inválidas usam
`unknown`; esporte é obtido da competição canônica e, se ausente, usa
`unknown`. O mapa usa o fuso IANA configurado em
`PATCH /api/v1/painel/preferencias`; a janela do painel continua no calendário
de São Paulo para preservar o contrato da #30. Dias da semana são `0=segunda`
até `6=domingo`; períodos são intervalos de seis horas a partir de 00:00.

As faixas de odds são `[1, 1.50)`, `[1.50, 2)`, `[2, 3)`, `[3, 5)` e `[5, ∞)`.
Odds até 1, não finitas ou ausentes entram em `unknown`. A odd média considera
somente apostas liquidadas com odd válida. `odds_desconhecidas` conta
liquidadas sem odd válida e `odds_nao_aplicaveis` conta pendentes e anuladas.
Profit factor é a soma dos lucros positivos dividida pelo módulo da soma dos
lucros negativos; sem perda, retorna `null`.

Os quartis usam o valor de face observado; freebets usam
`valor_aposta_centavos`. Limites repetidos se fundem, de modo que uma amostra
de valores iguais gera um único bucket. Valor de face ausente ou não positivo
vai para `not_applicable`. Progressão por banca divide o lucro pelo
`saldo_inicial_centavos` configurado na banca; banca sem esse valor ou com
zero retorna `null` e um motivo.

Na série de evolução existente, `lucro_*` representa apenas apostas
liquidadas. Depósitos e saques aparecem separadamente, com saques positivos
na saída. A identidade de cada ponto é
`saldo_centavos = saldo_inicial_centavos + lucro_acumulado_centavos + depositos_acumulados_centavos - saques_acumulados_centavos`.
Transferências, bônus e ajustes não entram nessa identidade; o saldo da série
é o saldo teórico dessa composição, enquanto o saldo de contas da #30 tem seu
próprio escopo. Sem saldo inicial conhecido, a série retorna saldo `null`.

Metas são privadas por usuário, têm intervalo civil inclusivo e métricas
revisadas (`lucro_centavos`, `giro_centavos`, `roi`, `win_rate` e
`total_apostas`). O progresso é calculado a partir das contribuições canônicas
ao consultar a meta. `DELETE` arquiva a meta sem apagar histórico. Todas as
rotas autenticadas usam `Cache-Control: private, no-store` e
`Vary: Authorization, Cookie`.

## Papel HTTP separado do papel de migração

Se a implantação usa papéis PostgreSQL distintos, conceda ao papel HTTP acesso
à nova superfície pública e à tabela de metas depois da migração, substituindo
`bancaemdia_app` pelo papel real:

```sql
GRANT SELECT ON public.painel_analises_apostas TO bancaemdia_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.metas_desempenho TO bancaemdia_app;
GRANT USAGE, SELECT ON SEQUENCE public.metas_desempenho_id_seq TO bancaemdia_app;
GRANT UPDATE (fuso_horario) ON public.usuarios TO bancaemdia_app;
```

O papel HTTP continua sem `USAGE` no schema privado `painel`. A política RLS
de `metas_desempenho` também exige `app.current_user_id` na sessão.
