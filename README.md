# rw-core

Biblioteca Python compartilhada, extraida do codigo que era duplicado, byte a byte, entre os
repositorios `Controle-integrado` e `Painel-Ipiranga`.

## Conteudo

- `rw_core.dashboards.components` - componentes de exibicao de tabelas no Streamlit.
- `rw_core.normalizers.fields` - normalizacao de texto, datas e colunas.
- `rw_core.reports.exporter` - exportacao de DataFrames para Excel.

Nenhum destes modulos contem configuracao, credenciais ou regra de negocio especifica de um
cliente. Codigo especifico (backup, autenticacao, paginas do Ipiranga, etc.) continua em cada
repositorio, separado.

## Instalacao

Em `requirements.txt` de um projeto consumidor:

```
rw-core @ git+https://github.com/mathotto95-byte/rw-core.git@main
```

## Fonte da verdade

O `Controle-integrado` e a origem deste codigo. Alteracoes no comportamento compartilhado devem
ser feitas aqui, num Pull Request, e entao propagadas aos projetos que dependem deste pacote
(atualizando a referencia em `requirements.txt`, se for fixar uma tag/commit especifico).

## Versionamento

Por padrao, os projetos apontam para a branch `main` (sempre a versao mais recente). Se um
projeto precisar travar numa versao especifica, use `@<commit-sha>` no lugar de `@main`.
