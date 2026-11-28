"""Small deterministic consultation state used by a voice/API adapter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .addresses import AddressCatalog, AddressMatch, ConnectionIntake, CoverageStatus


STATUS_TEXT = {
    CoverageStatus.UNKNOWN: "нет сведений",
    CoverageStatus.NEW: "новый статус в каталоге",
    CoverageStatus.PLANNED: "подключение запланировано в каталоге",
    CoverageStatus.CONNECTED: "дом отмечен как подключённый",
}


class TurnKind(str, Enum):
    GENERAL = "general"
    ASK_OBJECT_TYPE = "ask_object_type"
    ASK_ADDRESS = "ask_address"
    ADDRESS_FOUND = "address_found"
    ADDRESS_AMBIGUOUS = "address_ambiguous"
    ADDRESS_UNKNOWN = "address_unknown"
    SUPPORT = "support"


@dataclass(frozen=True)
class ConsultationTurn:
    kind: TurnKind
    reply: str
    matches: tuple[AddressMatch, ...] = ()
    address_changed: bool = False


@dataclass
class Consultation:
    """Keep user-provided slots outside the LLM and expose safe next actions."""

    catalog: AddressCatalog
    intake: ConnectionIntake
    address: AddressMatch | None = None
    connection_requested: bool = False
    support_requested: bool = False
    address_search_attempted: bool = False
    address_changed_this_turn: bool = False

    @classmethod
    def start(cls, catalog: AddressCatalog) -> Consultation:
        return cls(catalog=catalog, intake=ConnectionIntake())

    def handle_text(self, text: str) -> str | None:
        """Capture an explicit object type and return the next required question."""
        support_request = ConnectionIntake.is_support_request(text)
        new_connection = ConnectionIntake.is_new_connection_request(text)
        if support_request:
            self.support_requested = True
        elif new_connection:
            self.support_requested = False
            self.connection_requested = True
        self.intake.capture_object_type(text)
        if not self.connection_requested:
            return None
        return self.next_question()

    def address_mentions(self, text: str) -> tuple[AddressMatch, ...]:
        self.address_changed_this_turn = False
        normalized_text = " ".join(text.casefold().replace("ё", "е").split())
        if self.address and self._is_address_correction(normalized_text):
            previous_street = self.address.street
            previous_house = self.address.house
            corpus_only_correction = (
                "корпус" in normalized_text
                or "корп " in normalized_text
                or " к " in normalized_text
            ) and not any(
                self.catalog.street_is_mentioned(previous_street, normalized_text)
                or marker in normalized_text
                for marker in ("дом ", "д ")
            )
            if corpus_only_correction:
                corrected_houses = self.catalog.find_corpus_correction(
                    previous_street, previous_house, normalized_text
                )
            else:
                corrected_houses = self.catalog.find_house_mentions(
                    previous_street, normalized_text
                )
            self.address_search_attempted = True
            self.address = None
            if len(corrected_houses) == 1:
                self.address = AddressMatch(
                    previous_street,
                    corrected_houses[0],
                )
                self.address_changed_this_turn = True
                return (self.address,)
        street_mentioned = any(
            self.catalog.street_is_mentioned(street, normalized_text)
            for street in self.catalog.streets
        )
        if street_mentioned:
            self.address_search_attempted = True
            self.address = None
        matches = self.catalog.find_mentions(text)
        if len(matches) == 1:
            self.address = matches[0]
            self.address_changed_this_turn = True
        elif len(matches) > 1:
            self.address = None
        return matches

    @staticmethod
    def _is_address_correction(text: str) -> bool:
        return any(
            marker in text
            for marker in ("нет", "поправка", "точнее", "ошибся", "ошиблась", "корпус", "корп ", "дом ")
        )

    def set_address(self, street: str, house: str) -> tuple[AddressMatch, ...]:
        matches = self.catalog.find_houses(street, house)
        self.address = matches[0] if len(matches) == 1 else None
        return matches

    def coverage_status(self) -> CoverageStatus | None:
        return self.address.house.status if self.address else None

    def context_text(self) -> str:
        parts = [f"Тип объекта: {self.intake.object_type or 'не указан'}."]
        if self.address:
            parts.append(
                "Адрес каталога: "
                f"{self.address.street.name}, дом {self.address.house.label}; "
                f"статус: {STATUS_TEXT[self.address.house.status]}; "
                f"house_id: {self.address.house.id}."
            )
        else:
            parts.append("Точный адрес в каталоге ещё не найден.")
        return " ".join(parts)

    def next_question(self) -> str | None:
        if not self.connection_requested:
            return None
        if self.intake.question_if_missing():
            return self.intake.question_if_missing()
        if self.address is None:
            if self.address_search_attempted:
                return "Не нашёл точный номер дома в каталоге. Проверьте номер и корпус или уточните адрес у специалиста SkyNet."
            return "Назовите улицу, номер дома и корпус, если он есть."
        return None

    def process(self, text: str) -> ConsultationTurn:
        """Apply one transcript and return a deterministic business outcome."""
        self.handle_text(text)
        matches = (
            self.address_mentions(text) if self.connection_requested else ()
        )
        if self.support_requested:
            return ConsultationTurn(
                TurnKind.SUPPORT,
                "Понял, вопрос по действующему интернету. Этот тестовый агент пока "
                "не переключает звонки в поддержку.",
                matches,
                self.address_changed_this_turn,
            )
        if self.intake.question_if_missing() and self.connection_requested:
            return ConsultationTurn(
                TurnKind.ASK_OBJECT_TYPE,
                self.intake.question_if_missing() or "",
                matches,
            )
        if not self.connection_requested:
            return ConsultationTurn(TurnKind.GENERAL, "", matches)
        if len(matches) > 1:
            return ConsultationTurn(
                TurnKind.ADDRESS_AMBIGUOUS,
                "Нашёл несколько вариантов адреса. Уточните улицу, номер дома и корпус.",
                matches,
            )
        if self.address and self.address_changed_this_turn:
            prefix = "Для бизнеса условия проверяются отдельно. " if self.intake.object_type == "business" else ""
            address = self.address
            return ConsultationTurn(
                TurnKind.ADDRESS_FOUND,
                prefix
                + f"В каталоге SkyNet для адреса {address.street.name}, дом {address.house.label} "
                + f"указано: {STATUS_TEXT[address.house.status]}. Это справочный статус, "
                + "а не подтверждение подключения.",
                matches,
                True,
            )
        if self.address:
            return ConsultationTurn(TurnKind.GENERAL, "", matches)
        if self.address_search_attempted:
            return ConsultationTurn(
                TurnKind.ADDRESS_UNKNOWN,
                "Не нашёл точный номер дома в каталоге. Проверьте номер и корпус или "
                "уточните адрес у специалиста SkyNet.",
                matches,
            )
        return ConsultationTurn(
            TurnKind.ASK_ADDRESS,
            "Назовите улицу, номер дома и корпус, если он есть.",
            matches,
        )
