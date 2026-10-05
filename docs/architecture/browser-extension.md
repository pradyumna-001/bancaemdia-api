# Fronteira API / cliente de coleta

A extensão é um cliente não confiável. Envia envelopes sanitizados de respostas
observadas durante navegação explícita do usuário. O servidor nunca navega na
casa, faz apostas, clica controles ou chama endpoints privados em nome do cliente.
Host exato, método, caminho sem query, transporte e versão do sanitizador são
proveniência declarada; não são uma prova criptográfica de origem.

A API autentica a instalação (#107), mantém sessões imutáveis, publica o contrato,
persiste o recebimento e deriva casa, identidade do bilhete, datas, estado e hash
canônico pelo parser. Hints de usuário, casa, titular, lucro ou liquidação não
substituem esse processamento. O catálogo de hosts é o catálogo canônico da API:
não há wildcard, correspondência por sufixo nem URL arbitrária.

Materialização, eventos financeiros, matching, titularidade, saldos e totais
permanecem no backend. O v2 utiliza a materialização existente, com conta validada
pelo servidor; não cria outro cálculo financeiro na extensão. Regras adicionais
de matching/reconciliação das issues seguintes não são declaradas concluídas.

O repositório da extensão deve consumir uma versão/checksum publicados. IndexedDB,
armazenamento local de tokens, observação passiva, política de origem do browser,
fila local, ACK deletion, backoff e interface de reconexão pertencem ao backlog
da extensão. Os testes aqui exercitam a fronteira HTTP da API, não simulam uma
entrega da extensão.
