import asyncio
from decimal import Decimal

import pytest

from cielink.consent import ConfirmedDialogue
from cielink.consultation import Consultation
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
    for turn in ('да подключайте', 'А если иначе?', 'До 2200', 'Учебная 18 строение один'):
        assert 'Оформить заявку?' not in say(turn)
        assert state.needs == ['роутер'] and state.active_offer is None and dialogue.proposal is None
    assert state.monthly_budget == 2200 and state.address.structure == '1'
