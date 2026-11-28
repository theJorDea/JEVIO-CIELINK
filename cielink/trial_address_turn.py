"""Deterministic address fragments for the Gemini trial; no lookup or session I/O."""
import re
from dataclasses import dataclass

from .addresses import DemoAddressAdapter


_RESET = re.compile(r'(?:(?:давайте|проверим|давайте проверим) )?(?:другой|новый) адрес(?: посмотрим)?')
_FILLERS = re.compile(r'^(?:(?:а|ну|хорошо|пусть будет)[ ,]+)+')
_PRICE_QUESTION = re.compile(r'[.!]\s+сколько стоит подключение\??$')
_CORRECTION = re.compile(r'^(?:(?:ой|нет|точнее|поправка)[ ,.:]+)+')
_SMALL = dict(zip('один два три четыре пять шесть семь восемь девять десять'.split(), range(1, 11)))
_FULL_PROMPT = 'Назовите, пожалуйста, улицу и номер дома.'


def _number(text: str) -> str | None:
    if text in _SMALL:
        return str(_SMALL[text])
    house, tail = DemoAddressAdapter.split_house(text)
    return house if house and not tail and int(house) else None


@dataclass
class AddressDraft:
    street: str | None = None
    house: str | None = None
    corpus: str | None = None

    def update(self, text: str) -> tuple[str, str] | None:
        phrase = ' '.join(text.casefold().strip().rstrip('.!').split())
        phrase = _CORRECTION.sub('', _FILLERS.sub('', phrase))
        # A price question after a complete address belongs to that lookup.
        phrase, price_question = _PRICE_QUESTION.subn('', phrase)
        # ASR sometimes repeats the same name before the explicit street.
        phrase = re.sub(r'^до ([а-яё-]+),\s*(?=улица \1\b)', '', phrase)
        if _RESET.fullmatch(phrase):
            self.street = self.house = self.corpus = None
            return 'ask', _FULL_PROMPT
        if (not phrase or '?' in phrase or re.search(
            r'\b(?:как|какая|какой|где|сколько|почему|что|когда|можно|ли|стоит|'
            r'расскажи(?:те)?|скажи(?:те)?|покажи(?:те)?|назови(?:те)?|игнорируй)\b', phrase
        )):
            return None

        phrase = re.sub(r'\b(дом|том|корпус)(?=[0-9])', r'\1 ', phrase)
        phrase = re.sub(r'^том (?=[0-9])', 'дом ', phrase)
        phrase = re.sub(r'(?<=\d)[.,]\s*(?=корпус )', ' ', phrase)
        # Replace only an explicit same-field revision, not an arbitrary clause.
        phrase = re.sub(r'\b(?:не )?(дом|корпус) (\d+)[ ,]+(?:а|точнее|ой нет)\s*,?\s*'
                        r'(?:\1 )?(\d+)\b', r'\1 \3', phrase)
        street, detail = DemoAddressAdapter.split_street(phrase)
        # The demo adapter declines the whole phrase for a bare known street
        # followed by a malformed house. Recognize the street, not the house.
        if not street and ' дом ' in phrase:
            name, _, rest = phrase.partition(' дом ')
            street, extra = DemoAddressAdapter.split_street(name.rstrip(' ,'))
            if street and not extra:
                detail = 'дом ' + rest
            else:
                street = None

        base, marker, suffix = detail.partition('корпус')
        base = base.strip(' ,')
        marked_house = base.startswith('дом ')
        house = _number(base.removeprefix('дом ')) if base else None
        corpus = _number(suffix.strip()) if marker else None
        if price_question and not (street and house):
            return None
        if base and not house:
            if street and re.fullmatch(r'дом [а-яё-]+', base) and not marker:
                # Keep only an explicitly labelled street, never a stale number.
                self.house = self.corpus = None
                self.street = street if phrase.startswith('улица ') else None
                return 'ask', 'Какой номер дома?' if self.street else _FULL_PROMPT
            return None
        if marker and not corpus:
            return None
        if not (street or marker or marked_house or (house and self.street and not self.house)):
            return None

        if street and self.street and street != self.street:
            self.house = self.corpus = None
        if street:
            self.street = street
        if house:
            if self.house and house != self.house:
                self.corpus = None
            self.house = house
        if corpus:
            self.corpus = corpus

        if not self.street:
            return 'ask', 'Назовите, пожалуйста, улицу.' if self.house else _FULL_PROMPT
        if not self.house:
            return 'ask', 'Какой номер дома?'
        address = f'{self.street}, дом {self.house}'
        if self.corpus:
            address += f', корпус {self.corpus}'
        return 'lookup', address
