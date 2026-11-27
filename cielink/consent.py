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
    'order': {'да подключайте', 'подключайте', 'да оформляйте', 'да оформляйте заявку на подключение'},
    'coverage_request': {'да проверьте адрес', 'проверьте адрес', 'да отправьте на проверку'},
    'handoff': {'да передайте специалисту', 'передайте специалисту'},
}


class ConfirmedDialogue:
    def __init__(self, consultation: Consultation, store: OperationStore):
        self.consultation = consultation
        self.store = store
        self.proposal = None
        self.last_order = None  # (quote inputs, verified offer, full prompt), never delivery state.
        self.receipt = None
        self.finished = False

    def delivered(self):
        if self.proposal:
            self.proposal.delivered = True

    @staticmethod
    def _without_proposal(state):
        if not state.address:
            return 'Заявку не создаю. Назовите адрес подключения.'
        if state.pending_clarification:
            return 'Уточните обозначение дома. Заявку не создаю.'
        if 'роутер' in state.needs:
            return 'Условия подключения без собственного роутера не подтверждены. Заявку не создаю.'
        return 'Адрес уже известен. Для заявки нужно подтвердить полные условия.'

    async def respond(self, state: SessionState, text: str) -> str:
        phrase = ' '.join(text.casefold().strip(' .!?').replace(',', ' ').split())
        if self.receipt:
            return f'Операция уже сохранена. Номер {self.receipt.spoken_reference}.'
        if self.finished:
            return 'Обращение завершено без заявки.'
        clarifying = state.pending_clarification is not None and phrase in {'да', 'нет'}
        if not clarifying and phrase in {'нет', 'не надо', 'не оформляйте', 'отказываюсь', 'не хочу', 'подумаю', 'я подумаю', 'мне надо подумать'}:
            self.proposal = None
            self.finished = True
            return 'Хорошо, ничего не оформляю.'
        p = self.proposal
        if not clarifying and (phrase == 'да' or any(phrase in words for words in _ALLOWED.values())):
            if not p:
                if (phrase in _ALLOWED['order'] and state.address and state.active_offer
                        and not state.pending_clarification and self.consultation.coverage_status == 'available'
                        and self.last_order):
                    request, offer, prompt = self.last_order
                    if offer == state.active_offer and request == self.consultation._request(state, state.address):
                        return self._propose(state, 'order', prompt)
                return self._without_proposal(state)
            if not p.delivered or phrase not in _ALLOWED[p.kind]:
                p.delivered = False
                return p.prompt
            args = dict(session_id=state.context.session_id, operation_key=p.key,
                        operation_type=p.kind, confirmed_terms=p.terms)
            try:
                self.receipt = await asyncio.to_thread(self.store.record_operation, **args)
            except TimeoutError:
                self.receipt = await asyncio.to_thread(self.store.recover_operation, **args)
            return f'Сохранено. Номер {self.receipt.spoken_reference}.'
        if phrase in {'угу', 'да я слышу', 'я слышу', 'слышу'}:
            if p:
                p.delivered = False
                return p.prompt
            return self._without_proposal(state)
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
            kind = self.consultation.proposed_action
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
