import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from cielink import gemini
from cielink.consultation import Consultation
from cielink.domain import SessionContext, SessionState
from cielink.quotes import FIXTURES, FixtureQuoteAdapter


@pytest.fixture
def call(monkeypatch):
    monkeypatch.setattr(gemini, 'classify_equipment_intent', AsyncMock(return_value='unknown'))
    consultation = Consultation(FixtureQuoteAdapter())
    state = SessionState(context=SessionContext(session_id='equipment', channel='web'))
    return consultation, lambda text: asyncio.run(consultation.respond(state, text))


@pytest.mark.parametrize(('turn', 'model_id'), [
    ('Первый. Нет, второй!', 'dual-band'),
    ('Давайте первый вариант. А, нет, давайте лучше второй.', 'dual-band'),
    ('Я понял, что у вас 2 варианта. Давайте мне первый.', 'basic'),
    ('Хорошо, можно всё-таки второй?', 'dual-band'),
    ('нет не второй а первый', 'basic'),
    ('Я беру вторую модель ой точнее базовую', 'basic'),
    ('Давайте двух диапазонный вай-фай', 'dual-band'),
    ('тогда вариант номер 2', 'dual-band'),
    ('тогда мне бы тот что ловит на двух частотах', 'dual-band'),
])
def test_choices_resolve_only_to_presented_fixture_rows(call, turn, model_id):
    consult, say = call
    catalog = json.loads((FIXTURES / 'equipment.json').read_text(encoding='utf-8'))
    listing = say('Какие есть роутеры?')
    assert all(row['name'] in listing for row in catalog)
    say('первый')
    reply = say(turn)
    expected = next(row for row in catalog if row['id'] == model_id)
    assert consult.selected_equipment == expected
    assert expected['name'] in reply and str(expected['price_rub']) in reply
    assert consult.proposed_action == 'handoff' and reply.endswith('?')


@pytest.mark.parametrize('turn', [
    'первый?', 'первый или второй', 'не хочу второй', 'второй сколько стоит',
    'закажите и доставьте второй', 'расскажите про второй', 'первый нет третий',
    'беру второй и оформите кредит', 'Хорошо, можно второй или первый?',
    'Я понял, что у вас 2 варианта.',
])
def test_non_selections_do_not_propose_an_operation(call, turn):
    consult, say = call
    say('Покажите роутеры')
    say(turn)
    assert consult.selected_equipment is None and consult.proposed_action is None


@pytest.mark.parametrize('selection', [None, 'второй'])
def test_acknowledgement_and_frequency_keep_equipment_context(call, selection):
    consult, say = call
    say('Какие есть роутеры?')
    if selection:
        say(selection)
    selected = consult.selected_equipment
    acknowledgement = say('Я понял')
    assert 'слушаю' in acknowledgement.casefold()
    assert consult.selected_equipment == selected and consult.proposed_action is None
    reply = say('А это разово или в месяц?')
    assert 'один раз' in reply
    if selected:
        assert str(selected['price_rub']) in reply
    assert consult.selected_equipment == selected and consult.proposed_action is None


def test_voice_routes_equipment_ack_and_unselected_frequency_locally():
    from cielink.voice import _VoiceConsultation

    consult = _VoiceConsultation(FixtureQuoteAdapter())
    state = SessionState(context=SessionContext(session_id='equipment-route', channel='web'))
    for turn in ('Какие есть роутеры?', 'Я понял', 'А это разово или в месяц?'):
        asyncio.run(consult.respond(state, turn))
        assert 'equipment' in consult.lookup_trace
        assert consult.route == 'local:equipment'
        assert consult.selected_equipment is None and consult.proposed_action is None


@pytest.mark.parametrize(('hint', 'ordinal'), [('first', 'первый'), ('second', 'второй'), ('unknown', None)])
def test_gemini_can_clarify_but_only_local_choice_can_select(call, monkeypatch, hint, ordinal):
    consult, say = call
    suggest = AsyncMock(return_value=hint)
    monkeypatch.setattr(gemini, 'classify_equipment_intent', suggest)
    say('Мне бы роутер помощнее')  # Presents both models; no prior list to choose from.
    say('Сколько стоит второй?')
    say('А чем отличаются роутеры?')
    suggest.assert_not_awaited()
    reply = say('Мне бы тот, который помощнее')
    suggest.assert_awaited_once_with('мне бы тот, который помощнее')
    assert consult.selected_equipment is None and consult.proposed_action is None
    if ordinal:
        assert f'беру {ordinal}' in reply
        say('да')
        assert consult.selected_equipment is None and consult.proposed_action is None
        say(f'беру {ordinal}')
        assert consult.selected_equipment == consult.presented_equipment[0 if hint == 'first' else 1]
        assert consult.proposed_action == 'handoff'
    else:
        assert all(row['name'] in reply for row in consult.presented_equipment)
    assert suggest.await_count == 1


def test_mention_alone_does_not_establish_presented_choices(call):
    consult, say = call
    say('А есть гарантия на роутер?')
    say('беру второй')
    assert consult.selected_equipment is None and consult.proposed_action is None
    # The clarification above has now actually named the models.
    say('беру второй')
    assert consult.selected_equipment['id'] == 'dual-band'
