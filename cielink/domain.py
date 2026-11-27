"""Channel-independent call contracts. Monetary amounts are in RUB."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PositiveInt


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


Text = Annotated[str, Field(min_length=1)]
Rubles = Annotated[Decimal, Field(ge=0, decimal_places=2)]
ActionKind = Literal["order", "coverage_request", "handoff"]


class SessionContext(Contract):
    session_id: Text
    channel: Literal["web", "sip"]
    phone_number: str | None = None


class Address(Contract):
    house_id: Text
    street: Text
    house: Text
    city: str | None = None
    fraction: str | None = None
    corpus: str | None = None
    structure: str | None = None
    letter: str | None = None


class ResolvedAddress(Contract):
    status: Literal["resolved"]
    address: Address


class AmbiguousAddress(Contract):
    status: Literal["needs_clarification"]
    building_candidates: Annotated[list[Address], Field(min_length=2)]
    clarify_field: Text


class UnresolvedAddress(Contract):
    status: Literal["not_found", "error"]


AddressResult = Annotated[
    ResolvedAddress | AmbiguousAddress | UnresolvedAddress, Field(discriminator="status")
]


class CoverageResult(Contract):
    house_id: Text
    status: Literal["available", "unavailable", "unknown", "error"]


class QuoteRequest(Contract):
    house_id: Text
    on_date: date
    catalog_version: Text
    budget_today: Rubles | None = None
    monthly_budget: Rubles | None = None
    needs: list[str] = Field(default_factory=list)


class PricePeriod(Contract):
    months: PositiveInt | None = None  # None means recurring indefinitely.
    monthly_price: Rubles


class Offer(Contract):
    offer_id: Text
    house_id: Text
    catalog_version: Text
    advance: Rubles
    periods: Annotated[list[PricePeriod], Field(min_length=1)]
    mandatory_charges: dict[str, Rubles]
    services: list[str]
    conditions: str
    source_url: Text
    source_date: date


class Offers(Contract):
    offers: Annotated[list[Offer], Field(min_length=1, max_length=2)]


class NoOffer(Contract):
    reason: Text


QuoteResult = Offers | NoOffer


class ActionRequest(Contract):
    kind: ActionKind
    house_id: Text
    confirmed_terms: Text
    operation_key: Text


class ActionReceipt(Contract):
    action_id: Text
    operation_key: Text
    status: Text


class AddressAdapter(Protocol):
    async def resolve_address(self, text: str) -> AddressResult: ...


class CoverageAdapter(Protocol):
    async def check_coverage(self, house_id: str) -> CoverageResult: ...


class QuoteAdapter(Protocol):
    async def quote(self, request: QuoteRequest) -> QuoteResult: ...


class ActionAdapter(Protocol):
    async def submit_action(self, request: ActionRequest) -> ActionReceipt: ...

    async def get_action_status(self, operation_key: str) -> ActionReceipt | None: ...


class PendingConfirmation(Contract):
    kind: ActionKind
    revision: NonNegativeInt
    terms: Text


class SessionState(Contract):
    context: SessionContext
    address: Address | None = None
    on_date: date | None = None
    budget_today: Rubles | None = None
    monthly_budget: Rubles | None = None
    needs: list[str] = Field(default_factory=list)
    revisions: dict[str, NonNegativeInt] = Field(default_factory=dict)
    active_offer: Offer | None = None
    pending_confirmation: PendingConfirmation | None = None
    sequence: NonNegativeInt = 0
    offset_ms: NonNegativeInt = 0
    partial_text: str | None = None
    final_text: str | None = None


class PartialInput(Contract):
    type: Literal["partial"] = "partial"
    text: Text
    offset_ms: NonNegativeInt


class FinalInput(Contract):
    type: Literal["final"] = "final"
    text: Text
    offset_ms: NonNegativeInt


TranscriptInput = Annotated[PartialInput | FinalInput, Field(discriminator="type")]


class Event(Contract):
    session_id: Text
    sequence: PositiveInt
    offset_ms: NonNegativeInt
    type: Text
    revision: NonNegativeInt
    text: str | None = None


class NextAction(Contract):
    kind: Literal["wait", "ask_address"]
    text: str | None = None


class HandlerResult(Contract):
    state: SessionState
    event: Event
    next_action: NextAction


def handle_input(state: SessionState, incoming: TranscriptInput) -> HandlerResult:
    """Consume one STT update; a final transcript is not consent to a write."""
    if incoming.offset_ms < state.offset_ms:
        raise ValueError("offset_ms precedes the previous event")
    sequence = state.sequence + 1
    revision = state.revisions.get("transcript", 0) + 1
    event = Event(session_id=state.context.session_id, sequence=sequence,
                  offset_ms=incoming.offset_ms, type=incoming.type, revision=revision,
                  text=incoming.text)
    final = isinstance(incoming, FinalInput)
    updated = state.model_copy(update={
        "sequence": sequence, "offset_ms": incoming.offset_ms,
        "revisions": {**state.revisions, "transcript": revision},
        "partial_text": None if final else incoming.text,
        "final_text": incoming.text if final else state.final_text,
    })
    action = (NextAction(kind="ask_address", text="По какому адресу нужно подключение?")
              if final and state.address is None else NextAction(kind="wait"))
    return HandlerResult(state=updated, event=event, next_action=action)
