import asyncio
from uuid import uuid4
import pytest
from cielink.domain import SessionState, SessionContext
from cielink.consultation import Consultation
from cielink.consent import ConfirmedDialogue
from cielink.quotes import FixtureQuoteAdapter
from cielink.crm import OperationStore
from cielink.review import _audit_delivery


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
    assert all(s in terms for s in ('внести 1700 рублей', 'доплат нет', '1700 рублей в месяц', 'свой роутер'))
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


def test_speaks_naturally_and_audits_the_same_quote(case):
    dialogue, say, store = case
    assert say('Алло.').startswith('Да, я вас слышу.')
    assert say('Как у тебя дела?').startswith('Всё хорошо, спасибо!')
    assert say('Okay.') == 'Хорошо, я слушаю.'
    assert dialogue.proposal is None

    reply = say('Здравствуйте, улица Учебная, дом 18, корпус первый.')
    proposal = dialogue.proposal
    assert proposal.kind == 'order' and store.lookup_operation(proposal.key) is None
    findings = list(_audit_delivery(reply, proposal.terms['offer'], None))
    assert len(findings) == 3
    assert all(code == 'price_match' for code, _ in findings)


def test_conversational_address_budget_and_price_questions(case):
    dialogue, say, store = case
    assert 'Назовите улицу' in say('Мне нужен домашний интернет для работы.')
    say('Здравствуйте, я хочу провести домашний интернет на Учебной, дом 18, первый корпус, бюджет не больше 2000 в месяц.')
    first = dialogue.proposal
    assert first.terms['address']['house_id'] == 'training-house-a'
    assert first.terms['monthly_budget'] == '2000'
    dialogue.delivered()
    assert say('А сколько нужно внести сразу?') == 'При подключении нужно внести 1700 рублей.'
    assert say('А сколько я буду платить?') == '1700 рублей в месяц.'
    assert store.lookup_operation(first.key) is None
    assert 'Отправить запрос на проверку' in say('Стоп, корпус второй.')
    assert dialogue.proposal.terms['monthly_budget'] == '2000'


def test_recovers_when_recognition_drops_the_budget_amount(case):
    dialogue, say, _ = case
    reply = say('Хочу провести интернет на Учебной улице, дом 18, 1 корпус, бюджет не больше рублей в месяц.')
    assert 'Назовите сумму' in reply and dialogue.proposal is None
    assert '1700 рублей в месяц' in say('2000 рублей.')
    assert dialogue.proposal.terms['address']['house_id'] == 'training-house-a'
    assert dialogue.proposal.terms['monthly_budget'] == '2000'


def test_replays_greeting_promo_question_and_unheard_house(case):
    dialogue, say, store = case
    assert 'слышу' in say('Алло, здравствуйте.')
    assert 'чуть иначе' in say('Ну, чтобы ещё чё-то делать.')
    assert 'я слушаю' in say('Okay.')
    clarification = say('Улица Пушкина, дом Колотушкина. Подскажите, пожалуйста, корпус 13.')
    assert 'улица Пушкина, корпус 13' in clarification and 'Какой номер дома?' in clarification
    assert 'Улицу уже записал' in say('А подскажите, что вы можете сделать? Мне нужно с акциями разобраться.')
    assert '950 рублей' in say('Номер дома 12.')
    proposal = dialogue.proposal
    assert proposal.terms['address']['house_id'] == 'demo-pushkina-12-k13'
    assert '1450 рублей' in say('А после акции сколько?')
    assert store.lookup_operation(proposal.key) is None


def test_polite_connection_request_and_correction_before_street(case):
    dialogue, say, store = case
    assert say('Здравствуйте, я хотел бы узнать, можно ли мне интернет подключить домой?').startswith('Здравствуйте! Проверю')
    assert 'Чтобы проверить подключение' in say('Интернета.')
    answer = say('Дом 1, корпус 3. Нет, точнее, корпус 5.')
    assert 'дом 1 корпус 5' in answer and 'Как называется улица?' in answer
    assert dialogue.proposal is None

    answer = say('Улица Пушкина.')
    assert 'корпус 5' in answer and '1700 рублей' in answer
    assert dialogue.proposal.terms['address']['house_id'] == 'demo-pushkina-1-k5'
    assert store.lookup_operation(dialogue.proposal.key) is None


def test_equipment_followups_use_the_local_catalog_and_unknowns_go_to_a_specialist(case):
    dialogue, say, store = case
    say('Учебная улица, дом 18, корпус 1.')
    for question in ('А сколько стоит роутер?', 'А он один или есть несколько вариантов?', 'Цену роутера.'):
        answer = say(question)
        assert '2400' in answer and '3900' in answer and '1700' not in answer
        assert dialogue.proposal is None
    assert say('А интернет сколько стоит в месяц?') == '1700 рублей в месяц.'
    question = 'А есть гарантия на роутер?'
    assert 'Передать ваш вопрос специалисту?' in say(question)
    proposal = dialogue.proposal
    assert store.lookup_operation(proposal.key) is None
    dialogue.delivered()
    say('Да, передайте специалисту.')
    saved = store.lookup_operation(proposal.key)
    assert saved.operation_type == 'handoff' and saved.confirmed_terms['question'] == question


def test_equipment_choice_advances_the_conversation_and_requires_delivery(case):
    dialogue, say, store = case
    assert '3900' in say('Мне надо поставить роутер домой.')
    assert '2400' in say('А первый сколько стоит?') and dialogue.proposal is None
    answer = say('Я понял, давайте первый вариант.')
    assert 'Выбрали Базовый' in answer and '2400' in answer and '3900' not in answer
    proposal = dialogue.proposal
    assert say('да') == answer and store.lookup_operation(proposal.key) is None
    dialogue.delivered()
    assert 'Номер' in say('да')
    saved = store.lookup_operation(proposal.key)
    assert saved.operation_type == 'handoff' and saved.confirmed_terms['equipment']['id'] == 'basic'


def test_equipment_fallback_uses_model_label_and_catalog_price(case, monkeypatch):
    dialogue, say, _ = case

    async def classify(phrase):
        assert 'тот' in phrase
        return 'second'

    monkeypatch.setattr('cielink.consultation.classify_equipment', classify)
    say('Какие есть роутеры?')
    answer = say('Тогда мне бы тот, что ловит на двух частотах.')
    assert 'Двухдиапазонный' in answer and '3900' in answer
    assert dialogue.proposal.terms['equipment']['id'] == 'dual-band'


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
