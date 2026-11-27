"""Explicit scope-bound, delivered-offer consent; shared by web and SIP."""
import asyncio
from dataclasses import dataclass
from uuid import uuid4

from .consultation import Consultation
from .crm import OperationStore
from .domain import SessionState


@dataclass
class Proposal:
    kind: str
    key: str
    terms: dict
    prompt: str
    delivered: bool = False


_ALLOWED = {
    'order': {'да подключайте', 'подключайте', 'да оформляйте заявку на подключение'},
    'coverage_request': {'да проверьте адрес', 'проверьте адрес', 'да отправьте на проверку'},
    'handoff': {'да передайте специалисту', 'передайте специалисту'},
}


class ConfirmedDialogue:
    def __init__(self, consultation: Consultation, store: OperationStore):
        self.consultation = consultation
        self.store = store
        self.proposal = None
        self.receipt = None
        self.finished = False

    def delivered(self):
        if self.proposal:
            self.proposal.delivered = True

    async def respond(self, state: SessionState, text: str) -> str:
        phrase = ' '.join(text.casefold().strip(' .!?').replace(',', ' ').split())
        if self.receipt:
            return f'Операция уже сохранена. Номер {self.receipt.receipt_id}.'
        if self.finished:
            return 'Обращение завершено без заявки.'
        if phrase in {'нет', 'не надо', 'не оформляйте', 'отказываюсь', 'не хочу', 'подумаю', 'я подумаю', 'мне надо подумать'}:
            self.proposal = None
            self.finished = True
            return 'Хорошо, ничего не оформляю.'
        p = self.proposal
        if phrase == 'да' or any(phrase in words for words in _ALLOWED.values()):
            if not p:
                return 'Сначала уточним адрес и условия. Заявку не создаю.'
            if not p.delivered or (phrase != 'да' and phrase not in _ALLOWED[p.kind]):
                p.delivered = False
                return p.prompt
            args = dict(session_id=state.context.session_id, operation_key=p.key,
                        operation_type=p.kind, confirmed_terms=p.terms)
            try:
                self.receipt = await asyncio.to_thread(self.store.record_operation, **args)
            except TimeoutError:
                self.receipt = await asyncio.to_thread(self.store.recover_operation, **args)
            return f'Сохранено. Номер {self.receipt.receipt_id}.'
        if phrase in {'угу', 'да я слышу', 'я слышу', 'слышу'}:
            if p:
                p.delivered = False
                return p.prompt
            return 'Заявку не создаю. Назовите адрес подключения.'
        self.proposal = None  # revision or new utterance invalidates old confirmation
        if phrase in {'хочу оператора', 'переведите на оператора', 'нужен специалист'}:
            kind, prompt = 'handoff', 'Передать специалисту ваш вопрос и собранные условия?'
        else:
            prompt = await self.consultation.respond(state, text)
            if not state.address:
                return prompt
            status = self.consultation.coverage_status
            if status == 'unavailable':
                self.finished = True
                return prompt
            kind = {'unknown': 'coverage_request', 'error': 'handoff'}.get(status)
            if not kind and state.active_offer:
                kind = 'order'
            if not kind:
                return prompt
        self.proposal = Proposal(kind, uuid4().hex, {
            'address': state.address.model_dump(mode='json') if state.address else None,
            'offer': state.active_offer.model_dump(mode='json') if state.active_offer and kind == 'order' else None,
            'budget_today': str(state.budget_today) if state.budget_today is not None else None,
            'monthly_budget': str(state.monthly_budget) if state.monthly_budget is not None else None,
            'revisions': dict(state.revisions), 'prompt': prompt, 'needs': list(state.needs),
        }, prompt)
        return prompt
