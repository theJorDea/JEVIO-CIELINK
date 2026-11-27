import asyncio
from decimal import Decimal

import pytest

from cielink.consent import ConfirmedDialogue
from cielink.consultation import Consultation
from cielink.crm import OperationStore
from cielink.domain import SessionContext, SessionState
from cielink.quotes import FixtureQuoteAdapter


@pytest.fixture
def call():
    state = SessionState(context=SessionContext(session_id='test', channel='sip'))
    # No write is expected on any of these turns.
    dialogue = ConfirmedDialogue(Consultation(FixtureQuoteAdapter()), None)

    def say(text):
        prompt = asyncio.run(dialogue.respond(state, text))
        dialogue.delivered()
        return prompt

    say('Учебная 18 корпус один')
    return state, dialogue, say


@pytest.mark.parametrize('reply', ['да', 'нет'])
def test_ambiguous_correction_cannot_confirm_or_decline_order(call, reply):
    state, dialogue, say = call
    address = state.address
    say('До двух тысяч в месяц')
    assert state.monthly_budget == Decimal(2000) and state.address == address
    assert 'корпус два' in say('Нет, второй')
    assert state.address == address and state.active_offer is None
    assert dialogue.proposal is None
    say(reply)
    assert state.monthly_budget == Decimal(2000)
    assert not dialogue.finished and dialogue.receipt is None
    if reply == 'да':
        assert state.address.corpus == '2' and dialogue.proposal.kind == 'coverage_request'
    else:
        assert state.address == address and dialogue.proposal is None
        say('да')  # a rejected guess cannot be revived by an unscoped yes
        assert state.address == address and dialogue.proposal is None


@pytest.mark.parametrize(('house', 'regular'), [('корпус один', '1700'), ('строение один', '2000')])
def test_post_promo_price_tracks_current_house_only(call, house, regular):
    state, dialogue, say = call
    say(house)
    question = 'А сколько будет стоить после акции?'
    answer = say(question)
    assert regular in answer and 'нет акции' in answer and dialogue.proposal is None
    say('Стоп, корпус второй')
    assert regular not in say(question)
    assert state.address.corpus == '2' and state.active_offer is None
    assert dialogue.proposal.kind == 'coverage_request' and dialogue.proposal.terms['offer'] is None


def test_equipment_and_unrecognized_followups_preserve_facts_not_consent(call):
    state, dialogue, say = call
    say('До двух тысяч в месяц')
    address, offer = state.address, state.active_offer
    say('Можно подробнее?')
    assert state.address == address and state.active_offer == offer and dialogue.proposal is None
    say('У меня нет роутера')
    for turn in ('угу', 'да оформляйте', 'А если иначе?', 'До 2200', 'Учебная 18 строение один'):
        answer = say(turn)
        assert 'Оформить заявку?' not in answer
        if turn in ('угу', 'да оформляйте'):
            assert 'роутер' in answer and 'адрес' not in answer.casefold()
        assert state.needs == ['роутер'] and state.active_offer is None and dialogue.proposal is None
    assert state.monthly_budget == 2200 and state.address.structure == '1'


def test_price_question_then_order_intent_requires_fresh_delivered_confirmation(tmp_path):
    store = OperationStore(f'sqlite:///{tmp_path / "orders.db"}')
    state = SessionState(context=SessionContext(session_id='price', channel='web'))
    dialogue = ConfirmedDialogue(Consultation(FixtureQuoteAdapter()), store)

    def say(text):
        return asyncio.run(dialogue.respond(state, text))

    terms = say('Учебная 18 корпус один, до 2000')
    old_key = dialogue.proposal.key
    dialogue.delivered()
    say('А сколько будет стоить после акции?')
    assert dialogue.proposal is None
    assert say('да оформляйте') == terms
    new_key = dialogue.proposal.key
    assert old_key != new_key and not dialogue.proposal.delivered
    assert say('да оформляйте') == terms
    assert store.lookup_operation(old_key) is None and store.lookup_operation(new_key) is None
    dialogue.delivered()
    say('да оформляйте')
    receipt = store.lookup_operation(new_key)
    assert receipt.operation_type == 'order' and receipt.confirmed_terms['monthly_budget'] == '2000'
