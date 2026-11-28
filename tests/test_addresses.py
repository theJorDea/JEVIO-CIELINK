import asyncio

import pytest

from cielink.addresses import DemoAddressAdapter, DemoCoverageAdapter, FixtureAddressAdapter, FixtureCoverageAdapter
from cielink.demo_storage import DemoAddressStore
from cielink.domain import AddressAdapter, CoverageAdapter


addresses: AddressAdapter = FixtureAddressAdapter()
coverage: CoverageAdapter = FixtureCoverageAdapter()


@pytest.mark.parametrize("text, house_id, field, value, status", [
    ("г. Санкт-Петербург, Учебная улица, д. 18 к. 1", "training-house-a", "corpus", "1", "available"),
    ("улица Учебная, дом восемнадцать, корпус первый", "training-house-a", "corpus", "1", "available"),
    ("18 корпус 2", "uchebnaya-18-k2", "corpus", "2", "unknown"),
    ("на Учебной улице дом восемнадцать корпус второй", "uchebnaya-18-k2", "corpus", "2", "unknown"),
    ("18/2", "uchebnaya-18-f2", "fraction", "2", "unavailable"),
    ("дом восемнадцать дробь два", "uchebnaya-18-f2", "fraction", "2", "unavailable"),
    ("18 строение один", "training-house-b", "structure", "1", "available"),
    ("18 стр.1", "training-house-b", "structure", "1", "available"),
    ("18 литера А", "uchebnaya-18-la", "letter", "А", "error"),
    ("18А", "uchebnaya-18-la", "letter", "А", "error"),
])
def test_resolved_fixture_and_its_coverage(text, house_id, field, value, status):
    result = asyncio.run(addresses.resolve_address(text))
    assert result.status == "resolved"
    address = result.address
    assert (address.city, address.street, address.house) == ("Санкт-Петербург", "Учебная улица", "18")
    assert getattr(address, field) == value
    assert address.house_id == house_id
    checked = asyncio.run(coverage.check_coverage(address.house_id))
    assert (checked.house_id, checked.status) == (address.house_id, status)


@pytest.mark.parametrize("text, field, house_ids", [
    ("Учебная улица 18", "building_component", {
        "training-house-a", "uchebnaya-18-k2", "uchebnaya-18-f2", "training-house-b", "uchebnaya-18-la",
    }),
    ("дом восемнадцать два", "building_component", {"uchebnaya-18-k2", "uchebnaya-18-f2"}),
    ("18 корпус", "corpus", {"training-house-a", "uchebnaya-18-k2"}),
])
def test_clarification_includes_even_unknown_or_unavailable_houses(text, field, house_ids):
    result = asyncio.run(addresses.resolve_address(text))
    assert result.status == "needs_clarification"
    assert result.clarify_field == field
    assert {a.house_id for a in result.building_candidates} == house_ids


@pytest.mark.parametrize("text", [
    "", "дом", "Москва, Учебная улица, 18 корпус 1", "СПб, Лесная улица, 18/2",
    "18/2, Москва", "18 корпус 2 улица Лесная", "корпус 2", "Учебная улица 19",
    "182", "18 корпус 3", "18 лит. Б", "18/2 корпус 1", "18 строение", "18 корпус 1 или 2",
])
def test_fixture_only_mode_never_falls_back_to_known_house(text):
    assert asyncio.run(addresses.resolve_address(text)).status == "not_found"


def test_address_provider_error_is_distinct_from_not_found():
    offline = FixtureAddressAdapter(provider_error=True)
    assert asyncio.run(offline.resolve_address("18 корпус 1")).status == "error"


def test_absent_coverage_record_does_not_claim_an_outage_or_nonservice():
    checked = asyncio.run(coverage.check_coverage("outside-fixture"))
    assert (checked.house_id, checked.status) == ("outside-fixture", "unknown")


@pytest.mark.parametrize('text, expected', [
    ('улица Пушкина 12 корпус 13', 'available'),
    ('Пушкина, дом 12, корпус 14', 'unknown'),
    ('Пушкина 12/13', 'unavailable'),
    ('на Лесной улице, дом 20', 'available'),
    ('Улица Пушкина, дом 1. Корпус 5.', 'available'),
])
def test_named_street_directory_keeps_address_and_coverage_separate(text, expected):
    result = asyncio.run(addresses.resolve_address(text))
    assert result.status == 'resolved'
    assert asyncio.run(coverage.check_coverage(result.address.house_id)).status == expected


def test_demo_normalization_and_persistence(tmp_path, monkeypatch):
    db = tmp_path / 'nested' / 'demo.sqlite'
    monkeypatch.setenv('CIELINK_DEMO_ADDRESS_DB', str(db))
    store = DemoAddressStore()
    adapter = DemoAddressAdapter(store)
    def resolve(text):
        return asyncio.run(adapter.resolve_address(text)).address
    assert adapter.split_street('Улицы Пушкина.') == ('улица Пушкина', '')
    assert adapter.split_street('улица Космонавтов') == ('улица Космонавтов', '')
    fixture = resolve('Улицы Пушкина, дом 1, корпус 5')
    assert fixture.house_id == 'demo-pushkina-1-k5'
    assert asyncio.run(DemoCoverageAdapter(store).check_coverage(fixture.house_id)).status == 'available'
    assert not db.exists()
    address = resolve('СПб, улица Космонавтов, дом 034, корпус 02')
    assert address == resolve('Космонавтов ул. 34к2')
    assert store.status(address.house_id) in {'available', 'unavailable'}
    assert DemoAddressStore().status(address.house_id) == store.status(address.house_id)
    assert DemoAddressStore().status('demo-missing') == 'unknown'
    assert len({address.house_id, *(resolve('улица Космонавтов 34' + part).house_id
                                   for part in ('/2', ' стр.2', 'А', ''))}) == 5
    assert resolve('на Лесной улице дом двадцать корпус второй').house_id.startswith('demo-')


@pytest.mark.parametrize('text', ['дом 119', 'дом 18 корпус 1', '18/2', '18', '120', 'улица Космонавтов', 'Москва, Садовая улица 45',
                                  'улица Садовая 19 или 20', 'улица Садовая 19 корпус',
                                  'улица Садовая 19/2/3', 'нет 500 рублей', 'ул. Садовая 19, Москва'])
def test_invalid_demo_address_is_not_stored(tmp_path, text):
    store = DemoAddressStore(tmp_path / 'demo.sqlite')
    assert asyncio.run(DemoAddressAdapter(store).resolve_address(text)).status == 'not_found'
    assert not store.path.exists()


def test_deterministic_synthetic_coverage_distribution(tmp_path):
    store = DemoAddressStore(tmp_path / 'test.sqlite')
    statuses = [store.status(store.resolve(dict(city='Санкт-Петербург', street='улица Северная', house=str(n))).house_id)
                for n in range(1, 101)]
    assert 70 <= statuses.count('available') <= 90
    assert set(statuses) == {'available', 'unavailable'}
