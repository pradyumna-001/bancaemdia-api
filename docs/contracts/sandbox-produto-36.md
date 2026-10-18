# Sandbox completo do site — frontend #36

Árvore real de ensaio, sem merge administrativo nem autorização de publicação.
Pai: 5f5f06276114d35e062db45bc54c61c4cb6fc3ec (128 operações).
Fonte adicional: PR #191, 552c4ffe3924df3eac2caa50ea4f5df4c553b816.

Reaplica dessa fonte o domínio, repositório, rota, regras financeiras exatas e testes
específicos da Hipotética, sem substituir as operações existentes. Reconcilia somente
registro de rota/OpenAPI, permissão explícita, fresh no roteador e privacidade HTTP.
A composição inicial 2582511 omitiu o ramo de transporte da #191 para
Hipotética com identidade desabilitada, detectado pelos aceites PostgreSQL.
O ramo é preservado junto ao Painel filtrado: Cache-Control private/no-store
e Vary Authorization/Cookie, sem alterar os demais caminhos. Uma regressão
isolada cobre ambos os modos de identidade e os caminhos financeiros herdados. As demais fontes
do pai (#189/#187/#193/#195/#197/#199/#201/#203/#205/#207) permanecem preservadas.
OpenAPI inteiro gerado pelo aplicativo; nenhum schema concatenado.

Verificações locais: Ruff/formato completos; 19 casos de domínio/contrato da Hipotética.
PostgreSQL/aceites e as jornadas públicas são exigidos pelo workflow frontend da #36,
além de todos os gates anteriores no mesmo pin. Nenhum leitor real de casa ou pagamento
live é certificado. Após os merges originais, pin integrado e ensaios devem ser refeitos.
