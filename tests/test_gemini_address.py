import asyncio

import pytest

from cielink.gemini_address import check_address


def test_exact_building_returns_verified_price_periods():
    result = asyncio.run(check_address('улица Пушкина 12 корпус 13'))
    assert result['status'] == 'resolved'
    assert result['address'] == 'Улица Пушкина, дом 12, корпус 13'
    assert result['coverage_status'] == 'available'
    offer = result['offer']
    assert offer['house_id'] == 'demo-pushkina-12-k13'
    assert offer['advance'] == '1450'
    assert offer['mandatory_charges'] == {'connection': '0', 'equipment': '0'}
    assert offer['periods'] == [{'months': 3, 'monthly_price': '950'},
                                {'months': None, 'monthly_price': '1450'}]


@pytest.mark.parametrize(('text', 'status', 'coverage'), [
    ('дом 18 корпус 1', 'not_found', 'unknown'),
    ('улица Пушкина 12', 'needs_clarification', 'unknown'),
    ('улица Пушкина 12 корпус 14', 'resolved', 'unknown'),
    ('улица Пушкина 12 дробь 13', 'resolved', 'unavailable'),
    ('улица Пушкина 28', 'resolved', 'error'),
])
def test_no_offer_for_unconfirmed_address_or_coverage(text, status, coverage):
    result = asyncio.run(check_address(text))
    assert result['status'] == status
    assert result['coverage_status'] == coverage
    assert result['offer'] is None


def test_demo_lookup_leaves_persistent_store_untouched(tmp_path, monkeypatch):
    db = tmp_path / 'demo.sqlite3'
    monkeypatch.setenv('CIELINK_DEMO_ADDRESS_DB', str(db))
    result = asyncio.run(check_address('улица Звёздная 91 корпус 3'))
    assert result['address'] == 'Улица Звёздная, дом 91, корпус 3'
    assert result['coverage_status'] in {'available', 'unavailable'}
    assert (result['offer'] is not None) == (result['coverage_status'] == 'available')
    assert not db.exists()
