import json
import os
import time

import pytest

from cielink_asr.addresses import (
    AddressCatalog,
    AddressCatalogError,
    ConnectionIntake,
    CoverageStatus,
    ObjectType,
)
from cielink_asr.consultation import Consultation


def sample_catalog():
    return {
        "streets_max_length": 12,
        "streets": "Лесная".ljust(12) + "Набережная".ljust(12),
        "homes": [
            [
                {"id": 1, "t": "10", "s": 0},
                {"id": 2, "t": "10 корпус 2", "s": 1},
                {"id": 3, "t": "12", "s": 2},
                {"id": 4, "t": "14", "s": 3},
            ],
            [{"id": 5, "t": "1", "s": 3}],
        ],
    }


def test_catalog_unpacks_street_chunks_and_house_statuses():
    catalog = AddressCatalog.from_json(sample_catalog())

    assert [street.name for street in catalog.streets] == ["Лесная", "Набережная"]
    assert [house.status for house in catalog.streets[0].houses] == [
        CoverageStatus.UNKNOWN,
        CoverageStatus.NEW,
        CoverageStatus.PLANNED,
        CoverageStatus.CONNECTED,
    ]


def test_catalog_search_is_case_insensitive_and_matches_full_house_label():
    catalog = AddressCatalog.from_json(sample_catalog())

    matches = catalog.find_houses("лесная", "10 корпус 2")

    assert len(matches) == 1
    assert [street.name for street in catalog.find_streets("улица Лесная")] == ["Лесная"]
    assert matches[0].house.id == 2
    assert matches[0].house.status is CoverageStatus.NEW
    assert catalog.find_houses("Лесная", "1") == ()

    spoken = catalog.find_houses("улица Лесная", "дом десять, корпус второй")
    assert [match.house.id for match in spoken] == [2]


def test_catalog_finds_explicit_street_and_house_mention():
    catalog = AddressCatalog.from_json(sample_catalog())
    matches = catalog.find_mentions("Проверить подключение на Лесная, дом 14")
    assert len(matches) == 1
    assert matches[0].house.id == 4


def test_catalog_prefers_specific_house_label_over_shorter_prefix():
    catalog = AddressCatalog.from_json(sample_catalog())
    matches = catalog.find_mentions("ул. Лесная, дом 10 корпус 2")
    assert [match.house.id for match in matches] == [2]


def test_catalog_resolves_spoken_house_numbers_and_inflected_street_names():
    catalog = AddressCatalog.from_json(sample_catalog())

    matches = catalog.find_mentions("На улице Лесной дом десять корпус второй")

    assert [match.house.id for match in matches] == [2]


def test_catalog_understands_ordinal_corpus_and_street_case():
    payload = sample_catalog()
    payload["homes"][0].append({"id": 10, "t": "10 корпус 1", "s": 3})
    catalog = AddressCatalog.from_json(payload)

    matches = catalog.find_mentions("на улице Лесной дом десять корпус первый")

    assert [match.house.id for match in matches] == [10]


def test_catalog_does_not_fall_back_to_base_house_when_corpus_is_unknown():
    catalog = AddressCatalog.from_json(sample_catalog())

    matches = catalog.find_mentions("Лесная, дом десять корпус первый")

    assert matches == ()


def test_catalog_keeps_independent_numbers_as_ambiguous_candidates():
    payload = sample_catalog()
    payload["homes"][0].append({"id": 6, "t": "1000", "s": 3})
    catalog = AddressCatalog.from_json(payload)

    matches = catalog.find_mentions("Лесная, дом 14, бюджет до 1000 рублей")

    assert {match.house.id for match in matches} == {4, 6}


def test_catalog_rejects_invalid_street_offsets_and_statuses():
    payload = sample_catalog()
    payload["homes"][0][0]["s"] = 4
    with pytest.raises(AddressCatalogError, match="Invalid SkyNet"):
        AddressCatalog.from_json(payload)


def test_catalog_uses_stale_valid_cache_when_refresh_fails(tmp_path):
    cache = tmp_path / "addresses.json"
    cache.write_text(json.dumps(sample_catalog()), encoding="utf-8")
    old_time = time.time() - 2 * 86_400
    os.utime(cache, (old_time, old_time))

    def unavailable(*args, **kwargs):
        raise OSError("offline")

    catalog = AddressCatalog.load(cache, opener=unavailable)

    assert len(catalog.streets) == 2
    assert catalog.loaded_at.timestamp() < time.time() - 86_400


