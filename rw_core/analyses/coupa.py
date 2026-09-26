from __future__ import annotations

from datetime import datetime
from rw_core.utils.timezone import brasilia_now, brasilia_now_iso

from src.config.settings import load_config
from rw_core.inconsistencies.service import upsert_inconsistency
from rw_core.normalizers.fields import (
    normalize_location_key,
    normalize_text,
    normalizar_produto_kmm_coupa,
    normalizar_texto_match,
    parse_number,
    to_datetime,
)
from rw_core.operational.kmm_faturamento import carregar_base_kmm_faturamento
from rw_core.utils.cobranca_ipiranga import (
    cliente_equivalente_from_row,
    cobranca_from_row,
    is_ipiranga_cobranca_row,
    normalizar_texto_empresa,
)


def _value(row, field: str, default=None):
    try:
        value = row[field]
    except (IndexError, KeyError, TypeError):
        return default
    return default if value is None else value


def _number(value) -> float | None:
    return parse_number(value)


def _contains_ipiranga(value) -> bool:
    return "IPIRANGA" in normalize_text(value)


def _product(row) -> str:
    return _value(row, "produto_original", "") or _value(row, "produto", "") or _value(row, "mercadoria", "") or ""


def _product_key(value) -> str:
    return normalizar_produto_kmm_coupa(value)


def _text_key(value) -> str:
    return normalizar_texto_match(value)


def _origin_key(row) -> str:
    value = _value(row, "origem_norm", "") or _value(row, "origem_normalizada", "") or _value(row, "origem", "")
    key = _text_key(value)
    return "TODOS" if key in {"T TODOS", "TODOS", "T TODO"} else key


def _destination_key(row) -> str:
    return _text_key(_value(row, "destino_norm", "") or _value(row, "destino_normalizada", "") or _value(row, "destino", ""))


def _product_match_key(row) -> str:
    return _value(row, "produto_norm", "") or _value(row, "produto_normalizado", "") or _product_key(_product(row))


def _route_product_key(row) -> tuple[str, str, str]:
    return (
        _origin_key(row),
        _destination_key(row),
        _product_match_key(row),
    )


def _volume_for_coupa(value) -> float | None:
    volume = _number(value)
    if volume is None:
        return None
    return volume * 1000 if 0 < volume < 1000 else volume


def _fat_volume(row) -> float | None:
    return _number(_value(row, "volume_normalizado", "")) or _volume_for_coupa(_value(row, "volume"))


def _emission_date(row) -> str:
    return _value(row, "data_emissao_dt", "") or _value(row, "emissao_cte", "")


def _realized_value(row) -> float | None:
    for field in [
        "peso_frete",
        "total_conhecimento",
        "valor_total_cte_kmm",
        "valor_total_cte",
        "valor_faturado_kmm",
        "valor_kmm",
        "valor",
    ]:
        value = _number(_value(row, field, ""))
        if value is not None:
            return value
    return None


def _response_norm(row) -> str:
    return normalize_text(_value(row, "resposta_transportador_norm", "") or _value(row, "resposta_transportador", ""))


def _coupa_response_status(row) -> str:
    response = _response_norm(row)
    if response == "NOK":
        return "COUPA COM RESPOSTA NOK"
    if not response:
        return "COUPA SEM RESPOSTA TRANSPORTADOR"
    return ""


def _origin_is_all(row) -> bool:
    return (_value(row, "origem_coupa_tipo", "") or "").upper() == "TODOS" or _origin_key(row) == "TODOS"


def _operation_for(row) -> str:
    operation_text = _text_key(_value(row, "operacao_norm", "") or _value(row, "tabela_frete", "") or _value(row, "tipo_operacao", ""))
    if "COLETA" in operation_text:
        return "COLETA"
    if "TRANSFERENCIA" in operation_text or "TRANSF" in operation_text:
        return "TRANSFERENCIA"
    cobranca = _contains_ipiranga(_value(row, "cobranca", ""))
    origem = _contains_ipiranga(_value(row, "origem", ""))
    destino = _contains_ipiranga(_value(row, "destino", ""))
    if cobranca and origem and destino:
        return "TRANSFERENCIA"
    if cobranca and destino:
        return "COLETA"
    return ""


def _date_in_period(value, start, end) -> bool:
    date = to_datetime(value)
    start_date = to_datetime(start)
    end_date = to_datetime(end)
    if not start_date and not end_date:
        return True
    if not date:
        return False
    if start_date and date < start_date:
        return False
    if end_date and date > end_date:
        return False
    return True


def _period_status(value, start, end) -> str:
    date = to_datetime(value)
    start_date = to_datetime(start)
    end_date = to_datetime(end)
    if not start_date or not end_date:
        return "INVALIDO"
    if end_date < start_date:
        return "INVALIDO"
    if not date:
        return "FORA"
    if start_date <= date <= end_date:
        return "DENTRO"
    return "FORA"


def _candidate_period_status(candidate, emission) -> str:
    return _period_status(
        emission,
        _value(candidate, "dt_inicio_dt", "") or _value(candidate, "dt_inicio", ""),
        _value(candidate, "dt_termino_dt", "") or _value(candidate, "dt_termino", ""),
    )


def _select_period_candidate(candidates: list, emission, match_layer: str, outside_layer: str, use_latest_available: bool = False):
    ordered = _sort_coupa_candidates(candidates)
    invalid_candidate = None
    outside_candidate = None
    latest_prior = None
    latest_prior_end = None
    emission_date = to_datetime(emission)
    for candidate in ordered:
        status = _candidate_period_status(candidate, emission)
        if status == "DENTRO":
            return candidate, "", match_layer, ""
        if status == "INVALIDO" and invalid_candidate is None:
            invalid_candidate = candidate
        elif status == "FORA" and outside_candidate is None:
            outside_candidate = candidate
        end_date = to_datetime(_value(candidate, "dt_termino_dt", "") or _value(candidate, "dt_termino", ""))
        if use_latest_available and emission_date and end_date and end_date < emission_date:
            if latest_prior_end is None or end_date > latest_prior_end:
                latest_prior = candidate
                latest_prior_end = end_date
    if invalid_candidate is not None:
        return invalid_candidate, "PERIODO COUPA INVALIDO", "PERIODO COUPA INVALIDO", "Dt Inicio/Dt Termino da Coupa nao foram convertidas corretamente."
    if latest_prior is not None:
        return latest_prior, "CALCULADO COM ULTIMA TARIFA COUPA DISPONIVEL", outside_layer, "Rota/produto encontrados fora do periodo; usada ultima tarifa Coupa anterior disponivel para diagnostico."
    if outside_candidate is not None:
        return outside_candidate, outside_layer, outside_layer, "Rota/produto encontrados, mas fora do periodo Coupa."
    return None, "", "", ""


