# Releitura administrativa

`reprocessar_usuario usuario --usuario-id N` usa apenas a conexão normal da aplicação e lê somente o usuário informado.

`reprocessar_usuario reler-todas` precisa enumerar os IDs de usuários. Execute essa operação em um processo administrativo isolado, com `REPROCESS_ADMIN_DATABASE_URL` apontando para uma credencial com `BYPASSRLS`. Não coloque essa variável no ambiente da API ou dos workers. A conexão administrativa só lista IDs ativos; toda leitura de apostas e mídias continua usando a identidade de cada usuário na conexão normal.

Faça primeiro o dry run, confira o custo informado e use `--sim` apenas depois. Sem a variável administrativa, `reler-todas` recusa a execução em vez de anunciar falsamente que não há usuários.
