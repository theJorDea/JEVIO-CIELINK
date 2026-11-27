import pytest
from pydantic import TypeAdapter, ValidationError

from cielink.domain import (
    AddressResult, AmbiguousAddress, CoverageResult, FinalInput, HandlerResult,
    PartialInput, SessionContext, SessionState, TranscriptInput, handle_input,
)


def new_state(channel="web"):
    return SessionState(context=SessionContext(session_id="demo", channel=channel))


@pytest.mark.parametrize("channel", ["web", "sip"])
def test_partial_then_final(channel):
    start = new_state(channel)
    partial = handle_input(start, PartialInput(text="подклю", offset_ms=100))
    assert start.sequence == 0 and not start.revisions
    assert partial.next_action.kind == "wait"
    assert partial.state.partial_text == "подклю"
    assert partial.state.final_text is None

    incoming = TypeAdapter(TranscriptInput).validate_python(
        {"type": "final", "text": "подключить интернет", "offset_ms": 200}
    )
    assert isinstance(incoming, FinalInput)
    final = handle_input(partial.state, incoming)
    assert final.event.model_dump() == {
        "session_id": "demo", "sequence": 2, "offset_ms": 200,
        "type": "final", "revision": 2, "text": incoming.text,
    }
    assert final.state.partial_text is None and final.state.final_text == incoming.text
    assert final.state.revisions == {"transcript": 2}
    assert final.state.pending_confirmation is None
    assert final.next_action.kind == "ask_address" and final.next_action.text
    assert HandlerResult.model_validate_json(final.model_dump_json()) == final


def test_reject_out_of_order_without_changing_state():
    state = handle_input(new_state(), FinalInput(text="да", offset_ms=20)).state
    with pytest.raises(ValueError, match="offset_ms"):
        handle_input(state, PartialInput(text="новая фраза", offset_ms=19))
    assert state.final_text == "да" and state.sequence == 1


def test_channel_budget_and_input_validation():
    with pytest.raises(ValidationError):
        new_state("chat")
    with pytest.raises(ValidationError):
        SessionState(context=new_state().context, monthly_budget=-1)
    with pytest.raises(ValidationError):
        TypeAdapter(TranscriptInput).validate_python({"text": "да", "offset_ms": 20})


def test_ambiguous_address_is_not_a_coverage_result():
    candidates = [
        {"street": "Учебная", "house": "3", "corpus": str(i), "house_id": f"h{i}"}
        for i in (1, 2)
    ]
    data = {"status": "needs_clarification", "clarify_field": "corpus",
            "building_candidates": candidates}
    result = TypeAdapter(AddressResult).validate_python(data)
    assert isinstance(result, AmbiguousAddress)
    assert [address.house_id for address in result.building_candidates] == ["h1", "h2"]
    with pytest.raises(ValidationError):
        CoverageResult(house_id="h1", status="needs_clarification")