def _tariff_for_volume(coupa_row, volume: float | None) -> tuple[float | None, str, str]:
    volume = _volume_for_coupa(volume)
    if volume is None or volume <= 0:
        return None, "SEM VOLUME", "SEM VOLUME"
    if not coupa_row:
        return None, "", "SEM MATCH COUPA"
    if volume <= 50000:
        return _number(_value(coupa_row, "valor_bitrem")), "BITREM", ""
    if volume <= 63000:
        return _number(_value(coupa_row, "valor_rodotrem")), "RODOTREM", ""
    return None, "VOLUME FORA DA FAIXA COUPA", "VOLUME FORA DA FAIXA COUPA"


def _find_km(conn, row, operation: str):
    origem = normalize_location_key(_value(row, "origem", ""))
    destino = normalize_location_key(_value(row, "destino", ""))
    return conn.execute(
        """
        select * from base_km_origem_destino
        where origem_normalizada = ?
          and destino_normalizada = ?
          and coalesce(status, 'ATIVO') = 'ATIVO'
          and coalesce(status_registro, 'ATIVO') in ('ATIVO', 'NOVO REGISTRO', 'KM ATUALIZADO')
        order by
          case when coalesce(tipo_operacao, '') = ? then 0 else 1 end,
          id desc
        limit 1
        """,
        (origem, destino, operation),
    ).fetchone()


def _load_origin_mappings(conn) -> dict[str, set[str]]:
    try:
        rows = conn.execute(
            """
            select origem_coupa_norm, origem_kmm_norm
            from de_para_origem_coupa_kmm
            where coalesce(ativo, 'SIM') = 'SIM'
            """
        ).fetchall()
    except Exception:
        return {}
    mapping: dict[str, set[str]] = {}
    for row in rows:
        coupa = _text_key(_value(row, "origem_coupa_norm", ""))
        kmm = _text_key(_value(row, "origem_kmm_norm", ""))
        if coupa and kmm:
            mapping.setdefault(coupa, set()).add(kmm)
    return mapping


def _load_depara_mappings(conn) -> dict[str, dict[str, set[str]]]:
    mappings: dict[str, dict[str, set[str]]] = {"ORIGEM": {}, "DESTINO": {}, "PRODUTO": {}, "OPERACAO": {}}
    try:
        rows = conn.execute(
            """
            select campo, valor_coupa_norm, valor_fat_norm
            from de_para_coupa_fat
            where coalesce(ativo, 'SIM') = 'SIM'
            """
        ).fetchall()
    except Exception:
        return mappings
    for row in rows:
        field = _text_key(_value(row, "campo", "")).replace(" ", "_")
        if field == "OPERAÇÃO":
            field = "OPERACAO"
        if field not in mappings:
            continue
        coupa = _text_key(_value(row, "valor_coupa_norm", ""))
        fat = _text_key(_value(row, "valor_fat_norm", ""))
        if field == "PRODUTO":
            coupa = _product_key(coupa)
            fat = _product_key(fat)
        if coupa and fat:
            mappings[field].setdefault(coupa, set()).add(fat)
    try:
        product_rows = conn.execute(
            """
            select produto_norm, produto_mapeado
            from de_para_produto_coupa_kmm
            where coalesce(ativo, 'SIM') = 'SIM'
            """
        ).fetchall()
    except Exception:
        product_rows = []
    for row in product_rows:
        original = _product_key(_value(row, "produto_norm", ""))
        mapped = _product_key(_value(row, "produto_mapeado", ""))
        if original and mapped:
            mappings["PRODUTO"].setdefault(mapped, set()).add(original)
    return mappings


def _sort_coupa_candidates(candidates: list) -> list:
    return sorted(candidates, key=lambda item: 0 if _response_norm(item) == "OK" else 1)


def _period_delta_days(value, start, end) -> int:
    date = to_datetime(value)
    start_date = to_datetime(start)
    end_date = to_datetime(end)
    if not date:
        return 0
    if start_date and date < start_date:
        return (start_date - date).days
    if end_date and date > end_date:
        return (date - end_date).days
    return 0


def _matches_with_depara(field: str, fat_value: str, coupa_value: str, mappings: dict[str, dict[str, set[str]]]) -> bool:
    if fat_value == coupa_value:
        return True
    return fat_value in mappings.get(field, {}).get(coupa_value, set())


