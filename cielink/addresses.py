"""Address matching and coverage against an offline training directory."""

import json
import re
import sqlite3

from cielink.demo_storage import DemoAddressStore
from pathlib import Path

from cielink.domain import (
    Address, AddressAdapter, AddressResult, AmbiguousAddress, CoverageAdapter,
    CoverageResult, ResolvedAddress, UnresolvedAddress,
)


_ROWS = json.loads((Path(__file__).resolve().parents[1] / 'fixtures' / 'addresses.json').read_text(encoding='utf-8'))
_COVERAGE = {row['house_id']: row['status'] for row in _ROWS}
OFFER_PROFILES = {row['house_id']: row['offer_profile'] for row in _ROWS if 'offer_profile' in row}
_HOUSES = {}
_ALIASES = {}
for row in _ROWS:
    address = Address(city='Санкт-Петербург', **{k: v for k, v in row.items() if k not in {'status', 'offer_profile'}})
    _HOUSES.setdefault((address.street, address.house), []).append(address)
    name = address.street.casefold().replace('ё', 'е').removeprefix('улица ').removesuffix(' улица')
    aliases = [name, f'улица {name}', f'ул. {name}', f'{name} улица', f'на улице {name}']
    if name.endswith('ая'):
        inflected = name[:-2] + 'ой'
        aliases.extend([f'на {inflected}', f'на {inflected} улице'])
    _ALIASES.update(dict.fromkeys(aliases, address.street))

# Match the whole utterance. An unrecognised city, street or suffix must not
# disappear while we search for a familiar house number.
_CITY = re.compile(r"^(?:город\s+|г\.\s*)?(?:санкт[- ]петербург|спб)\s+")
_STREET = re.compile(r'^(?:' + '|'.join(map(re.escape, sorted(_ALIASES, key=len, reverse=True))) + r')(?=\s|$)')
_WORDS = dict(zip(('один', 'двенадцать', 'восемнадцать', 'двадцать', 'двадцать один', 'двадцать два', 'двадцать три',
                   'двадцать четыре', 'двадцать пять', 'двадцать шесть', 'двадцать семь', 'двадцать восемь',
                   'двадцать девять'), ('1', '12', '18', *map(str, range(20, 30)))))
_ADDRESS = re.compile(r'(?:дом\s+|д\.\s*)?(?P<house>[0-9]{1,4}|'
                      + '|'.join(sorted(_WORDS, key=len, reverse=True)) + r')(?P<suffix>.*)')
_MARKERS = {"корпус": "corpus", "к": "corpus", "корп": "corpus",
            "дробь": "fraction", "/": "fraction", "строение": "structure",
            "стр": "structure", "литера": "letter", "литер": "letter", "лит": "letter"}
_NUMBERS = {"один": "1", "первый": "1", "первое": "1", "два": "2", "второй": "2",
            "три": "3", "третий": "3", "пять": "5", "пятый": "5",
            "тринадцать": "13", "тринадцатый": "13", "четырнадцать": "14", "четырнадцатый": "14"}
_SUFFIX = re.compile(r"\s*(?:(корпус|корп|к|строение|стр|литера|литер|лит|дробь|/)\.?\s*)?"
                     r"([а-я]+|[0-9]+)?")


