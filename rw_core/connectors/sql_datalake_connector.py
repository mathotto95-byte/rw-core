from __future__ import annotations

import pandas as pd


def carregar_base(nome_base: str, origem: str = "sql_datalake", **_kwargs) -> pd.DataFrame:
    """Conector reservado para integracao futura com SQL/datalake."""
    raise NotImplementedError(
        f"A origem {origem} para a base {nome_base} ainda nao esta ativa. "
        "Use a importacao manual por Excel nesta etapa."
    )