def _find_coupa_match(coupa_rows, origin_mappings: dict[str, set[str]], depara_mappings: dict[str, dict[str, set[str]]], row, options: dict | None = None):
    options = options or {}
    emission = _emission_date(row)
    origin = _origin_key(row)
    destination = _destination_key(row)
    product = _product_match_key(row)
    if not to_datetime(emission):
        return None, "DATA FAT INVALIDA", "DATA FAT INVALIDA", "Data de emissao da FAT/KMM nao foi convertida corretamente."
    if not origin or not destination or not product:
        return None, "DADOS INSUFICIENTES", "DADOS INSUFICIENTES", "Origem, destino, produto ou data ausente na FAT/KMM."

    def same_destination_product(candidate) -> bool:
        return (
            _destination_key(candidate) == destination
            and _product_match_key(candidate) == product
        )

    exact = [
        candidate for candidate in coupa_rows
        if same_destination_product(candidate)
        and _origin_key(candidate) == origin
    ]
    selected = _select_period_candidate(exact, emission, "MATCH EXATO", "MATCH FORA DO PERIODO COUPA", bool(options.get("use_latest_available_tariff")))
    if selected[0]:
        return selected

    all_origin = [candidate for candidate in coupa_rows if same_destination_product(candidate) and _origin_is_all(candidate)]
    selected = _select_period_candidate(all_origin, emission, "MATCH ORIGEM TODOS", "MATCH ORIGEM TODOS FORA DO PERIODO", bool(options.get("use_latest_available_tariff")))
    if selected[0]:
        return selected

    mapped = [
        candidate for candidate in coupa_rows
        if _matches_with_depara("DESTINO", destination, _destination_key(candidate), depara_mappings)
        and _matches_with_depara("PRODUTO", product, _product_match_key(candidate), depara_mappings)
        and (
            origin in origin_mappings.get(_origin_key(candidate), set())
            or _matches_with_depara("ORIGEM", origin, _origin_key(candidate), depara_mappings)
            or _origin_is_all(candidate)
        )
    ]
    selected = _select_period_candidate(mapped, emission, "MATCH DE/PARA", "MATCH DE/PARA FORA DO PERIODO", bool(options.get("use_latest_available_tariff")))
    if selected[0]:
        return selected

    if options.get("allow_reverse_route_match"):
        reversed_routes = [
            candidate for candidate in coupa_rows
            if _origin_key(candidate) == destination
            and _destination_key(candidate) == origin
            and _product_match_key(candidate) == product
        ]
        selected = _select_period_candidate(reversed_routes, emission, "MATCH ROTA INVERTIDA", "MATCH ROTA INVERTIDA FORA DO PERIODO", bool(options.get("use_latest_available_tariff")))
    if selected[0]:
        return selected

    destination_matches = [candidate for candidate in coupa_rows if _destination_key(candidate) == destination]
    if destination_matches and not any(_product_match_key(candidate) == product for candidate in destination_matches):
        return None, "PRODUTO NAO MAPEADO", "SEM MATCH COUPA", "Destino encontrado na Coupa, mas produto nao bate apos normalizacao."
    if any(_product_match_key(candidate) == product for candidate in coupa_rows):
        return None, "SEM MATCH COUPA", "SEM MATCH COUPA", "Produto existe na Coupa, mas rota/origem/destino nao foram encontrados. Verificar se existe essa rota na Coupa, se a rota esta invertida, cadastrar de/para origem/destino ou verificar se origem Coupa esta como fornecedor e nao como cidade."
    return None, "PRODUTO NAO MAPEADO", "SEM MATCH COUPA", "Produto KMM nao mapeado na Coupa."


