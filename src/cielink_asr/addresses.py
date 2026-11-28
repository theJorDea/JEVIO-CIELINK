"""Public SkyNet order-form address catalog reader."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen


ADDRESS_CATALOG_URL = "https://skynet.ru/data/addresses.json"


class CoverageStatus(IntEnum):
    UNKNOWN = 0
    NEW = 1
    PLANNED = 2
    CONNECTED = 3


@dataclass(frozen=True)
class House:
    id: int
    label: str
    status: CoverageStatus


@dataclass(frozen=True)
class Street:
    name: str
    houses: tuple[House, ...]


@dataclass(frozen=True)
class AddressMatch:
    street: Street
    house: House


class AddressCatalogError(RuntimeError):
    """Raised when the public address catalog cannot be loaded or parsed."""


def _normalize(value: str) -> str:
    normalized = value.casefold().replace("ё", "е")
    tokens = re.findall(r"\d+[a-zа-я]*|[a-zа-я]+|[/\-]", normalized)
    return " ".join(_replace_number_words(tokens)).strip()


_NUMBER_WORDS = {
    "ноль": 0, "нуль": 0,
    "один": 1, "одна": 1, "одно": 1, "одного": 1, "одной": 1,
    "первый": 1, "первая": 1, "первое": 1, "первом": 1,
    "два": 2, "две": 2, "двух": 2, "второй": 2, "второго": 2, "втором": 2,
    "три": 3, "трех": 3, "третьего": 3, "третий": 3, "третьем": 3,
    "четыре": 4, "четырех": 4, "четвертого": 4, "четвертый": 4,
    "пять": 5, "пяти": 5, "пятого": 5, "пятый": 5,
    "шесть": 6, "шести": 6, "шестого": 6, "шестой": 6,
    "семь": 7, "семи": 7, "седьмого": 7, "седьмой": 7,
    "восемь": 8, "восьми": 8, "восьмого": 8, "восьмой": 8,
    "девять": 9, "девяти": 9, "девятого": 9, "девятый": 9,
    "десять": 10, "десяти": 10, "десятого": 10, "десятый": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16,
    "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19,
    "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100, "двести": 200, "триста": 300,
    "четыреста": 400, "пятьсот": 500,
}


def _replace_number_words(tokens: list[str]) -> list[str]:
    result = []
    index = 0
    while index < len(tokens):
        value = _NUMBER_WORDS.get(tokens[index])
        if value is None:
            result.append(tokens[index])
            index += 1
            continue

        total = value
        index += 1
        while index < len(tokens) and tokens[index] in _NUMBER_WORDS:
            next_value = _NUMBER_WORDS[tokens[index]]
            if next_value >= 100:
                total = max(total, 1) * next_value
            elif next_value >= 20:
                total += next_value
            elif total % 100 >= 20:
                total += next_value
            else:
                break
            index += 1
        result.append(str(total))
    return result


def _street_aliases(value: str) -> tuple[str, ...]:
    normalized = _normalize(value)
    aliases = [normalized]
    for prefix in ("улица ", "ул ", "проспект ", "просп ", "пр-т ", "переулок ", "пер "):
        if normalized.startswith(prefix):
            aliases.append(normalized[len(prefix) :])
    inflections = (
        ("улица ", ("улице ", "улицу ", "улицы ")),
        ("проспект ", ("проспекте ", "проспекту ", "проспекта ")),
        ("переулок ", ("переулке ", "переулку ", "переулка ")),
    )
    for base, forms in inflections:
        if normalized.startswith(base):
            name = normalized[len(base) :]
            aliases.extend(form + name for form in forms)
    for alias in tuple(aliases):
        last_token = alias.rsplit(" ", 1)[-1]
        if last_token.endswith("ая"):
            aliases.append(alias[: -len("ая")] + "ой")
            aliases.append(alias[: -len("ая")] + "ей")
        elif last_token.endswith("яя"):
            aliases.append(alias[: -len("яя")] + "ей")
        if alias.endswith(" улица"):
            aliases.append(alias[: -len(" улица")])
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def _normalize_house_text(value: str) -> str:
    normalized = _normalize(value)
    normalized = re.sub(r"\b(?:корпус|корп|к\.?)\b", "к", normalized)
    normalized = re.sub(r"\b(?:дом|д\.?)\b", " ", normalized)
    return " ".join(normalized.split())


def _house_pattern(label: str) -> str:
    tokens = _normalize_house_text(label).split()
    parts = []
    for token in tokens:
        if token == "к":
            parts.append(r"(?:к|корп(?:ус)?)\.?")
        elif token in {"/", "-"}:
            parts.append(r"[/\-]")
        else:
            parts.append(re.escape(token))
    return r"(?<!\w)" + r"\s*".join(parts) + r"(?!\w)"


def _remove_contained_house_matches(
    matches: list[tuple[int, int, AddressMatch]],
) -> tuple[AddressMatch, ...]:
    selected: list[tuple[int, int, AddressMatch]] = []
    for start, end, match in sorted(
        matches, key=lambda item: (item[0], -(item[1] - item[0]))
    ):
        if any(
            start >= selected_start and end <= selected_end
            for selected_start, selected_end, _ in selected
        ):
            continue
        selected = [
            item
            for item in selected
            if not (item[0] >= start and item[1] <= end)
        ]
        selected.append((start, end, match))
    return tuple(match for _, _, match in selected)


@dataclass(frozen=True)
class AddressCatalog:
    streets: tuple[Street, ...]
    source_url: str = ADDRESS_CATALOG_URL
    loaded_at: datetime | None = None

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> AddressCatalog:
        try:
            packed_streets = payload["streets"]
            chunk_length = int(payload["streets_max_length"])
            groups = payload["homes"]
            if chunk_length <= 0 or len(packed_streets) != chunk_length * len(groups):
                raise ValueError("street chunks do not match house groups")

            streets = []
            for index, raw_houses in enumerate(groups):
                start = index * chunk_length
                name = packed_streets[start : start + chunk_length].strip()
                if not name:
                    raise ValueError(f"street name is empty at index {index}")
                houses = tuple(
                    House(
                        id=int(raw["id"]),
                        label=str(raw["t"]).strip(),
                        status=CoverageStatus(int(raw["s"])),
                    )
                    for raw in raw_houses
                )
                streets.append(Street(name=name, houses=houses))
        except (KeyError, TypeError, ValueError) as exc:
            raise AddressCatalogError("Invalid SkyNet address catalog format") from exc
        return cls(tuple(streets))

    @classmethod
    def load(
        cls,
        cache_path: str | Path | None = None,
        *,
        opener: Callable[..., Any] | None = None,
    ) -> AddressCatalog:
        cache = Path(
            cache_path
            or os.getenv("CIELINK_ADDRESS_CACHE")
            or Path.home() / ".cache" / "cielink" / "skynet-addresses.json"
        )
        cached_catalog: AddressCatalog | None = None
        if cache.is_file():
            try:
                catalog = cls.from_json(json.loads(cache.read_text(encoding="utf-8")))
                cached_catalog = replace(
                    catalog,
                    loaded_at=datetime.fromtimestamp(
                        cache.stat().st_mtime, tz=timezone.utc
                    ),
                )
                age_seconds = (
                    datetime.now(timezone.utc) - cached_catalog.loaded_at
                ).total_seconds()
                if 0 <= age_seconds < 86_400:
                    return cached_catalog
            except (OSError, json.JSONDecodeError, AddressCatalogError):
                pass

        request = Request(ADDRESS_CATALOG_URL, headers={"User-Agent": "CIELINK/0.1"})
        try:
            with (opener or urlopen)(request, timeout=20) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            catalog = cls.from_json(payload)
        except (
            URLError,
            TimeoutError,
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            AddressCatalogError,
        ) as exc:
            if cached_catalog is not None:
                return cached_catalog
            raise AddressCatalogError(f"Cannot download SkyNet address catalog: {exc}") from exc

        temporary_path: Path | None = None
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=cache.parent, prefix=f"{cache.name}.", delete=False
            ) as temporary:
                temporary.write(raw)
                temporary_path = Path(temporary.name)
            temporary_path.replace(cache)
        except OSError:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return replace(catalog, loaded_at=datetime.now(timezone.utc))

    def find_streets(self, query: str) -> tuple[Street, ...]:
        aliases = _street_aliases(query)
        if not aliases:
            return ()
        exact = tuple(
            s
            for s in self.streets
            if any(alias == _normalize(s.name) for alias in aliases)
        )
        if exact:
            return exact
        return tuple(
            s
            for s in self.streets
            if any(alias in _normalize(s.name) for alias in aliases)
        )

    def find_houses(self, street_query: str, house_query: str) -> tuple[AddressMatch, ...]:
        streets = self.find_streets(street_query)
        house = _normalize_house_text(house_query)
        if not house:
            return ()
        return tuple(
            AddressMatch(street, item)
            for street in streets
            for item in street.houses
            if _normalize_house_text(item.label) == house
        )

    def find_mentions(self, text: str) -> tuple[AddressMatch, ...]:
        """Find exact street and house labels explicitly present in a transcript."""
        normalized_text = _normalize(text)
        corpus_specified = bool(
            re.search(r"\b(?:корп(?:ус)?|к)\b\s*(?:\d+|[a-zа-я]+)", normalized_text)
        )
        matches = []
        for street in self.streets:
            if not self.street_is_mentioned(street, normalized_text):
                continue
            street_matches = []
            for house in street.houses:
                label = _normalize_house_text(house.label)
                label = re.sub(r"\b(корпус|корп|корп\.)\b", "к", label)
                if label == "все дома":
                    continue
                if corpus_specified and " к " not in f" {label} ":
                    continue
                for found in re.finditer(_house_pattern(label), normalized_text):
                    street_matches.append(
                        (found.start(), found.end(), AddressMatch(street, house))
                    )
            matches.extend(_remove_contained_house_matches(street_matches))
        return tuple(matches)

    def find_house_mentions(self, street: Street, text: str) -> tuple[House, ...]:
        normalized_text = _normalize(text)
        corpus_specified = bool(
            re.search(r"\b(?:корп(?:ус)?|к)\b\s*(?:\d+|[a-zа-я]+)", normalized_text)
        )
        matches = []
        for house in street.houses:
            label = _normalize_house_text(house.label)
            if label == "все дома":
                continue
            if corpus_specified and " к " not in f" {label} ":
                continue
            if re.search(_house_pattern(label), normalized_text):
                matches.append((len(label), house))
        if not matches:
            return ()
        longest_label = max(length for length, _ in matches)
        return tuple(house for length, house in matches if length == longest_label)

    def find_corpus_correction(
        self, street: Street, previous_house: House, correction: str
    ) -> tuple[House, ...]:
        normalized = _normalize_house_text(correction)
        corpus_match = re.search(r"(?:к|корп(?:ус)?)\s+(\d+)", normalized)
        if not corpus_match:
            return ()
        previous_label = _normalize_house_text(previous_house.label)
        base_label = previous_label.split(" к ", 1)[0]
        corrected_label = f"{base_label} к {corpus_match.group(1)}"
        return tuple(
            house
            for house in street.houses
            if _normalize_house_text(house.label) == corrected_label
        )

    @staticmethod
    def street_is_mentioned(street: Street, text: str) -> bool:
        normalized_text = _normalize(text)
        return any(
            re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", normalized_text)
            for alias in _street_aliases(street.name)
        )


class ObjectType:
    APARTMENT = "apartment"
    PRIVATE_HOUSE = "private_house"
    BUSINESS = "business"


@dataclass
class ConnectionIntake:
    """Conversation slots for the connection type; never infer it from an address."""

    object_type: str | None = None

    @staticmethod
    def is_new_connection_request(text: str) -> bool:
        normalized = _normalize(text)
        support_intent = (
            "не работает",
            "пропал интернет",
            "сломался",
            "плохо работает",
            "медленный интернет",
            "техподдерж",
            "уже абонент",
            "я абонент",
            "не подключается",
        )
        if any(phrase in normalized for phrase in support_intent):
            return False
        new_connection_intent = (
            "подключ",
            "провести интернет",
            "хочу интернет",
            "покрытие по адресу",
            "проверить покрытие",
        )
        return any(phrase in normalized for phrase in new_connection_intent)

    @staticmethod
    def is_support_request(text: str) -> bool:
        normalized = _normalize(text)
        incident = any(
            phrase in normalized
            for phrase in (
                "не работает",
                "пропал интернет",
                "сломался",
                "плохо работает",
                "медленный интернет",
                "техподдерж",
                "не подключается",
            )
        )
        existing_customer = any(
            phrase in normalized for phrase in ("уже абонент", "я абонент", "действующий абонент")
        )
        explicit_new_order = any(
            phrase in normalized
            for phrase in (
                "хочу подключ",
                "нужно подключ",
                "подключить второй",
                "подключить еще",
                "новое подключение",
            )
        )
        return incident or (existing_customer and not explicit_new_order)

    def capture_object_type(self, text: str) -> str | None:
        normalized = _normalize(text)
        if any(word in normalized for word in ("юрлицо", "юридическое лицо", "бизнес", "офис", "коммерческ")):
            self.object_type = ObjectType.BUSINESS
        elif any(
            word in normalized
            for word in ("частный дом", "частного дома", "коттедж", "ижс")
        ):
            self.object_type = ObjectType.PRIVATE_HOUSE
        elif any(
            word in normalized
            for word in ("квартира", "квартиры", "квартире", "квартиру", "многоквартир", "апартамент")
        ):
            self.object_type = ObjectType.APARTMENT
        return self.object_type

    def question_if_missing(self) -> str | None:
        if self.object_type is not None:
            return None
        return "Подключение нужно в квартиру, частный дом или для бизнеса?"
