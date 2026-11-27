import asyncio

import pytest

from cielink.addresses import FixtureAddressAdapter, FixtureCoverageAdapter
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
def test_unmatched_or_conflicting_input_never_falls_back_to_known_house(text):
    assert asyncio.run(addresses.resolve_address(text)).status == "not_found"


def test_address_provider_error_is_distinct_from_not_found():
    offline = FixtureAddressAdapter(provider_error=True)
    assert asyncio.run(offline.resolve_address("18 корпус 1")).status == "error"


def test_absent_coverage_record_does_not_claim_an_outage_or_nonservice():
    checked = asyncio.run(coverage.check_coverage("outside-fixture"))
    assert (checked.house_id, checked.status) == ("outside-fixture", "unknown")