def _insert_kmm_coupa_diag(
    conn,
    row,
    coupa_row,
    status: str,
    stage: str,
    camada: str,
    reason: str,
    username: str,
    processed_at: str,
    base_faturamento: str = "",
    status_periodo: str = "",
    km_origem: str = "",
) -> None:
    delta = _period_delta_days(
        _emission_date(row),
        _value(coupa_row, "dt_inicio_dt", "") or _value(coupa_row, "dt_inicio", "") if coupa_row else "",
        _value(coupa_row, "dt_termino_dt", "") or _value(coupa_row, "dt_termino", "") if coupa_row else "",
    )
    conn.execute(
        """
        insert or replace into diagnostico_kmm_x_coupa (
            numero_conhecimento, nota_fiscal, cliente, origem, destino, produto,
            volume, data_emissao, motivo_nao_gerado, etapa_falha,
            data_processamento, usuario_processamento, origem_kmm, destino_kmm,
            produto_kmm, produto_normalizado, sugestao, camada_match,
            resposta_transportador, origem_coupa, destino_coupa, produto_coupa,
            dt_inicio_coupa, dt_termino_coupa, diferenca_periodo_dias,
            motivo_nao_match, sugestao_correcao, base_faturamento,
            status_periodo_coupa, data_emissao_kmm, km_origem, signature
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _value(row, "cte_numero"),
            _value(row, "nota_fiscal"),
            _value(row, "cobranca") or _value(row, "cliente"),
            _value(row, "origem"),
            _value(row, "destino"),
            _product(row),
            _number(_value(row, "volume")),
            _emission_date(row),
            status,
            stage,
            processed_at,
            username,
            _value(row, "origem"),
            _value(row, "destino"),
            _product(row),
            _product_key(_product(row)),
            reason or "Cadastrar de/para de origem ou revisar destino/produto/periodo.",
            camada,
            _value(coupa_row, "resposta_transportador") if coupa_row else "",
            _value(coupa_row, "origem") if coupa_row else "",
            _value(coupa_row, "destino") if coupa_row else "",
            _product(coupa_row) if coupa_row else "",
            _value(coupa_row, "dt_inicio_dt", "") or _value(coupa_row, "dt_inicio", "") if coupa_row else "",
            _value(coupa_row, "dt_termino_dt", "") or _value(coupa_row, "dt_termino", "") if coupa_row else "",
            delta,
            status,
            reason or "Verificar se existe essa rota na Coupa, se a rota esta invertida, cadastrar de/para origem/destino ou verificar se origem Coupa esta como fornecedor e nao como cidade.",
            base_faturamento,
            status_periodo or _period_label(status, camada),
            _emission_date(row),
            km_origem,
            f"diag_kmm_coupa:{base_faturamento}:{_value(row, 'id')}:{status}:{stage}",
        ),
    )


OUTSIDE_PERIOD_STATUSES = {
    "MATCH FORA DO PERIODO COUPA",
    "MATCH ORIGEM TODOS FORA DO PERIODO",
    "MATCH DE/PARA FORA DO PERIODO",
    "MATCH ROTA INVERTIDA FORA DO PERIODO",
    "CALCULADO COM ULTIMA TARIFA COUPA DISPONIVEL",
}


def _period_label(match_status: str, camada_match: str) -> str:
    if match_status in {"SEM MATCH COUPA", "PRODUTO NAO MAPEADO", "DADOS INSUFICIENTES", "DATA FAT INVALIDA"} or not camada_match:
        return "NAO APLICAVEL"
    if match_status == "PERIODO COUPA INVALIDO" or camada_match == "PERIODO COUPA INVALIDO":
        return "PERIODO COUPA INVALIDO"
    if match_status in OUTSIDE_PERIOD_STATUSES or camada_match in OUTSIDE_PERIOD_STATUSES:
        return "FORA DO PERIODO"
    return "DENTRO DO PERIODO"


def _status_calculo_label(status: str, outside_period: bool, calculated: float | None) -> str:
    if calculated not in (None, 0):
        if status == "CALCULADO COM ULTIMA TARIFA COUPA DISPONIVEL":
            return "CALCULADO COM ULTIMA TARIFA COUPA DISPONIVEL"
        return "CALCULADO FORA DO PERIODO" if outside_period else "CALCULADO"
    if status == "SEM TARIFA COUPA":
        return "SEM TARIFA"
    if status == "SEM KM CADASTRADO":
        return "SEM KM"
    if status == "VOLUME FORA DA FAIXA COUPA":
        return "VOLUME FORA DA FAIXA"
    if status == "PRODUTO SEM REGRA DE CALCULO":
        return "PRODUTO SEM REGRA"
    return "NAO CALCULADO"


def _calculation_empty_reason(status_calculo: str, tariff: float | None, product: str, volume: float | None, km: float | None) -> str:
    if status_calculo.startswith("CALCULADO"):
        return ""
    if tariff in (None, 0):
        return "Tarifa Coupa vazia ou zerada."
    if product == "ETANOL" and km in (None, 0):
        return "Produto ETANOL exige KM; nao encontrado na base KM nem na distancia Coupa."
    if product in {"BIODIESEL", "DERIVADOS"} and volume in (None, 0):
        return "Produto exige volume normalizado; volume vazio ou zerado."
    if status_calculo == "VOLUME FORA DA FAIXA":
        return "Volume fora da faixa de tarifa Coupa."
    if status_calculo == "PRODUTO SEM REGRA":
        return "Produto normalizado sem regra de calculo."
    return status_calculo or "Calculo nao executado."


def _status_final(status: str, status_calculo: str, status_comparacao: str, outside_period: bool) -> str:
    if status in {"SEM MATCH COUPA", "DADOS INSUFICIENTES", "DATA FAT INVALIDA"}:
        return "SEM MATCH COUPA"
    if status == "PRODUTO NAO MAPEADO":
        return "PRODUTO NAO MAPEADO"
    if status == "SEM TARIFA COUPA":
        return "SEM TARIFA COUPA"
    if status == "SEM KM CADASTRADO":
        return "SEM KM CADASTRADO"
    if status == "VOLUME FORA DA FAIXA COUPA":
        return "VOLUME FORA DA FAIXA"
    if status in {"POSSIVEL COMPLEMENTO PENDENTE", "FATURADO ACIMA DA COUPA"}:
        return status
    if status_calculo == "CALCULADO COM ULTIMA TARIFA COUPA DISPONIVEL":
        return "CALCULADO COM ULTIMA TARIFA COUPA DISPONIVEL"
    if outside_period and status_calculo == "CALCULADO FORA DO PERIODO":
        return "CALCULADO FORA DO PERIODO"
    if status_comparacao == "DIVERGENTE":
        return "DIVERGENTE"
    if status_comparacao == "OK":
        return "OK"
    return status


def _calculate_kmm_coupa(conn, row, coupa_row, operation: str, match_status: str) -> dict:
    product = _product_key(_product(row))
    volume = _fat_volume(row)
    freight_value = _realized_value(row)
    tariff, vehicle_type, tariff_status = _tariff_for_volume(coupa_row, volume)
    km = None
    km_origin = "NAO_ENCONTRADO"
    calculated = None
    unit = ""
    calculation_base = ""
    volume_used = None
    km_used = None
    outside_period = match_status in OUTSIDE_PERIOD_STATUSES
    blocking_match_status = match_status if match_status not in OUTSIDE_PERIOD_STATUSES else ""
    status = blocking_match_status or tariff_status

    if not status:
        if tariff in (None, 0):
            status = "SEM TARIFA COUPA"
        elif product in {"BIODIESEL", "DERIVADOS"}:
            volume_used = (volume or 0) / 1000
            calculated = volume_used * tariff
            unit = "R$/M3"
            calculation_base = "VOLUME_M3"
            status = "OK"
        elif product == "ETANOL":
            km_row = _find_km(conn, row, operation)
            if not km_row or not _value(km_row, "km"):
                km = _number(_value(coupa_row, "distancia", "")) if coupa_row else None
                if km:
                    km_origin = "COUPA"
                    km_used = km
                    calculated = km * tariff
                    unit = "R$/KM"
                    calculation_base = "KM"
                    status = "OK"
                else:
                    status = "SEM KM CADASTRADO"
            else:
                km = _number(_value(km_row, "km"))
                km_origin = "BASE_KM"
                km_used = km
                calculated = km * tariff
                unit = "R$/KM"
                calculation_base = "KM"
                status = "OK"
        elif not product:
            status = "SEM DADOS SUFICIENTES"
        else:
            status = "PRODUTO SEM REGRA DE CALCULO"

    diff = None
    diff_pct = None
    status_comparacao = "NAO COMPARADO"
    if calculated not in (None, 0) and freight_value is not None:
        diff = freight_value - calculated
        diff_pct = diff / calculated
        tolerance = float(load_config().get("kmm_coupa", {}).get("value_tolerance", 0.02))
        status_comparacao = "OK" if abs(diff) <= tolerance else "DIVERGENTE"
        status = status_comparacao
        if status_comparacao == "DIVERGENTE":
            status = "POSSIVEL COMPLEMENTO PENDENTE" if diff < 0 else "FATURADO ACIMA DA COUPA"

    response_status = _coupa_response_status(coupa_row) if coupa_row else ""
    if response_status and status in {"OK", "DIVERGENTE"}:
        status = response_status
    status_calculo = _status_calculo_label(match_status if match_status == "CALCULADO COM ULTIMA TARIFA COUPA DISPONIVEL" else status, outside_period, calculated)
    final_status = _status_final(status, status_calculo, status_comparacao, outside_period)
    empty_reason = _calculation_empty_reason(status_calculo, tariff, product, volume, km)

    return {
        "operation": operation,
        "tariff": tariff,
        "unidade_tarifa_coupa": unit,
        "base_calculo_coupa": calculation_base,
        "volume_usado_coupa": volume_used,
        "km_usado_coupa": km_used,
        "vehicle_type": vehicle_type,
        "km": km,
        "km_origin": km_origin,
        "calculated": calculated,
        "diff": diff,
        "diff_pct": diff_pct,
        "status": status,
        "status_calculo": status_calculo,
        "status_comparacao": status_comparacao,
        "status_final": final_status,
        "motivo_calculo_vazio": empty_reason,
    }


def _kmm_coupa_stage(status: str) -> str:
    if status == "SEM MATCH COUPA":
        return "MATCH COUPA"
    if status in {
        "FORA DO PERIODO COUPA",
        "MATCH FORA DO PERIODO COUPA",
        "MATCH ORIGEM TODOS FORA DO PERIODO",
        "MATCH DE/PARA FORA DO PERIODO",
        "MATCH ROTA INVERTIDA FORA DO PERIODO",
        "ROTA EXISTE MAS FORA DO PERIODO COUPA",
        "PERIODO COUPA INVALIDO",
        "CALCULADO FORA DO PERIODO",
        "CALCULADO COM ULTIMA TARIFA COUPA DISPONIVEL",
    }:
        return "PERIODO COUPA"
    if status == "SEM TARIFA COUPA":
        return "TARIFA COUPA"
    if status == "SEM KM CADASTRADO":
        return "KM ORIGEM DESTINO"
    if status in {"SEM VOLUME", "SEM DADOS SUFICIENTES", "DADOS INSUFICIENTES", "DATA FAT INVALIDA", "PRODUTO NAO MAPEADO", "PRODUTO SEM REGRA DE CALCULO"}:
        return "DADOS KMM"
    if status in {"COUPA COM RESPOSTA NOK", "COUPA SEM RESPOSTA TRANSPORTADOR"}:
        return "RESPOSTA TRANSPORTADOR"
    if status in {"POSSIVEL COMPLEMENTO PENDENTE", "FATURADO ACIMA DA COUPA"}:
        return "COMPARACAO VALOR"
    return "CALCULO"


def _load_fat_rows(conn) -> tuple[list, str]:
    return carregar_base_kmm_faturamento(conn)


def run_kmm_x_coupa(conn, username: str = "") -> dict:
    conn.execute("delete from analise_kmm_x_coupa")
    conn.execute("delete from diagnostico_kmm_x_coupa")
    processed_at = brasilia_now_iso()
    active: set[str] = set()
    metrics = {
        "registros_kmm_lidos": 0,
        "registros_kmm_ipiranga": 0,
        "registros_transferencia": 0,
        "registros_coleta": 0,
        "registros_coupa_lidos": 0,
        "registros_fat_lidos": 0,
        "registros_coupa_tarifa_valida": 0,
        "registros_origem_destino_normalizados": 0,
        "registros_produto_normalizado": 0,
        "registros_match": 0,
        "quantidade_match_exato": 0,
        "quantidade_match_origem_todos": 0,
        "quantidade_match_de_para": 0,
        "quantidade_match_fora_periodo": 0,
        "quantidade_calculado_fora_periodo": 0,
        "quantidade_periodo_invalido": 0,
        "quantidade_produto_nao_mapeado": 0,
        "quantidade_sem_match": 0,
        "quantidade_sem_tarifa": 0,
        "quantidade_sem_km": 0,
        "quantidade_com_tarifa": 0,
        "quantidade_calculada": 0,
        "quantidade_tarifa_sem_calculo": 0,
        "quantidade_fora_periodo": 0,
        "quantidade_descartada": 0,
        "quantidade_valor_zero": 0,
        "quantidade_gerada": 0,
    }
    coupa_rows = conn.execute(
        "select * from base_coupa_fluxos_normalizados where coalesce(status_registro, 'ATIVO') = 'ATIVO'"
    ).fetchall()
    metrics["registros_coupa_lidos"] = len(coupa_rows)
    metrics["registros_coupa_tarifa_valida"] = sum(
        1 for row in coupa_rows if (_number(_value(row, "valor_bitrem")) not in (None, 0) or _number(_value(row, "valor_rodotrem")) not in (None, 0))
    )
    origin_mappings = _load_origin_mappings(conn)
    depara_mappings = _load_depara_mappings(conn)
    kmm_coupa_config = load_config().get("kmm_coupa", {})
    match_options = {
        "use_latest_available_tariff": bool(kmm_coupa_config.get("use_latest_available_tariff", False)),
        "allow_reverse_route_match": bool(kmm_coupa_config.get("allow_reverse_route_match", False)),
    }
    kmm_rows, faturamento_table = _load_fat_rows(conn)
    metrics["registros_kmm_lidos"] = len(kmm_rows)
    metrics["registros_fat_lidos"] = len(kmm_rows)
    kmm_rows = [row for row in kmm_rows if is_ipiranga_cobranca_row(row)]
    metrics["registros_kmm_ipiranga"] = len(kmm_rows)
    metrics["quantidade_descartada"] = metrics["registros_kmm_lidos"] - len(kmm_rows)
    for row in kmm_rows:
        if normalize_location_key(_value(row, "origem", "")) and normalize_location_key(_value(row, "destino", "")):
            metrics["registros_origem_destino_normalizados"] += 1
        if _product_key(_product(row)):
            metrics["registros_produto_normalizado"] += 1
        operation = _operation_for(row)
        if operation == "TRANSFERENCIA":
            metrics["registros_transferencia"] += 1
        elif operation == "COLETA":
            metrics["registros_coleta"] += 1
        else:
            operation = "SEM CLASSIFICACAO"
        coupa_row, match_status, camada_match, match_reason = _find_coupa_match(coupa_rows, origin_mappings, depara_mappings, row, match_options)
        if coupa_row:
            metrics["registros_match"] += 1
        if camada_match == "MATCH EXATO":
            metrics["quantidade_match_exato"] += 1
        elif camada_match == "MATCH ORIGEM TODOS":
            metrics["quantidade_match_origem_todos"] += 1
        elif camada_match in {"MATCH DE/PARA", "MATCH DE/PARA ORIGEM"}:
            metrics["quantidade_match_de_para"] += 1
        elif camada_match in {"MATCH FORA DO PERIODO COUPA", "MATCH ORIGEM TODOS FORA DO PERIODO", "MATCH DE/PARA FORA DO PERIODO", "MATCH ROTA INVERTIDA FORA DO PERIODO"}:
            metrics["quantidade_match_fora_periodo"] += 1
        elif camada_match == "PERIODO COUPA INVALIDO":
            metrics["quantidade_periodo_invalido"] += 1
        result = _calculate_kmm_coupa(conn, row, coupa_row, operation, match_status)
        status = result["status_final"]
        status_periodo = _period_label(match_status, camada_match)
        status_match_coupa = match_status or ("MATCH COUPA" if coupa_row else "SEM MATCH COUPA")
        has_tariff = result["tariff"] not in (None, 0)
        has_calculation = result["calculated"] not in (None, 0)
        if has_tariff:
            metrics["quantidade_com_tarifa"] += 1
        else:
            metrics["quantidade_sem_tarifa"] += 1
        if has_calculation:
            metrics["quantidade_calculada"] += 1
        if has_tariff and not has_calculation:
            metrics["quantidade_tarifa_sem_calculo"] += 1
        if status_periodo == "FORA DO PERIODO":
            metrics["quantidade_fora_periodo"] += 1
        if result["status_calculo"] in {"CALCULADO FORA DO PERIODO", "CALCULADO COM ULTIMA TARIFA COUPA DISPONIVEL"}:
            metrics["quantidade_calculado_fora_periodo"] += 1
        if status == "SEM MATCH COUPA":
            metrics["quantidade_sem_match"] += 1
        if status in {"PRODUTO NAO MAPEADO", "PRODUTO SEM REGRA DE CALCULO"}:
            metrics["quantidade_produto_nao_mapeado"] += 1
        if status == "SEM KM CADASTRADO" or result["status_calculo"] == "SEM KM":
            metrics["quantidade_sem_km"] += 1
        product = _product(row)
        product_norm = _product_key(product)
        razao_social_cobranca = cobranca_from_row(row)
        cliente_equivalente = cliente_equivalente_from_row(row)
        signature = f"kmm_coupa:{faturamento_table}:{_value(row, 'id')}:{operation}"
        observation = (
            f"Veiculo: {result['vehicle_type']}; "
            f"Valor realizado: {_realized_value(row)}; "
            f"Tarifa Coupa: {result['tariff']}; "
            f"Volume normalizado: {_fat_volume(row)}; "
            f"KM usado: {result['km']}; "
            f"Origem KM: {result['km_origin']}; "
            f"Frete esperado: {result['calculated']}; "
            f"Diferenca: {result['diff']}; "
            f"Status periodo: {status_periodo}; "
            f"Status calculo: {result['status_calculo']}; "
            f"Camada match: {camada_match}; "
            f"Motivo: {match_reason or result['motivo_calculo_vazio']}"
        )
        conn.execute(
            """
            insert or replace into analise_kmm_x_coupa (
                cliente, placa, origem, destino, cobranca, produto, periodo,
                razao_social_cobranca, razao_social_cobranca_norm,
                cliente_equivalente, cliente_equivalente_norm,
                volume, valor, quantidade, indicador, status, observacao,
                cte, nota_fiscal, tipo_operacao, peso_frete_kmm, tarifa_coupa,
                frete_calculado, diferenca, diferenca_percentual, km,
                arquivo_origem, camada_match, motivo_nao_gerado, resposta_transportador,
                origem_coupa, destino_coupa, produto_coupa, produto_normalizado,
                dt_inicio_coupa, dt_termino_coupa, distancia_coupa,
                volume_normalizado, motivo_nao_match,
                valor_realizado, frete_esperado, diferenca_valor,
                status_comparacao, base_faturamento, status_match_coupa,
                status_periodo_coupa, status_calculo, status_final, km_origem,
                unidade_tarifa_coupa, base_calculo_coupa, volume_usado_coupa,
                km_usado_coupa, cte_normal, ctes_complementares,
                complemento_original, observacao_original, motivo_identificacao_complemento,
                valor_cte_normal, valor_cte_complementar, valor_kmm_total,
                tem_complemento, status_complemento_cte, diferenca_sem_complemento,
                diferenca_com_complemento,
                signature
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _value(row, "cliente"),
                _value(row, "placa"),
                _value(row, "origem"),
                _value(row, "destino"),
                _value(row, "cobranca"),
                product,
                (_emission_date(row) or "")[:7],
                razao_social_cobranca,
                normalizar_texto_empresa(razao_social_cobranca),
                cliente_equivalente,
                normalizar_texto_empresa(cliente_equivalente),
                _fat_volume(row),
                _realized_value(row),
                1,
                _value(row, "cte_numero"),
                status,
                observation,
                _value(row, "cte_numero"),
                _value(row, "nota_fiscal"),
                operation,
                _realized_value(row),
                result["tariff"],
                result["calculated"],
                result["diff"],
                result["diff_pct"],
                result["km"],
                _value(row, "arquivo_origem"),
                camada_match,
                match_reason or (status if status != "OK" else ""),
                _value(coupa_row, "resposta_transportador") if coupa_row else "",
                _value(coupa_row, "origem") if coupa_row else "",
                _value(coupa_row, "destino") if coupa_row else "",
                _product(coupa_row) if coupa_row else "",
                product_norm,
                _value(coupa_row, "dt_inicio_dt", "") or _value(coupa_row, "dt_inicio", "") if coupa_row else "",
                _value(coupa_row, "dt_termino_dt", "") or _value(coupa_row, "dt_termino", "") if coupa_row else "",
                _number(_value(coupa_row, "distancia", "")) if coupa_row else None,
                _fat_volume(row),
                match_reason or result["motivo_calculo_vazio"] or (status if status != "OK" else ""),
                _realized_value(row),
                result["calculated"],
                result["diff"],
                result["status_comparacao"],
                faturamento_table,
                status_match_coupa,
                status_periodo,
                result["status_calculo"],
                status,
                result["km_origin"],
                result["unidade_tarifa_coupa"],
                result["base_calculo_coupa"],
                result["volume_usado_coupa"],
                result["km_usado_coupa"],
                _value(row, "cte_normal") or _value(row, "cte_numero"),
                _value(row, "ctes_complementares"),
                _value(row, "complemento_original"),
                _value(row, "observacao_original"),
                _value(row, "motivo_identificacao_complemento"),
                _number(_value(row, "valor_cte_normal")),
                _number(_value(row, "valor_cte_complementar")),
                _realized_value(row),
                _value(row, "tem_complemento"),
                _value(row, "status_complemento_cte"),
                (_number(_value(row, "valor_cte_normal")) - result["calculated"]) if _number(_value(row, "valor_cte_normal")) is not None and result["calculated"] is not None else None,
                result["diff"],
                signature,
            ),
        )
        metrics["quantidade_gerada"] += 1
        if status != "OK":
            _insert_kmm_coupa_diag(
                conn,
                row,
                coupa_row,
                status,
                _kmm_coupa_stage(status),
                camada_match,
                match_reason,
                username,
                processed_at,
                faturamento_table,
                status_periodo,
                result["km_origin"],
            )
            active.add(
                upsert_inconsistency(
                    conn,
                    {
                        "signature": f"inc:kmm_coupa:{status}:{_value(row, 'id')}:{operation}",
                        "modulo_origem": "KMM x Coupa",
                        "tipo_inconsistencia": status,
                        "cliente": _value(row, "cliente"),
                        "placa": _value(row, "placa"),
                        "nota_fiscal": _value(row, "nota_fiscal"),
                        "cte": _value(row, "cte_numero"),
                        "origem": _value(row, "origem"),
                        "destino": _value(row, "destino"),
                        "cobranca": _value(row, "cobranca"),
                        "produto": product,
                        "periodo": operation,
                        "valor_envolvido": result["calculated"],
                        "volume_envolvido": _fat_volume(row),
                        "diferenca_valor": result["diff"],
                        "percentual_diferenca": result["diff_pct"],
                        "prioridade": "ALTA" if status == "DIVERGENTE" else "MEDIA",
                        "observacao": observation,
                        "arquivo_origem": _value(row, "arquivo_origem"),
                    },
                )
            )
    metrics["registros_gerados"] = metrics["quantidade_gerada"]
    metrics["registros_processados"] = metrics["registros_kmm_lidos"]
    metrics["active"] = active
    return metrics


