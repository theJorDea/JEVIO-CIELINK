import asyncio

from cielink.consultation import Consultation
from cielink.domain import SessionContext, SessionState
from cielink.quotes import FixtureQuoteAdapter


def test_simulated_calls_reuse_real_address_and_only_quote_available_houses(tmp_path, monkeypatch):
    monkeypatch.setenv('CIELINK_DEMO_ADDRESS_DB', str(tmp_path / 'addresses.sqlite3'))

    async def call(house):
        rules = Consultation(FixtureQuoteAdapter())
        state = SessionState(context=SessionContext(session_id='test', channel='web'))
        answer = await rules.respond(state, f'Улица Космонавтов, дом {house}, корпус 4.')
        address = state.address
        assert address.street.casefold() == 'улица космонавтов'
        assert (address.house, address.corpus) == (str(house), '4')
        assert rules.describe(address) in answer
        followup = await rules.respond(state, 'Ну ладно, а сколько будет стоить всё?')
        assert '?' not in followup
        if rules.coverage_status == 'available':
            assert state.active_offer.house_id == address.house_id
            assert f'{state.active_offer.periods[-1].monthly_price} рублей в месяц' in followup
            due = state.active_offer.advance + sum(state.active_offer.mandatory_charges.values())
            assert f'При подключении — {due} рублей' in followup
        else:
            assert rules.coverage_status == 'unavailable'
            assert state.active_offer is None and 'рублей' not in followup
        await rules.reads.close()
        return rules.coverage_status, address.house_id

    seen = {}
    for house in range(1, 51):
        status, house_id = asyncio.run(call(house))
        seen[status] = house, house_id
        if len(seen) == 2:
            break
    assert set(seen) == {'available', 'unavailable'}
    for status, (house, house_id) in seen.items():
        assert asyncio.run(call(house)) == (status, house_id)


def test_total_after_router_selection_is_not_a_router_only_question():
    async def exercise():
        rules = Consultation(FixtureQuoteAdapter())
        state = SessionState(context=SessionContext(session_id='router-total', channel='web'))
        await rules.respond(state, 'улица Пушкина дом 1 корпус 5')
        await rules.respond(state, 'Какие есть роутеры?')
        await rules.respond(state, 'Давайте первый вариант.')
        question = 'Ну ладно, а сколько будет стоить всё?'
        answer = await rules.respond(state, question)
        assert 'При подключении — 1700 рублей' in answer and '1700 рублей в месяц' in answer
        assert '2400 рублей разово' in answer and 'всего 4100 рублей' in answer
        state.active_offer.mandatory_charges['equipment'] = 2400
        included = await rules.respond(state, question)
        assert 'При подключении — 4100 рублей' in included and '1700 рублей в месяц' in included
        assert 'разово' not in included and '6500' not in included
        await rules.reads.close()

    asyncio.run(exercise())
