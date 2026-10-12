# Sandbox único frontend #23 e #25

Esta árvore estende a fonte f109655afa4721b239afc1c2c7593c5391cc047a da #23 com código da PR backend #195/6db8964d11c471b47d15c08bd5d46d864623bb1e, por diferença real contra sua fonte herdada #193/71f1a1a. Mantém filtros/Painel/projeção/foto das #184/#189/#187/#193. Não há merge administrativo, união de schemas, deploy ou PR backend nova.

Reconciliação: prints migram após s183filter2026 numa cadeia única; inventários mantêm as 70 tabelas e 14 identidades que não usam BIGSERIAL; documentação de 105 operações e 38 request bodies corresponde ao app inteiro. OpenAPI e docs/API.md são gerados pelo aplicativo. Hash SHA-256 do OpenAPI: 310e0612597074367e48800de71e249defaf5624eca5b4c4d6f14341baac0281.

Fonte de ensaio imutável na branch codex/prints-frontend-reconciliation. Não promove nenhum candidato a main; as PRs originais e os merges permanecem com o administrador. Publicação frontend continua bloqueada até pin integrado e revalidação. A CI frontend preserva os 139 invariantes anteriores e acrescenta as 35 provas obrigatórias de prints/worker real da #195, além da jornada da SPA.

Local: ruff/format, mypy strict dos 260 arquivos de src, 173 testes direcionados de modelos/migrações/repositórios/contrato passaram após revalidar os inventários, sem reduzir verificações. Os casos de contrato gerados inaplicáveis permanecem os existentes; as provas de prints/foto/central exigem zero skips em banco real na CI.