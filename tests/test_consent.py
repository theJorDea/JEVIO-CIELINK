import asyncio
from uuid import uuid4
import pytest
from cielink.domain import SessionState, SessionContext
from cielink.consultation import Consultation
from cielink.consent import ConfirmedDialogue
from cielink.quotes import FixtureQuoteAdapter
from cielink.crm import OperationStore


@pytest.fixture
def case():
    from tempfile import NamedTemporaryFile
    from pathlib import Path
    with NamedTemporaryFile(delete=False) as f:
        path = Path(f.name)
    try:
        store = OperationStore(f'sqlite:///{path}')
        state = SessionState(context=SessionContext(session_id=uuid4().hex, channel='web'))
        dialogue = ConfirmedDialogue(Consultation(FixtureQuoteAdapter()), store)
        yield dialogue, lambda text: asyncio.run(dialogue.respond(state, text)), store
    finally:
        path.unlink(missing_ok=True)


def test_interrupted_terms_cannot_be_accepted_and_ack_is_not_consent(case):
    d, say, store = case
    terms = say('Учебная 18 корпус один')
    assert all(s in terms for s in ('аванс 1700', 'обязательные доплаты 0', 'ежемесячно 1700', 'собственный роутер'))
    key = d.proposal.key
    assert say('да, подключайте') == terms  # client interrupted before price
    d.delivered()
    assert say('да, я слышу') == terms and store.lookup_operation(key) is None
    d.delivered()
    reprompt = say('да')
    assert reprompt.startswith(terms) and 'скажите: «да, оформляйте»' in reprompt
    assert say('да') == reprompt and store.lookup_operation(key) is None
    assert say('да, оформляйте') == reprompt and store.lookup_operation(key) is None
    d.delivered()
    answer = say('да, оформляйте')
    receipt = store.lookup_operation(key)
    assert receipt.confirmed_terms['prompt'] == reprompt
    assert receipt.operation_type == 'order' and answer == f'Сохранено. Номер {receipt.spoken_reference}.'
    assert say('да, подключайте') == f'Операция уже сохранена. Номер {receipt.spoken_reference}.'


def test_correction_with_old_consent_can_only_create_new_check(case):
    d, say, store = case
    say('Учебная 18 корпус один'); key = d.proposal.key; d.delivered()
    consent = say('стоп, корпус второй')
    d.delivered()
    assert say('да оформляйте') == consent and store.lookup_operation(key) is None
    d.delivered(); say('да проверьте адрес')
    assert d.receipt.operation_type == 'coverage_request'
    assert d.receipt.confirmed_terms['address']['corpus'] == '2'
    assert d.receipt.confirmed_terms['offer'] is None


def test_decline_or_no_service_never_creates_order(case):
    d, say, _ = case
    say('Учебная 18/2'); say('да')
    assert d.finished and d.receipt is None


def test_deferral_after_price_retires_offer(case):
    dialogue, say, store = case
    say('Учебная 18 корпус один')
    key = dialogue.proposal.key
    dialogue.delivered()
    assert 'не оформляю' in say('Я подумаю')
    say('да, подключайте')
    assert dialogue.finished and store.lookup_operation(key) is None


def test_explicit_handoff_or_refusal(case):
    d, say, store = case
    prompt = say('хочу оператора'); key = d.proposal.key; d.delivered()
    assert say('да') == prompt and store.lookup_operation(key) is None
    d.delivered()
    assert say('да оформляйте') == prompt and store.lookup_operation(key) is None
    say('не надо'); say('да')
    assert d.finished and store.lookup_operation(key) is None