def _coupa_value(row) -> tuple[float | None, str]:
    qtd = _number(_value(row, "qtd"))
    if qtd is None or qtd <= 0:
        return None, "SEM QTD COUPA"
    if qtd <= 50000:
        tariff = _number(_value(row, "valor_bitrem"))
    elif qtd <= 63000:
        tariff = _number(_value(row, "valor_rodotrem"))
    else:
        tariff = _number(_value(row, "valor_rodotrem")) or _number(_value(row, "valor_bitrem"))
    if tariff in (None, 0):
        return None, "SEM VALOR COUPA"
    return qtd * tariff, ""


def _kmm_matches_for_coupa(kmm_rows, coupa_row):
    key = _route_product_key(coupa_row)
    route_matches = [row for row in kmm_rows if _route_product_key(row) == key]
    if not route_matches:
        return [], "SEM MATCH ORIGEM DESTINO PRODUTO"
    period_matches = [
        row for row in route_matches
        if _date_in_period(_value(row, "emissao_cte", ""), _value(coupa_row, "dt_inicio", ""), _value(coupa_row, "dt_termino", ""))
    ]
    if not period_matches:
        return [], "FORA DO PERIODO"
    return period_matches, ""


def _insert_coupa_faturado_diag(
    conn,
    coupa_row,
    status: str,
    stage: str,
    reason: str,
    value_coupa,
    value_billed,
    volume_billed,
    username: str,
    processed_at: str,
) -> None:
    conn.execute(
        """
        insert or replace into diagnostico_coupa_x_faturado (
            item_coupa, cliente, origem, destino, produto, periodo_inicio,
            periodo_fim, qtd_coupa, valor_coupa, valor_faturado,
            volume_faturado, motivo_valor_zero, etapa_falha, status,
            data_processamento, usuario_processamento, signature
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _value(coupa_row, "item"),
            _value(coupa_row, "resposta_transportador"),
            _value(coupa_row, "origem"),
            _value(coupa_row, "destino"),
            _value(coupa_row, "produto"),
            _value(coupa_row, "dt_inicio"),
            _value(coupa_row, "dt_termino"),
            _number(_value(coupa_row, "qtd")),
            value_coupa,
            value_billed,
            volume_billed,
            reason,
            stage,
            status,
            processed_at,
            username,
            f"diag_coupa_fat:{_value(coupa_row, 'id')}:{status}:{stage}",
        ),
    )


def _insert_coupa_faturado_analysis(conn, row, value_coupa, value_billed, volume_billed, qtd_billed, status, observation, suffix) -> None:
    base_values = (
        _value(row, "resposta_transportador"),
        None,
        _value(row, "origem"),
        _value(row, "destino"),
        None,
        _value(row, "produto"),
        f"{_value(row, 'dt_inicio') or ''} a {_value(row, 'dt_termino') or ''}".strip(),
        volume_billed or 0,
        value_billed or 0,
        qtd_billed or 0,
    )
    signature_base = f"coupa_fat:{suffix}:{_value(row, 'id')}"
    conn.execute(
        """
        insert or replace into analise_coupa_x_faturado_volume (
            cliente, placa, origem, destino, cobranca, produto, periodo,
            volume, valor, quantidade, indicador, status, observacao, signature
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*base_values, f"Volume Coupa: {_value(row, 'qtd')}; Volume faturado: {volume_billed}", status, observation, f"{signature_base}:volume"),
    )
    conn.execute(
        """
        insert or replace into analise_coupa_x_faturado_valor (
            cliente, placa, origem, destino, cobranca, produto, periodo,
            volume, valor, quantidade, indicador, status, observacao, signature
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*base_values, f"Valor Coupa: {value_coupa}; Valor faturado: {value_billed}", status, observation, f"{signature_base}:valor"),
    )


def run_coupa_x_faturado(conn, username: str = "") -> dict:
    conn.execute("delete from analise_coupa_x_faturado_volume")
    conn.execute("delete from analise_coupa_x_faturado_valor")
    conn.execute("delete from diagnostico_coupa_x_faturado")
    processed_at = brasilia_now_iso()
    active: set[str] = set()
    metrics = {
        "registros_coupa_lidos": 0,
        "registros_kmm_lidos": 0,
        "registros_nsdocs_lidos": 0,
        "registros_match": 0,
        "registros_sem_faturamento": 0,
        "registros_com_faturamento": 0,
        "registros_valor_coupa_calculado": 0,
        "registros_valor_faturado": 0,
        "quantidade_valor_zero": 0,
        "quantidade_sem_match": 0,
        "quantidade_sem_tarifa": 0,
        "quantidade_sem_km": 0,
        "quantidade_descartada": 0,
        "quantidade_gerada": 0,
    }
    coupa_rows = conn.execute(
        "select * from base_coupa_fluxos_normalizados where coalesce(status_registro, 'ATIVO') = 'ATIVO'"
    ).fetchall()
    kmm_rows = conn.execute(
        """
        select * from base_kmm_original
        where status_complemento_normalizado = 'NORMAL'
          and coalesce(status_registro, 'ATIVO') = 'ATIVO'
        """
    ).fetchall()
    metrics["registros_coupa_lidos"] = len(coupa_rows)
    metrics["registros_kmm_lidos"] = len(kmm_rows)
    try:
        metrics["registros_nsdocs_lidos"] = int(conn.execute("select count(*) from base_nsdocs_original").fetchone()[0] or 0)
    except Exception:
        metrics["registros_nsdocs_lidos"] = 0

    matched_kmm_ids: set[int] = set()
    tolerance = float(load_config().get("kmm_coupa", {}).get("value_tolerance", 0.02))
    for row in coupa_rows:
        value_coupa, coupa_error = _coupa_value(row)
        matches, match_error = _kmm_matches_for_coupa(kmm_rows, row)
        value_billed = sum((_number(_value(item, "total_conhecimento")) or 0) for item in matches)
        volume_billed = sum((_number(_value(item, "volume")) or 0) for item in matches)
        qtd_billed = len(matches)
        for item in matches:
            matched_kmm_ids.add(int(_value(item, "id", 0) or 0))
        if value_coupa not in (None, 0):
            metrics["registros_valor_coupa_calculado"] += 1
        if value_billed not in (None, 0):
            metrics["registros_valor_faturado"] += 1
        if matches:
            metrics["registros_match"] += 1
            metrics["registros_com_faturamento"] += 1
        else:
            metrics["registros_sem_faturamento"] += 1
        reason = ""
        if coupa_error:
            status = "SEM VALOR COUPA"
            reason = coupa_error
            stage = "VALOR COUPA"
        elif match_error == "SEM MATCH ORIGEM DESTINO PRODUTO":
            status = "SEM MATCH ORIGEM DESTINO PRODUTO"
            reason = match_error
            stage = "MATCH FATURAMENTO"
            metrics["quantidade_sem_match"] += 1
        elif match_error == "FORA DO PERIODO":
            status = "FORA DO PERIODO"
            reason = match_error
            stage = "PERIODO"
        elif value_billed in (None, 0):
            status = "SEM VALOR FATURADO"
            reason = "Total do conhec. vazio ou zerado"
            stage = "VALOR FATURADO"
        else:
            diff = (value_billed or 0) - (value_coupa or 0)
            status = "OK" if abs(diff) <= tolerance else "DIVERGENTE"
            stage = "CALCULO"
        if value_coupa in (None, 0) or value_billed in (None, 0):
            metrics["quantidade_valor_zero"] += 1
            reason = reason or "Valor zero na origem"
            if status == "OK":
                status = "VALOR ZERO NA ORIGEM"
        observation = (
            f"Valor Coupa: {value_coupa}; Valor faturado KMM: {value_billed}; "
            f"Volume faturado: {volume_billed}; Motivo: {reason}"
        )
        _insert_coupa_faturado_analysis(conn, row, value_coupa, value_billed, volume_billed, qtd_billed, status, observation, "coupa")
        metrics["quantidade_gerada"] += 1
        if status != "OK":
            _insert_coupa_faturado_diag(conn, row, status, stage, reason, value_coupa, value_billed, volume_billed, username, processed_at)
            active.add(
                upsert_inconsistency(
                    conn,
                    {
                        "signature": f"inc:coupa_faturado:{status}:{_value(row, 'id')}",
                        "modulo_origem": "Coupa x Faturado",
                        "tipo_inconsistencia": status,
                        "origem": _value(row, "origem"),
                        "destino": _value(row, "destino"),
                        "produto": _value(row, "produto"),
                        "valor_envolvido": value_billed,
                        "volume_envolvido": volume_billed,
                        "diferenca_valor": (value_billed or 0) - (value_coupa or 0),
                        "prioridade": "ALTA" if status == "DIVERGENTE" else "MEDIA",
                        "observacao": observation,
                    },
                )
            )

    coupa_keys = {_route_product_key(row) for row in coupa_rows}
    for row in kmm_rows:
        row_id = int(_value(row, "id", 0) or 0)
        if row_id in matched_kmm_ids or _route_product_key(row) in coupa_keys:
            continue
        fake_row = {
            "id": f"kmm_{row_id}",
            "resposta_transportador": _value(row, "cobranca"),
            "origem": _value(row, "origem"),
            "destino": _value(row, "destino"),
            "produto": _product(row),
            "dt_inicio": "",
            "dt_termino": "",
            "qtd": 0,
            "item": _value(row, "cte_numero"),
        }
        status = "FATURADO SEM COUPA"
        observation = "Registro faturado no KMM sem correspondencia na base Coupa."
        _insert_coupa_faturado_analysis(
            conn,
            fake_row,
            None,
            _number(_value(row, "total_conhecimento")),
            _number(_value(row, "volume")),
            1,
            status,
            observation,
            f"kmm_{row_id}",
        )
        metrics["quantidade_sem_match"] += 1
        metrics["quantidade_gerada"] += 1

    metrics["registros_gerados"] = metrics["quantidade_gerada"]
    metrics["registros_processados"] = metrics["registros_coupa_lidos"] + metrics["registros_kmm_lidos"]
    metrics["active"] = active
    return metrics
