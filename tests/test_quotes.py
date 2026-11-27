import asyncio
from datetime import date
from cielink.domain import NoOffer, Offers, QuoteRequest
from cielink.quotes import FixtureQuoteAdapter


def quote(**changes):
    adapter = FixtureQuoteAdapter()
    data = dict(house_id='training-house-a', on_date=date(2026, 11, 27), catalog_version=adapter.version)
    return asyncio.run(adapter.quote(QuoteRequest(**(data | changes))))


def test_advance_not_added_again_as_monthly_payment():
    result = quote(budget_today=1700, monthly_budget=1700)
    assert isinstance(result, Offers)
    assert [o.offer_id for o in result.offers] == ['t-400-500-monthly']
    offer = result.offers[0]
    assert offer.advance == 1700 and offer.periods[-1].monthly_price == 1700
    assert sum(offer.mandatory_charges.values()) == 0


def test_rejects_unknown_post_promo_conditions_and_excess_budget():
    result = quote(monthly_budget=900)
    assert isinstance(result, NoOffer)
    assert 'ежемесячный бюджет' in result.reason and 'после акции' in result.reason
    assert 'бюджет при подключении' in quote(budget_today=1699).reason


def test_expired_promo_and_catalog_revision():
    assert 'истёк' in quote(on_date=date(2026, 12, 1), monthly_budget=900).reason
    assert 'Версия' in quote(catalog_version='stale').reason
    assert 'не подтверждена' in quote(house_id='not-in-fixture').reason