class FixtureAddressAdapter(AddressAdapter):
    def __init__(self, *, provider_error: bool = False) -> None:
        self._provider_error = provider_error

    @staticmethod
    def split_street(text: str) -> tuple[str | None, str]:
        normalized = ' '.join(text.casefold().replace('ё', 'е').replace(',', ' ').split()).rstrip('.')
        normalized = re.sub(r'\bул\.\s*', 'ул. ', _CITY.sub('', normalized))
        if match := _STREET.match(normalized):
            return _ALIASES[match[0]], normalized[match.end():].strip()
        return None, normalized

    @staticmethod
    def split_house(detail: str) -> tuple[str | None, str]:
        if match := _ADDRESS.fullmatch(detail):
            suffix = re.sub(r'^\s*\.\s*(?=(?:корпус|строение|литера|дробь)\b)', ' ', match['suffix'])
            return _WORDS.get(match['house'], match['house']), suffix
        return None, detail

    async def resolve_address(self, text: str) -> AddressResult:
        if self._provider_error:
            return UnresolvedAddress(status="error")
        street, detail = self.split_street(text)
        house, suffix = self.split_house(detail)
        if not house:
            return UnresolvedAddress(status="not_found")
        houses = _HOUSES.get((street or 'Учебная улица', house), ())
        if len(houses) == 1 and not suffix and all(getattr(houses[0], field) is None for field in _MARKERS.values()):
            return ResolvedAddress(status='resolved', address=houses[0].model_copy())
        part = _SUFFIX.fullmatch(suffix)
        if not part or suffix[:1].isdigit():  # 182 is not house 18, component 2.
            return UnresolvedAddress(status="not_found")
        marker, word = part.groups()
        field = _MARKERS.get(marker)
        value = _NUMBERS.get(word, word)
        if value == "а":
            field, value = field or "letter", "А"
        fields = (field,) if field else ("corpus", "fraction", "structure", "letter")
        candidates = [house.model_copy() for house in houses
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


_TYPES = {'улица': 'улица', 'ул': 'улица', 'улицы': 'улица', 'проспект': 'проспект',
          'пр-т': 'проспект', 'переулок': 'переулок', 'пер': 'переулок', 'шоссе': 'шоссе',
          'набережная': 'набережная', 'наб': 'набережная', 'бульвар': 'бульвар', 'площадь': 'площадь'}
_TYPE = '(?:' + '|'.join(_TYPES) + r')\.?'
_WORD = r'[а-яё]+(?:-[а-яё]+)*'
_NAME = rf'{_WORD}(?:\s+{_WORD}){{0,3}}'
_EXPLICIT = re.compile(rf'(?:(?P<prefix>{_TYPE})(?:\s+|(?<=\.))(?P<name>{_NAME})|(?P<name2>{_NAME})\s+(?P<suffix>{_TYPE}))')
_HOUSE_START = re.compile(r'(?:\s*,\s*|\s+)(?=(?:дом\b|д\.|[0-9]|' + '|'.join(_WORDS) + r'\b))')
_NUM = r'(?:[0-9]{1,3}|' + '|'.join(_NUMBERS) + ')'
_COMPONENT = re.compile(r'\s*(?:(корпус|корп|к|строение|стр|дробь|/)\.?\s*(' + _NUM +
                        r')|(?:литера|литер|лит)\.?\s*([а-яё])|([а-яё])(?=\s|$))')
_RESERVED = set(_TYPES) | set(_MARKERS) | {'и', 'или', 'не', 'дом', 'город', 'г', 'москва', 'бюджет'}


class DemoAddressAdapter(FixtureAddressAdapter):
    def __init__(self, store: DemoAddressStore) -> None:
        super().__init__()
        self.store = store

    @staticmethod
    def split_street(text: str) -> tuple[str | None, str]:
        if len(text) > 256:
            return None, text
        normalized = ' '.join(text.casefold().split()).rstrip('.')
        normalized = re.sub(r'^(?:город\s+|г\.\s*)?(?:санкт[- ]петербург|спб)(?:\s*,\s*|\s+)', '', normalized)
        normalized = re.sub(r'^улицы\s+', 'улица ', normalized)
        street, detail = FixtureAddressAdapter.split_street(normalized)
        if street and (not detail or FixtureAddressAdapter.split_house(detail)[0]):
            return street, detail
        boundary = _HOUSE_START.search(normalized)
        name = normalized[:boundary.start()] if boundary else normalized
        match = _EXPLICIT.fullmatch(name)
        if not match:
            return None, normalized
        name = match['name'] or match['name2']
        if any(word in _RESERVED for word in name.split()):
            return None, normalized
        kind = _TYPES[(match['prefix'] or match['suffix']).rstrip('.')]
        canonical = f'{kind} {name.title()}'
        street = _ALIASES.get(canonical.casefold().replace('ё', 'е'), canonical)
        detail = normalized[boundary.end():] if boundary else ''
        return street, ' '.join(detail.replace(',', ' ').split())

    async def resolve_address(self, text: str) -> AddressResult:
        street, detail = self.split_street(text)
        if not street:
            return UnresolvedAddress(status='not_found')
        fixture = await super().resolve_address(text)
        if fixture.status != 'not_found':
            return fixture
        house, tail = self.split_house(detail)
        if not house or not int(house) or tail[:1].isdigit():
            return fixture
        parts = dict(city='Санкт-Петербург', street=street, house=str(int(house)))
        while tail:
            component = _COMPONENT.match(tail)
            if not component:
                return fixture
            marker, number, letter, bare_letter = component.groups()
            if bare_letter and (tail.startswith(' ') or bare_letter in {'к', 'с'}):
                return fixture
            key = _MARKERS[marker] if marker else 'letter'
            if key in parts:
                return fixture
            if number:
                value = str(int(_NUMBERS.get(number, number)))
                if value == '0':
                    return fixture
            else:
                value = (letter or bare_letter).upper()
            parts[key] = value
            tail = tail[component.end():]
        matches = _HOUSES.get((street, parts['house']), ())
        if matches and len(parts) == 3:
            return await super().resolve_address(f"{street} {parts['house']}")
        for known in matches:
            if known.model_dump(exclude={'house_id'}, exclude_none=True) == parts:
                return ResolvedAddress(status='resolved', address=known.model_copy())
        try:
            address = self.store.resolve(parts)
        except (sqlite3.Error, OSError):
            return UnresolvedAddress(status='error')
        return ResolvedAddress(status='resolved', address=address)


class DemoCoverageAdapter(FixtureCoverageAdapter):
    def __init__(self, store: DemoAddressStore) -> None:
        self.store = store

    async def check_coverage(self, house_id: str) -> CoverageResult:
        if house_id in _COVERAGE or not house_id.startswith('demo-'):
            return await super().check_coverage(house_id)
        return CoverageResult(house_id=house_id, status=self.store.status(house_id))
