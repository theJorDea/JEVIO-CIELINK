"""Offline Учебная улица fixtures; this is not a general Russian address parser."""

import re

from cielink.domain import (
    Address, AddressAdapter, AddressResult, AmbiguousAddress, CoverageAdapter,
    CoverageResult, ResolvedAddress, UnresolvedAddress,
)


def _house(house_id: str, **parts: str) -> Address:
    return Address(house_id=house_id, city="Санкт-Петербург",
                   street="Учебная улица", house="18", **parts)


_HOUSES = (
    _house("training-house-a", corpus="1"), _house("uchebnaya-18-k2", corpus="2"),
    _house("uchebnaya-18-f2", fraction="2"), _house("training-house-b", structure="1"),
    _house("uchebnaya-18-la", letter="А"),
)
_COVERAGE = {
    "training-house-a": "available", "uchebnaya-18-k2": "unknown",
    "uchebnaya-18-f2": "unavailable", "training-house-b": "available",
    "uchebnaya-18-la": "error",
}

# Match the whole utterance. An unrecognised city, street or suffix must not
# disappear while we search for a familiar house number.
_CITY = r"(?:(?:город\s+|г\.\s*)?(?:санкт[- ]петербург|спб)\s+)?"
_STREET = r"(?:(?:(?:улица\s+|ул\.\s*)учебная|учебная(?:\s+улица)?|на\s+учебной(?:\s+улице)?)\s+)?"
_ADDRESS = re.compile(_CITY + _STREET + r"(?:дом\s+|д\.\s*)?(?:18|восемнадцать)(?P<suffix>.*)")
_MARKERS = {"корпус": "corpus", "к": "corpus", "корп": "corpus",
            "дробь": "fraction", "/": "fraction", "строение": "structure",
            "стр": "structure", "литера": "letter", "литер": "letter", "лит": "letter"}
_NUMBERS = {"один": "1", "первый": "1", "первое": "1", "два": "2", "второй": "2"}
_SUFFIX = re.compile(r"\s*(?:(корпус|корп|к|строение|стр|литера|литер|лит|дробь|/)\.?\s*)?"
                     r"([а-я]+|[0-9]+)?")


class FixtureAddressAdapter(AddressAdapter):
    def __init__(self, *, provider_error: bool = False) -> None:
        self._provider_error = provider_error

    async def resolve_address(self, text: str) -> AddressResult:
        if self._provider_error:
            return UnresolvedAddress(status="error")
        normalized = " ".join(text.casefold().replace(",", " ").split()).rstrip(".")
        match = _ADDRESS.fullmatch(normalized)
        if not match:
            return UnresolvedAddress(status="not_found")
        suffix = match["suffix"]
        part = _SUFFIX.fullmatch(suffix)
        if not part or suffix[:1].isdigit():  # 182 is not house 18, component 2.
            return UnresolvedAddress(status="not_found")
        marker, word = part.groups()
        field = _MARKERS.get(marker)
        value = _NUMBERS.get(word, word)
        if value == "а":
            field, value = field or "letter", "А"
        fields = (field,) if field else ("corpus", "fraction", "structure", "letter")
        candidates = [house.model_copy() for house in _HOUSES
                      if any(getattr(house, f) is not None and
                             (value is None or getattr(house, f) == value) for f in fields)]
        if len(candidates) > 1:
            return AmbiguousAddress(status="needs_clarification", building_candidates=candidates,
                                    clarify_field=field or "building_component")
        if len(candidates) == 1 and field and value:
            return ResolvedAddress(status="resolved", address=candidates[0])
        return UnresolvedAddress(status="not_found")


class FixtureCoverageAdapter(CoverageAdapter):
    async def check_coverage(self, house_id: str) -> CoverageResult:
        return CoverageResult(house_id=house_id, status=_COVERAGE.get(house_id, "unknown"))
