from __future__ import annotations

def format_money(value: float | int | str | None) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0
    formatted = f"{number:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {formatted}"


def format_percent(value: float | int | str | None) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0
    return f"{number:.2%}".replace(".", ",")

__all__ = ["format_money", "format_percent"]