def test_catalog_does_not_overwrite_valid_cache_with_invalid_refresh(tmp_path):
    cache = tmp_path / "addresses.json"
    cache.write_text(json.dumps(sample_catalog()), encoding="utf-8")
    old_time = time.time() - 2 * 86_400
    os.utime(cache, (old_time, old_time))

    class InvalidResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"streets": "broken"}'

    catalog = AddressCatalog.load(cache, opener=lambda *args, **kwargs: InvalidResponse())

    assert [street.name for street in catalog.streets] == ["Лесная", "Набережная"]


def test_connection_intake_asks_and_remembers_explicit_object_type():
    intake = ConnectionIntake()

    assert intake.question_if_missing() == "Подключение нужно в квартиру, частный дом или для бизнеса?"
    assert intake.capture_object_type("Это квартира") == ObjectType.APARTMENT
    assert intake.question_if_missing() is None
    assert intake.capture_object_type("Нет, частный дом") == ObjectType.PRIVATE_HOUSE


def test_connection_intake_recognizes_business():
    intake = ConnectionIntake()
    assert intake.capture_object_type("Подключение на юридическое лицо") == ObjectType.BUSINESS


def test_connection_intake_does_not_route_existing_subscriber_to_sales():
    assert not ConnectionIntake.is_new_connection_request("Я уже абонент, интернет пропал")
    assert ConnectionIntake.is_support_request("Я уже абонент, интернет пропал")
    assert ConnectionIntake.is_support_request("Я уже абонент")
    assert not ConnectionIntake.is_support_request(
        "Я уже абонент, хочу подключить второй адрес"
    )


def test_consultation_keeps_object_type_separate_from_address():
    consultation = Consultation.start(AddressCatalog.from_json(sample_catalog()))

    assert consultation.next_question() is None
    assert consultation.handle_text("Хочу подключить интернет") == (
        "Подключение нужно в квартиру, частный дом или для бизнеса?"
    )
    assert consultation.handle_text("Хочу интернет для частного дома") == (
        "Назовите улицу, номер дома и корпус, если он есть."
    )
    assert consultation.next_question() == "Назовите улицу, номер дома и корпус, если он есть."
    matches = consultation.set_address("Лесная", "14")

    assert len(matches) == 1
    assert consultation.coverage_status() is CoverageStatus.CONNECTED


def test_consultation_resolves_address_mentioned_in_transcript():
    consultation = Consultation.start(AddressCatalog.from_json(sample_catalog()))
    consultation.handle_text("Хочу подключить интернет в квартиру")

    matches = consultation.address_mentions("Адрес Лесная, 14")

    assert len(matches) == 1
    assert consultation.address.house.id == 4
    assert "дом отмечен как подключённый" in consultation.context_text()


def test_changed_street_takes_precedence_over_previous_house_lookup():
    payload = sample_catalog()
    payload["homes"][0].append({"id": 6, "t": "1", "s": 0})
    consultation = Consultation.start(AddressCatalog.from_json(payload))
    consultation.process("Хочу интернет в квартире, Лесная 14")
    consultation.process("Нет, Набережная дом 1")
    assert consultation.address.house.id == 5


@pytest.mark.parametrize("previous", [False, True])
def test_apartment_number_is_not_a_catalog_house(previous):
    consultation = Consultation.start(AddressCatalog.from_json(sample_catalog()))
    consultation.process("Хочу интернет в квартире")
    if previous:
        consultation.process("Лесная 12")
    turn = consultation.process("дом 99, квартира 14" if previous else "Лесная дом 99, квартира 14")
    assert turn.kind.value == "address_unknown"
    assert consultation.address is None


def test_internet_question_preserves_current_address():
    consultation = Consultation.start(AddressCatalog.from_json(sample_catalog()))
    consultation.process("Хочу интернет в квартире, Лесная 14")
    turn = consultation.process("А интернет быстрый?")
    assert turn.kind.value == "general"
    assert consultation.address.house.id == 4


def test_explicit_second_address_order_starts_intake_for_existing_subscriber():
    consultation = Consultation.start(AddressCatalog.from_json(sample_catalog()))
    turn = consultation.process("Я уже абонент, хочу подключить второй адрес")
    assert turn.kind.value == "ask_object_type"
    assert consultation.connection_requested
