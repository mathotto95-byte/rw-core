from __future__ import annotations

import pandas as pd

from rw_core.modules.ipiranga.repository import source_counts


def dashboard_summary() -> pd.DataFrame:
    return pd.DataFrame(source_counts())

