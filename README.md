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

## Contrato com o projeto consumidor

Os modulos abaixo dependem de `src.config.settings`, que **continua vivendo em cada projeto**
(nao faz parte deste pacote, de proposito — sao caminhos e configuracao especificos de cada
deploy):

- `rw_core.database.connection` — `DB_PATH`, `ensure_directories`
- `rw_core.database.maintenance` — `BACKUPS_DIR`, `DATA_DIR`, `DB_PATH`, `EXPORTS_DIR`
- `rw_core.importers.import_service` — `UPLOADS_DIR`, `load_config`
- `rw_core.vale_pedagio.service` — `UPLOADS_DIR`, `load_config`

Qualquer projeto que use estes modulos precisa manter um `src/config/settings.py` local que
exponha esses nomes.

## Terceiro lote

Extraidos tambem `src/admin`, `src/analyses` (exceto `orchestrator.py` e `tripla_ipp.py`),
`src/inconsistencies`, `src/ipiranga/validacao_tripla.py`, `src/modules` (a maior parte —
faturamento, ipiranga, backup, migration, repository), `src/operational` (o restante),
`src/security` (so o `__init__.py` — `auth.py` continua local, e diverge de proposito) e
`src/shared`.

**Ficaram de fora, de proposito, e continuam locais em cada projeto:**
- `src/modules/faturamento/coupa/page.py` e `saldo.py` — ja divergiam entre os dois projetos.
- `src/modules/faturamento/validacao_tripla_ipp/page.py`, `repository.py` e `service.py` —
  dependem de `coupa.saldo`, que diverge; por tabela, tambem `src/analyses/orchestrator.py`
  e `src/analyses/tripla_ipp.py`, que dependem desse `repository.py`.
- `src/config/settings.py`, `src/security/auth.py`, `src/database/backup.py`,
  `automatic_backup.py`, `local_zip_restore.py`, `src/ipiranga/service.py` — o que ja
  divergia desde o primeiro relatorio.

Se um dia o Coupa parar de divergir entre os dois projetos, esse cluster de 5 arquivos pode
ser reavaliado para extracao.
