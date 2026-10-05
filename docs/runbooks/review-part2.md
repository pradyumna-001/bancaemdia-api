# Parte 2 — billing, contas e titulares

Entrega para `main` reunindo os PRs #154, #134, #135 e #136; preserva os IDs publicados das migrations. O histórico dos PRs originais permanece disponível para revisão.

A conta padrão é escolhida pela data do jogo (`data_jogo`, ou `comeca_em` da leitura). Não se substitui esse instante pelo horário de colocação, postagem, captura ou processamento. Sem data do jogo, a atribuição automática fica pendente. Uma conta explicitamente informada no multicontas conserva a identidade real depois da validação de usuário e casa, mesmo quando outra conta era a padrão no dia do jogo. A prévia de troca de titular exclui essas referências explícitas e não altera apostas antigas sem revisão.

A criação manual e a planilha aceitam `data_jogo`. As correções e a materialização usam a mesma regra. A data de colocação continua disponível como informação da aposta, sem escolher a conta padrão.

O guard de billing valida uma lista explícita de tabelas e suas colunas de tenant; uma tabela protegida incompatível impede a instalação. Colunas nullable existentes são compatíveis, mas uma escrita sem tenant não libera acesso. A API consulta o caminho efetivo sob `root_path`; billing, revogação de vínculo Telegram e exclusão dos próprios dados são operações de controle acessíveis em modo somente leitura. A exclusão usa um marcador local à transação vinculado ao tenant autenticado; a exceção permite somente DELETE ou remoção dos campos pessoais do evento, conservando os demais campos. Não libera INSERT nem edição financeira. O acesso depende de trial confirmado ou período pago vigente; `PAST_DUE` não recebe uma carência implícita.

Migrations: `h2review2026` converge titulares com `g93tenant2026`. A revisão corretiva conserva os guards mais fortes durante downgrade, até a revisão dona dos objetos removê-los. Reinstalar um módulo opcional invoca `billing_install_write_guards()`.

O aceite de workers Telegram com expiração entre enfileiramento e execução pertence à parte 3 e será executado na composição dos dois HEADs. Não confundir essa dependência de revisão/integração com ativação de lançamento.

Ativação de lançamento: cadastrar configuração e segredos do provedor de billing e ativar rollout somente na janela administrada. Esta entrega não provisiona serviços, usa credenciais reais, faz merge ou fecha issues.
