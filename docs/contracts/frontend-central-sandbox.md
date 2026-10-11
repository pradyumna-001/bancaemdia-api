# Sandbox único para a jornada frontend #23

Autorizado pelo titular em 11/10/2026 para trabalhar antes dos merges administrativos.

Esta árvore tem como único pai a main b916f54331f14cf47a3800324bd61d8638043c06. Reaplica código real das candidatas #189/22d5b77f946c58dda4f0eeb64db201ee5c63c009 (inclui #184), #187/186be7482437eb5dd4b70914843dc6c5f90395f1 e #193/71f1a1acba17f2b99e8dcd55c25c672c98f43967. Nenhum merge administrativo, publicação ou nova funcionalidade de domínio.

Reconciliação própria: lista usa seleção completa de filtros e snapshot REPEATABLE READ com projeção em lote de eventos/contexto; mantém headers privados. Remove duplicação das mesmas definições SQL de materialização. Credencial de coleta mantém o retorno atômico revisado da #193. CI backend conserva os gates de filtros/Painel e foto juntos. OpenAPI/documentação são regenerados pelo app real desta árvore; 100 operações. Nenhum schema composto manualmente.

A branch codex/e2e-central-reconciliation é apenas fonte imutável do sandbox de testes frontend #23. Não representa integração, deploy, PR backend entregue ou autorização para substituir as PRs originais. O administrador continua responsável por suas revisões e merges. O frontend registra SHA, hash e proveniência, bloqueia publicação e reconcilia o pin com a main após integração.

A CI frontend testa os gates obrigatórios das quatro capacidades contra esta fonte, emissor/PostgreSQL/restrições reais e SPA pública. O sucesso dessa prova não substitui a CI completa nem a reconciliação administrativa do backend.
