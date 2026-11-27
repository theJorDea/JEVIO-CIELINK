"""Deterministic training consultation; only the fixture street is supported."""
import re
from datetime import date
from decimal import Decimal

from .addresses import FixtureAddressAdapter, FixtureCoverageAdapter
from .domain import Address, NoOffer, QuoteRequest, SessionState
from .quotes import FixtureQuoteAdapter
from .speculation import Mode, SpeculationCoordinator


class Consultation:
    def __init__(self, quotes: FixtureQuoteAdapter, mode: Mode = 'sequential'):
        self.addresses = FixtureAddressAdapter()
        self.coverage = FixtureCoverageAdapter()
        self.quotes = quotes
        self.base = ''
        self.coverage_status = None
        self.reads = SpeculationCoordinator(self.coverage, quotes, mode=mode)
        self.prepared = None
        self.proposed_action = None

    @staticmethod
    def _parse(state: SessionState, text: str):
        phrase = text.strip(' .!?').casefold()
        # Keep domain values separate from speech revisions; never erase budget on an address correction.
        parts = re.split(r'[,;]|\s+и\s+', phrase)
        address_parts = []
        has_budget = False
        for part in parts:
            part = part.strip()
            part = re.sub(r'\bдо двух тысяч\b', 'до 2000', part)
            budget = re.fullmatch(r'(?:(сегодня|сейчас|при подключении|ежемесячно)\s+)?(?:бюджет\s+)?до\s+(\d+)\s*(?:₽|рублей|рубля|руб)?\s*(после скидки|после акции|в месяц|сегодня)?', part)
            if budget:
                field = 'budget_today' if (budget[1] in ('сегодня', 'сейчас', 'при подключении') or budget[3] == 'сегодня') else 'monthly_budget'
                value = Decimal(budget[2])
                if getattr(state, field) != value:
                    setattr(state, field, value)
                    state.revisions[field] = state.revisions.get(field, 0) + 1
                has_budget = True
            elif part in ('нет', 'ой', 'стоп'):
                continue
            elif part:
                address_parts.append(part)
        return ' '.join(address_parts), has_budget

    async def _resolve(self, phrase):
        phrase = re.sub(r'^(?:нет|стоп|ой)\s+', '', phrase)
        if re.match(r'^(?:корпус|литера|строение|дробь)\b', phrase) and self.base:
            phrase = f'{self.base} {phrase}'
        return await self.addresses.resolve_address(phrase)

    def _request(self, state, address):
        return QuoteRequest(house_id=address.house_id, on_date=state.on_date or date.fromisoformat(self.quotes.version), catalog_version=self.quotes.version, budget_today=state.budget_today, monthly_budget=state.monthly_budget, needs=state.needs)

    async def partial(self, state, text):
        if self.reads.mode == 'sequential':
            return
        draft = state.model_copy(deep=True)
        phrase, _ = self._parse(draft, text)
        result = await self._resolve(phrase) if phrase else None
        self.prepared = (phrase, self.base, result)
        address = result.address if result and result.status == 'resolved' else (draft.address if not phrase else None)
        self.reads.partial(self._request(draft, address) if address else None)

    async def respond(self, state: SessionState, text: str) -> str:
        phrase, has_budget = self._parse(state, text)
        prepared, self.prepared = self.prepared, None
        self.proposed_action = None  # Only an actual invitation on this turn is confirmable.
        if phrase in {'второй', 'нет второй'} and self.base:
            state.pending_clarification = 'corpus_2'
            state.active_offer = None
            self.reads.invalidate()
            return f'Вы имеете в виду корпус два по адресу {self.base}?'
        if state.pending_clarification and text.strip(' .!?').casefold() == 'нет':
            state.pending_clarification = 'building_component'
            return 'Тогда уточните обозначение дома: корпус, строение или дробь и номер.'
        if state.pending_clarification == 'corpus_2' and phrase == 'да':
            phrase = 'корпус два'
        if re.search(r'\b(?:нет (?:своего |собственного )?роутера|роутера (?:у меня )?нет)\b', phrase):
            if 'роутер' not in state.needs:
                state.needs.append('роутер')
                state.revisions['needs'] = state.revisions.get('needs', 0) + 1
            state.active_offer = None
            self.reads.invalidate()
            return 'Для этого предложения нужен собственный роутер. Условия предоставления роутера не подтверждены, заявку не оформляю.'
        if (re.search(r'\bпосле (?:акции|скидки)\b', phrase)
                or re.fullmatch(r'(?:а )?сколько (?:будет )?потом', phrase)):
            if self.coverage_status == 'unknown' and state.address and not state.pending_clarification:
                self.proposed_action = 'coverage_request'
                return f'{self.describe(state.address)}: цена не подтверждена. Отправить запрос на проверку дома?'
            offer = state.active_offer
            if not offer or not state.address or offer.house_id != state.address.house_id:
                return 'Для этого дома подтверждённой цены нет.'
            price = offer.periods[-1].monthly_price
            if len(offer.periods) == 1:
                return f'У этого тарифа нет акции: ежемесячная цена {price} рублей.'
            return f'После акции цена {price} рублей в месяц.'
        if state.pending_clarification and not phrase:
            return 'Уточните корпус, строение или дробь и номер.'
        if phrase:
            result = prepared[2] if prepared and prepared[:2] == (phrase, self.base) else await self._resolve(phrase)
            if result.status == 'not_found' and not re.search(
                r'^(?:(?:нет|ой|стоп)\s+)?(?:\d|восемнадцать|учебн|на учебной|улица\b|'
                r'дом\b|корпус\b|строение\b|литера\b|дробь\b)|\bдом\s+\d+', phrase
            ):
                return 'Уточните, пожалуйста, что вы хотите изменить или узнать.'
            state.pending_clarification = None
            if result.status != 'resolved':
                self.reads.invalidate()
            state.active_offer = None
            state.pending_confirmation = None
            state.revisions['address'] = state.revisions.get('address', 0) + 1
            state.address = None
            self.base = ''
            self.coverage_status = None
            if result.status == 'needs_clarification':
                first = result.building_candidates[0]
                self.base = f'{first.street} {first.house}'
                return 'Уточните обозначение дома: например, корпус один, корпус два или дробь два?'
            if result.status != 'resolved':
                return 'Не удалось определить адрес. Назовите улицу, дом и корпус.'
            state.address = result.address
            self.base = f'{result.address.street} {result.address.house}'
        elif not has_budget:
            return ('Уточните, пожалуйста, что вы хотите изменить или узнать.' if state.address
                    else 'По какому адресу нужно подключение?')
        if not state.address:
            return 'Бюджет учтён. Назовите адрес подключения.'
        address = state.address
        coverage, options = await self.reads.final(self._request(state, address))
        self.coverage_status = coverage.status
        description = self.describe(address)
        if self.coverage_status == 'unknown':
            self.proposed_action = 'coverage_request'
            return f'{description}: данных о покрытии нет. Отправить запрос на проверку дома?'
        if self.coverage_status == 'unavailable':
            return f'{description}: по учебным данным подключения нет. Заявку не создаю.'
        if self.coverage_status == 'error':
            self.proposed_action = 'handoff'
            return f'{description}: проверка сейчас недоступна. Передать вопрос специалисту?'
        assert options is not None  # available coverage always joins the matching quote
        if isinstance(options, NoOffer):
            state.active_offer = None
            return options.reason
        offer = options.offers[0]
        state.active_offer = offer
        self.proposed_action = 'order'
        name = self.quotes.offers[offer.offer_id]['name']
        fees = sum(offer.mandatory_charges.values())
        prices = '; затем '.join(f'ежемесячно {p.monthly_price} рублей' +
                                 (f' в течение {p.months} мес.' if p.months else '') for p in offer.periods)
        return (f'{description}: доступен {name}. Сегодня внести {offer.advance + fees} рублей: '
                f'аванс {offer.advance}, обязательные доплаты {fees}. Далее {prices}. '
                f'{offer.conditions} Оформить заявку?')

    @staticmethod
    def describe(address: Address) -> str:
        return ' '.join([address.street, 'дом', address.house] + [f'{label} {value}' for label, value in [('корпус', address.corpus), ('дробь', address.fraction), ('строение', address.structure), ('литера', address.letter)] if value])
