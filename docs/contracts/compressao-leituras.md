# Compressão gzip das leituras JSON — contrato da issue #208

Destino: [frontend #37](https://github.com/pradyumna-001/bancaemdia-frontend/issues/37) e [frontend #106](https://github.com/pradyumna-001/bancaemdia-frontend/issues/106). Implementação em [`src/bancaemdia/middleware/compression.py`](../../src/bancaemdia/middleware/compression.py). O OpenAPI não muda: a compressão é negociação HTTP, não campo da resposta.

## Política

A API comprime com gzip (nível 6) somente quando **todas** as condições valem:

- método `GET`;
- caminho exato, depois de remover `root_path`, em `COMPRESSIBLE_READS`;
- o pedido traz `gzip` em `Accept-Encoding`;
- o corpo tem pelo menos 1.024 bytes;
- a rota ainda não definiu `Content-Encoding`.

| Rota comprimível | Origem |
| --- | --- |
| `/api/v1/apostas` | main |
| `/api/v1/painel` | main (evolução de caixa) |
| `/api/v1/painel/analises` | main (resposta única ao vivo, regra 55) |
| `/api/v1/painel/filtrado`, `/filtrado/metricas` | PR #184 |
| `/api/v1/painel/filtrado/diario`, `/filtrado/grupos` | PR #189 |
| `/api/v1/resultados/fila` | PR #199 |

Rotas dos PRs ainda não integrados ficam inertes na main até chegarem. Toda resposta dessas rotas declara `Vary: Accept-Encoding`, inclusive abaixo do mínimo, somado ao `Vary: Authorization, Cookie` privado já existente. `Cache-Control: private, no-store` continua igual.

Nunca comprimidas: `/auth/*` (sessão, refresh, logout, callback, start, JWKS), `/api/v1/telegram/*` (código de vínculo), `/api/v1/coleta/*` (código de pareamento, token de instalação, sessões), exportações XLSX/CSV, fotos e qualquer `POST`/`PUT`/`PATCH`/`DELETE`/`HEAD`, inclusive nas rotas da lista. A lista é exata: detalhe (`/api/v1/apostas/{chave}`), barra final e outra caixa de letras não entram. Rota nova só entra depois da análise abaixo, nunca por prefixo.

Conteúdo, campos, ordem e valores são os mesmos: o JSON descomprimido é byte a byte o que a rota gerou. Logs, métricas, `request_id` e o tratamento de erros rodam por dentro da compressão e continuam vendo o corpo original.

## Análise de risco BREACH

O BREACH recupera um segredo de um corpo comprimido quando, na mesma resposta, há: (1) um segredo de valor fixo, como CSRF, token ou código; (2) texto escolhido pelo atacante e refletido a cada pedido; (3) um observador que mede o tamanho cifrado enquanto induz muitos pedidos da vítima.

- **Sem segredo no corpo.** Nenhuma rota da lista devolve token, cookie, `csrf_token`, senha, código temporário ou credencial. O teste de contrato percorre o schema completo de cada rota publicada e recusa nome de campo de segredo. O `csrf_token` sai só em `/auth/session` e `/auth/refresh`, que nunca são comprimidos.
- **Sem reflexo de texto livre.** Os parâmetros dessas leituras são tipados: IDs, datas, enums, booleanos, inteiros e cursores HMAC opacos. Cursor marca posição, não autentica e não abre nada sem a sessão. O eco `filtros` das rotas filtradas contém só esses valores já validados; nenhuma string arbitrária do pedido volta no corpo.
- **Conteúdo de terceiros.** Textos de tipsters capturados do Telegram (`evento`, `descricao`) aparecem nesses corpos como dado armazenado, não como reflexo por pedido, e nunca junto de um segredo de autenticação.
- **Indução cruzada.** O cookie de sessão é `SameSite=Lax`: pedidos de subrecurso entre sites não levam a sessão, e Bearer não é anexado por outro site. Isso reduz a indução, mas a garantia não depende dela: a garantia vem de não haver segredo no corpo comprimido.

O risco residual é a inferência estatística do tamanho de dados financeiros privados por quem já observa o tráfego cifrado. Ele é o mesmo que o tamanho não comprimido já expõe, e por isso não justifica bloquear a compressão.

## Borda e ensaio

- **Produção/staging (Caddy, `deploy/lightsail/Caddyfile`).** A borda não tem `encode`: antes ela comprimiria todas as respostas JSON, inclusive `/auth/session`. O `reverse_proxy` repassa `Accept-Encoding` à API e devolve `Content-Encoding`/`Vary` e os bytes sem recomprimir. O teste de aceite roda o Caddy real (workflow *Phase 1 operational acceptance*). Um upstream que imita a allowlist comprova o repasse de gzip, a ausência de compressão sem `Accept-Encoding` e a ausência de compressão em `/auth/session`, mesmo com o cliente pedindo `gzip`/`zstd`.
- **Frontend estático (nginx do frontend).** Comprime só HTML/CSS/JS/SVG e não encaminha `/api/*`.
- **Ensaio do frontend.** O proxy `PublicBuild` (frontend, `tests/identity/test_browser_contract.py`) repassa o `Accept-Encoding` do navegador e os bytes crus com `Content-Encoding`, sem descomprimir (frontend `da0329c`, PR #105). O harness de identidade desta API (`tests/identity/journey.py`) só encaminha o emissor Keycloak e não fica no caminho dos dados da API.

## Provas

- `tests/unit/test_read_compression.py`: cada rota da lista comprime e descomprime para os mesmos bytes; sem `gzip` no `Accept-Encoding`, abaixo do mínimo, rotas de identidade/código/credencial/export/foto, mutations e caminhos vizinhos ficam intactos; corpo já codificado passa sem mudança.
- `tests/contract/test_read_compression_contract.py`: as rotas publicadas da lista respondem só `application/json` e não têm campo de segredo.
- `tests/integration/test_read_compression_db.py`: app real, JWT e PostgreSQL. Análises, painel e apostas comprimem com o mesmo JSON; `/auth/session`, `/auth/refresh`, código do Telegram, código de pareamento da Coleta, instalações, export e mutation não comprimem.
- `tests/integration/test_lightsail_acceptance.py`: Caddy real repassando a codificação da API.
