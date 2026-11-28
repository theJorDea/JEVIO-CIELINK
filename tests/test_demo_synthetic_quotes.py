import asyncio
from datetime import date
from unittest.mock import Mock

import pytest

from cielink.domain import NoOffer, Offers, QuoteRequest
from cielink.quotes import FixtureQuoteAdapter


def quote_for(status, **kwargs):
    store = Mock()
    store.status.return_value = status
    adapter = FixtureQuoteAdapter(demo_store=store)
    request = QuoteRequest(house_id='demo-deadbeef', catalog_version=adapter.version,
                           on_date=date.fromisoformat(adapter.version), **kwargs)
    result = asyncio.run(adapter.quote(request))
    store.status.assert_called_once_with(request.house_id)
    return result


def test_synthetic_offer_preserves_the_quoted_building_and_budget_checks():
    result = quote_for('available')
    assert isinstance(result, Offers)
    assert all(offer.house_id == 'demo-deadbeef' for offer in result.offers)
    assert isinstance(quote_for('available', monthly_budget=1), NoOffer)


@pytest.mark.parametrize('status', ['unavailable', None])
def test_synthetic_offer_requires_persisted_availability(status):
    assert isinstance(quote_for(status), NoOffer)
