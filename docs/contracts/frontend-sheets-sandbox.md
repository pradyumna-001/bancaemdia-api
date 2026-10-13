# Fonte única de ensaio frontend #26

Estende cf20de6862b1f155fbff19043f89f5f6db4c5cd2 (#23/#25) com código próprio da #197/2140bdabb21835e3c66487c4fe532b09fadebe0b: XLSX, identidade, prévia, remapeamento, confirmação, HTTP/CORS e parser hardened. Infraestrutura compartilhada já presente é preservada, sem sobrescrever filtros/foto/prints ou promover Hipotética/USDT. Migração de planilha segue p194prints2026 numa única cadeia, mantendo 73 tabelas e 17 identidades sem BIGSERIAL.

OpenAPI inteiro gerado pelo aplicativo: 115 operações, 42 request bodies; hash c28137d18b64db1a7d3663159f15ee3d6a456abf9d6b8c25776b9f68050301de. Não une schemas manualmente. Nenhum merge administrativo, PR backend nova ou deploy. O pin frontend continua sandbox_reconciliation, com publicação bloqueada até integração administrativa e revalidação.

Ruff/format e mypy strict do aplicativo passaram. 142 testes locais de migrações/modelos/RLS/contrato passaram; 14 gerações negativas historicamente inaplicáveis permanecem. A CI frontend exige os 51 casos reais da #197 sem skips e preserva os 174 invariantes e 18 jornadas anteriores, no mesmo checkout/API/banco.