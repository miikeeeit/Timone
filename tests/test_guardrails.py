"""Test dei guardrail — la copertura più importante del progetto."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from timone import guardrails as gr
from timone.models import OrderSide, ProposedOrder

ROME = ZoneInfo("Europe/Rome")


def buy(ticker="AAA", eur=100.0):
    return ProposedOrder(ticker=ticker, side=OrderSide.BUY, notional_eur=eur, reason="test")


def sell(ticker="AAA", eur=100.0):
    return ProposedOrder(ticker=ticker, side=OrderSide.SELL, notional_eur=eur, reason="test")


# --- Àncora -----------------------------------------------------------------

def test_ancora_attiva_blocca():
    assert gr.check_anchor(True).approved is False


def test_ancora_alzata_passa():
    assert gr.check_anchor(False).approved is True


# --- Finestra oraria --------------------------------------------------------

def test_dentro_finestra():
    now = datetime(2026, 7, 6, 16, 0, tzinfo=ROME)  # lunedì 16:00
    assert gr.check_trading_window(now).approved is True


def test_prima_della_finestra():
    now = datetime(2026, 7, 6, 9, 0, tzinfo=ROME)
    assert gr.check_trading_window(now).approved is False


def test_dopo_la_finestra():
    now = datetime(2026, 7, 6, 22, 30, tzinfo=ROME)
    assert gr.check_trading_window(now).approved is False


def test_finestra_converte_timezone():
    # 21:00 UTC == 23:00 ora di Roma (estate) -> fuori finestra.
    now = datetime(2026, 7, 6, 21, 0, tzinfo=ZoneInfo("UTC"))
    assert gr.check_trading_window(now).approved is False


def test_estremi_finestra_inclusi():
    start = datetime(2026, 7, 6, 15, 30, tzinfo=ROME)
    end = datetime(2026, 7, 6, 22, 0, tzinfo=ROME)
    assert gr.check_trading_window(start).approved is True
    assert gr.check_trading_window(end).approved is True


# --- Whitelist --------------------------------------------------------------

def test_whitelist_ammette_ticker_in_rotta():
    assert gr.check_ticker_whitelist("AAA", {"AAA", "BBB"}).approved is True


def test_whitelist_rifiuta_ticker_estraneo():
    res = gr.check_ticker_whitelist("ZZZ", {"AAA", "BBB"})
    assert res.approved is False
    assert res.rule == "whitelist"


# --- Max ordine -------------------------------------------------------------

def test_max_order_entro_limite():
    assert gr.check_max_order(gr.MAX_ORDER_EUR).approved is True


def test_max_order_oltre_limite():
    res = gr.check_max_order(gr.MAX_ORDER_EUR + 0.01)
    assert res.approved is False
    assert res.rule == "max_order_eur"


# --- Budget giornaliero -----------------------------------------------------

def test_budget_entro_limite():
    assert gr.check_daily_budget(100.0, 0.0).approved is True


def test_budget_esatto_al_limite_passa():
    assert gr.check_daily_budget(gr.MAX_DAILY_EUR, 0.0).approved is True


def test_budget_superato_blocca():
    res = gr.check_daily_budget(1.0, gr.MAX_DAILY_EUR)
    assert res.approved is False
    assert res.rule == "daily_budget"


# --- Max ordini per run -----------------------------------------------------

def test_max_orders_sotto_tetto():
    assert gr.check_max_orders_per_run(gr.MAX_ORDERS_PER_RUN - 1).approved is True


def test_max_orders_al_tetto_blocca():
    assert gr.check_max_orders_per_run(gr.MAX_ORDERS_PER_RUN).approved is False


# --- evaluate_order (composito) ---------------------------------------------

def test_evaluate_ok():
    res = gr.evaluate_order(
        buy(eur=100.0),
        allowed_tickers={"AAA"},
        spent_today_eur=0.0,
        approved_so_far=0,
    )
    assert res.approved is True


def test_evaluate_rifiuta_ticker_estraneo():
    res = gr.evaluate_order(
        buy(ticker="ZZZ"),
        allowed_tickers={"AAA"},
        spent_today_eur=0.0,
        approved_so_far=0,
    )
    assert res.rule == "whitelist"


def test_evaluate_vendita_non_consuma_budget():
    # Budget già al massimo, ma una VENDITA non spende: deve passare.
    res = gr.evaluate_order(
        sell(eur=100.0),
        allowed_tickers={"AAA"},
        spent_today_eur=gr.MAX_DAILY_EUR,
        approved_so_far=0,
    )
    assert res.approved is True


def test_evaluate_acquisto_oltre_budget_blocca():
    res = gr.evaluate_order(
        buy(eur=100.0),
        allowed_tickers={"AAA"},
        spent_today_eur=gr.MAX_DAILY_EUR,
        approved_so_far=0,
    )
    assert res.rule == "daily_budget"


def test_evaluate_ordine_troppo_grande_blocca():
    res = gr.evaluate_order(
        buy(eur=gr.MAX_ORDER_EUR + 50),
        allowed_tickers={"AAA"},
        spent_today_eur=0.0,
        approved_so_far=0,
    )
    assert res.rule == "max_order_eur"


# --- Idempotenza ------------------------------------------------------------

def test_client_order_id_deterministico():
    a = gr.client_order_id("20260706", "AAA", OrderSide.BUY)
    b = gr.client_order_id("20260706", "AAA", OrderSide.BUY)
    assert a == b


def test_client_order_id_varia_per_parametri():
    base = gr.client_order_id("20260706", "AAA", OrderSide.BUY)
    assert base != gr.client_order_id("20260707", "AAA", OrderSide.BUY)
    assert base != gr.client_order_id("20260706", "BBB", OrderSide.BUY)
    assert base != gr.client_order_id("20260706", "AAA", OrderSide.SELL)


def test_client_order_id_formato():
    cid = gr.client_order_id("20260706", "AAA", OrderSide.BUY)
    assert cid.startswith("timone-20260706-AAA-buy-")
