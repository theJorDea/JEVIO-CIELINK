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

    @staticmethod
    def _parse(state: SessionState, text: str):
        phrase = text.strip(' .!?').casefold()
        # Keep domain values separate from speech revisions; never erase budget on an address correction.
        parts = re.split(r'[,;]|\s+и\s+', phrase)
        address_parts = []
        changed_budget = False
        for part in parts:
            part = part.strip()
            budget = re.fullmatch(r'(?:(сегодня|сейчас|при подключении|ежемесячно)\s+)?(?:бюджет\s+)?до\s+(\d+)\s*(?:₽|рублей|рубля|руб)?\s*(после скидки|после акции|в месяц|сегодня)?', part)
            if budget:
                field = 'budget_today' if (budget[1] in ('сегодня', 'сейчас', 'при подключении') or budget[3] == 'сегодня') else 'monthly_budget'
                value = Decimal(budget[2])
                if getattr(state, field) != value:
                    setattr(state, field, value)
                    state.revisions[field] = state.revisions.get(field, 0) + 1
                    changed_budget = True
            elif part in ('нет', 'ой', 'стоп'):
                continue
            elif part:
                address_parts.append(part)
        return ' '.join(address_parts), changed_budget

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
        phrase, changed_budget = self._parse(state, text)
        prepared, self.prepared = self.prepared, None
        if phrase:
            result = prepared[2] if prepared and prepared[:2] == (phrase, self.base) else await self._resolve(phrase)
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
        elif not changed_budget and not state.address:
            return 'По какому адресу нужно подключение?'
        if not state.address:
            return 'Бюджет учтён. Назовите адрес подключения.'
        address = state.address
        coverage, options = await self.reads.final(self._request(state, address))
        self.coverage_status = coverage.status
        description = self.describe(address)
        if self.coverage_status == 'unknown':
            return f'{description}: данных о покрытии нет. Отправить запрос на проверку дома?'
        if self.coverage_status == 'unavailable':
            return f'{description}: по учебным данным подключения нет. Заявку не создаю.'
        if self.coverage_status == 'error':
            return f'{description}: проверка сейчас недоступна. Передать вопрос специалисту?'
        assert options is not None  # available coverage always joins the matching quote
        if isinstance(options, NoOffer):
            state.active_offer = None
            return options.reason
        offer = options.offers[0]
        state.active_offer = offer
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
