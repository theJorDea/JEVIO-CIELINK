"""Deterministic training consultation with a local address directory."""
import json
import re
from datetime import date
from decimal import Decimal

from .addresses import DemoAddressAdapter, DemoCoverageAdapter
from .domain import Address, NoOffer, QuoteRequest, SessionState
from . import gemini
from .equipment_intent import classify_equipment
from .quotes import FIXTURES, FixtureQuoteAdapter
from .speculation import Mode, SpeculationCoordinator


_GREETING = re.compile(r'^(алло|алё|здравствуйте|привет|добрый (?:день|вечер)|доброе утро)\b[,.!?\s]*', re.IGNORECASE)
_CONNECTION_REQUEST = re.compile(
    r'^(?:(?:я\s+)?хочу|мне нужно|мне надо|можно|можете(?:\s+ли)?|можно\s+ли)\s+'
    r'(?:у вас\s+)?(?:подключить|провести|оформить)\s+(?:себе\s+)?(?:домашний\s+)?интернет\b'
)
_BUDGET = re.compile(
    r'(?:(сегодня|сейчас|при подключении|ежемесячно)\s+)?'
    r'(?:(?:мой\s+)?бюджет\s+|(?:я\s+)?(?:хочу|могу|готов(?:а)?)\s+(?:платить|внести|потратить)\s+)?'
    r'(?:до|не больше|не дороже|максимум)\s+(\d+)\s*(?:₽|рублей|рубля|руб\.?)?'
    r'\s*(после скидки|после акции|в месяц|сегодня|при подключении)?'
)
_PRICE_QUESTION = re.compile(
    r'\bсколько\s+(?:я\s+)?(?:буду\s+)?(?:стоит|будет|нужно|надо|платить|внести|обойдётся|обойдется|это|в месяц)\b'
    r'|^(?:а\s+)?(?:какая цена|по цене|можно подешевле|есть что-нибудь дешевле)\b'
)
_CONNECTION_INTENT = re.compile(r'\b(?:подключить|подключиться|провести)\b')
_ADDRESS_WORDS = re.compile(r'\b(?:улица|улице|дом|корпус|дробь|строение|литера)\b')
_ADDRESS_CORRECTION = re.compile(
    r'\s*[.,]?\s*(?:нет\s*[,.]?\s*)?(?:точнее|вернее|поправка)\b[ ,.]*'
    r'(?P<component>(?:корпус|строение|литера|дробь)\s+\w+)$'
)
_EQUIPMENT = json.loads((FIXTURES / 'equipment.json').read_text(encoding='utf-8'))
_ROUTER = re.compile(r'\bроутер\w*\b')
_ROUTER_FOLLOWUP = re.compile(
    r'\b(?:он|его|него|вариант\w*|модел\w*|разниц\w*|отлича\w*|гаранти\w*|доставк\w*|рассрочк\w*)\b'
)
_EQUIPMENT_OPTION = re.compile(r'\b(перв\w*|втор\w*|[12]|базов\w*|двухдиапазон\w*)\b')
_EQUIPMENT_ACTION = re.compile(
    r'\b(?:давайте|выберу|беру|возьму|возьм[её]м|подойд[её]т|оставьте|запишите|хочу|нужен|мне бы|остановимся)\b'
)
_EQUIPMENT_QUESTION = re.compile(r'\b(?:сколько|стоит|поч[её]м|цена|цену|разниц\w*|чем|лучше)\b')


class Consultation:
    def __init__(self, quotes: FixtureQuoteAdapter, mode: Mode = 'sequential'):
        self.addresses = DemoAddressAdapter(quotes.demo_store)
        self.coverage = DemoCoverageAdapter(quotes.demo_store)
        self.quotes = quotes
        self.base = ''
        self.coverage_status = None
        self.reads = SpeculationCoordinator(self.coverage, quotes, mode=mode)
        self.prepared = None
        self.proposed_action = None
        self.awaiting_budget = None
        self.pending_street = None
        self.pending_component = ''
        self.pending_house = None
        self.connection_requested = False
        self.lookup_trace = {}
        self.unrecognized_turns = 0
        self.discussing_equipment = False
        self.selected_equipment = None
        self.presented_equipment = ()

    @staticmethod
    def _parse(state: SessionState, text: str):
        phrase = text.strip(' .!?').casefold()
        phrase = re.sub(r'^(?:да|так|ну|здравствуйте|алло)[,.!\s]+', '', phrase)
        phrase = _CONNECTION_REQUEST.sub('', phrase).lstrip(' ,.')
        phrase = re.sub(r'^номер дома\b', 'дом', phrase)
        phrase = re.sub(r'^(?:по адресу|мой адрес|адрес такой|я живу)\b[ ,.:]*', '', phrase)
        # Preserve a spoken component correction before splitting comma-separated clauses.
        phrase = re.sub(r'\bой[ ,.]+нет[ ,.]+(?=(?:корпус|строение|литера|дробь)\b)', 'точнее ', phrase)
        # Keep domain values separate from speech revisions; never erase budget on an address correction.
        parts = re.split(r'[,;]|\s+и\s+|\s+(?=(?:мой\s+)?бюджет\b)', phrase)
        address_parts = []
        has_budget = False
        missing_budget = None
        for part in parts:
            part = part.strip()
            part = re.sub(r'\b(до|не больше|максимум) двух тысяч\b', r'\1 2000', part)
            budget = _BUDGET.fullmatch(part)
            if budget:
                field = ('budget_today' if budget[1] in ('сегодня', 'сейчас', 'при подключении')
                         or budget[3] in ('сегодня', 'при подключении') else 'monthly_budget')
                value = Decimal(budget[2])
                if getattr(state, field) != value:
                    setattr(state, field, value)
                    state.revisions[field] = state.revisions.get(field, 0) + 1
                has_budget = True
            elif re.match(r'^(?:мой\s+)?бюджет\b', part):
                missing_budget = ('при подключении' if re.search(r'сегодня|при подключении', part) else 'в месяц')
            elif part in ('нет', 'ой', 'стоп'):
                continue
            elif part:
                address_parts.append(part)
        return ' '.join(address_parts), has_budget, missing_budget

    def _address_query(self, phrase):
        phrase = re.sub(r'^(?:нет|стоп|ой)\s+', '', phrase)
        phrase = re.sub(r'\b(первый|второй)\s+корпус\b|(?<=\d\s)(\d+)\s+корпус$',
                        lambda match: f'корпус {match[1] or match[2]}', phrase)
        if correction := _ADDRESS_CORRECTION.search(phrase):
            street, detail = self.addresses.split_street(phrase[:correction.start()].rstrip(' ,.'))
            house, _ = self.addresses.split_house(detail)
            if house:
                phrase = ' '.join(part for part in (street, f'дом {house}', correction['component']) if part)
        if self.pending_house:
            street, detail = self.addresses.split_street(phrase)
            if street and not detail:
                return f'{street} {self.pending_house}'
            if re.fullmatch(r'(?:корпус|строение|литера|дробь)\s+\w+', phrase):
                house, _ = self.addresses.split_house(self.pending_house)
                return f'дом {house} {phrase}'
        if re.match(r'^(?:корпус|литера|строение|дробь)\b', phrase) and self.base:
            return f'{self.base} {phrase}'
        if self.pending_street:
            house, suffix = self.addresses.split_house(phrase)
            if house:
                component = self.pending_component if not suffix else ''
                return f'{self.pending_street} {phrase} {component}'.strip()
        return phrase

    async def _resolve(self, phrase):
        return await self.addresses.resolve_address(self._address_query(phrase))

    def _request(self, state, address):
        return QuoteRequest(house_id=address.house_id, on_date=state.on_date or date.fromisoformat(self.quotes.version), catalog_version=self.quotes.version, budget_today=state.budget_today, monthly_budget=state.monthly_budget, needs=state.needs)

    async def partial(self, state, text):
        if self.reads.mode == 'sequential':
            return
        draft = state.model_copy(deep=True)
        phrase, _, _ = self._parse(draft, text)
        result = await self._resolve(phrase) if phrase else None
        self.prepared = (phrase, self.base, result)
        address = result.address if result and result.status == 'resolved' else (draft.address if not phrase else None)
        self.reads.partial(self._request(draft, address) if address else None)

    async def equipment_reply(self, state, phrase):
        phrase = phrase.casefold().strip()
        equipment_named = _ROUTER.search(phrase)
        if not self.discussing_equipment:
            self.presented_equipment = ()
            if equipment_named:  # New equipment enquiry; never reuse a stale selection.
                self.selected_equipment = None
        intent = await classify_equipment(phrase) if self.presented_equipment else 'unknown'
        if self.discussing_equipment and re.fullmatch(r'я понял[аи]?[.! ]*', phrase):
            self.lookup_trace['equipment'] = {'intent': 'acknowledgement'}
            return 'Хорошо, я слушаю.'
        if (self.presented_equipment and re.fullmatch(
                r'(?:а )?(?:это )?(?:разово|разовая оплата|разовый плат[её]ж)(?: или (?:в месяц|ежемесячно))?[?.! ]*', phrase)):
            model = self.selected_equipment
            self.proposed_action = None
            self.lookup_trace['equipment'] = {'source': 'fixtures/equipment.json', 'selected': model}
            if model is None:
                return 'За любой из этих роутеров платите один раз.'
            return f'За этот роутер — {model["price_rub"]} рублей один раз.'
        internet_bill = re.search(r'\b(?:тариф\w*|абонентск\w*|интернет\w*|в месяц|подключени\w*)\b', phrase)
        followup = self.discussing_equipment and not internet_bill and (
            _ROUTER_FOLLOWUP.search(phrase) or _PRICE_QUESTION.search(phrase)
            or intent != 'unknown' or _EQUIPMENT_OPTION.search(phrase) or _EQUIPMENT_ACTION.search(phrase)
        )
        if not equipment_named and not followup:
            if internet_bill:
                self.discussing_equipment = False
            return None
        self.discussing_equipment = True
        self.proposed_action = None
        if re.search(r'гаранти|доставк|рассрочк|аренд|наличи|совместим|разъ[её]м|keenetic|tp[ -]?link|asus|xiaomi|кин[еэ]тик', phrase):
            # This handoff concerns the question, not an earlier model selection.
            self.selected_equipment = None
            self.proposed_action = 'handoff'
            return 'В моём справочнике такой информации о роутере нет. Передать ваш вопрос специалисту?'
        if (re.search(r'\b(?:свой|собственный)\b', phrase) and state.active_offer
                and 'собственный роутер' in state.active_offer.conditions):
            return 'Да, по этому предложению можно использовать свой роутер. Если назовёте модель, вопрос о совместимости можно передать специалисту.'
        if (re.search(r'входит|включ[её]н', phrase) and state.active_offer
                and 'собственный роутер' in state.active_offer.conditions):
            return 'Для этого предложения нужен свой роутер. Я могу назвать цены на оборудование отдельно.'
        self.lookup_trace['equipment'] = {'source': 'fixtures/equipment.json', 'result': _EQUIPMENT}
        choices = {0 if m[0].startswith(('перв', '1', 'базов')) else 1
                   for m in _EQUIPMENT_OPTION.finditer(phrase)}
        self.lookup_trace['equipment']['intent'] = intent
        index = {'first': 0, 'second': 1}.get(intent)
        if index is not None and index < len(self.presented_equipment):
            model = self.presented_equipment[index]
        else:
            model = next((row for row in self.presented_equipment if row['id'] == intent), None)
        if model is not None:
            self.selected_equipment = model
            self.lookup_trace['equipment']['selected'] = model
            self.proposed_action = 'handoff'
            return (f'Выбрали {model["name"]} за {model["price_rub"]} рублей. '
                    'Передать специалисту запрос по этой модели?')
        if (self.presented_equipment and len(choices) == 1 and _EQUIPMENT_QUESTION.search(phrase)
                and not re.search(r'разниц|чем|лучше', phrase)):
            model = self.presented_equipment[next(iter(choices))]
            return f'{model["name"]} стоит {model["price_rub"]} рублей разово.'
        if self.selected_equipment and _PRICE_QUESTION.search(phrase):
            model = self.selected_equipment
            return f'Выбранный {model["name"]} стоит {model["price_rub"]} рублей разово.'
        if (self.presented_equipment and intent == 'unknown' and _EQUIPMENT_ACTION.search(phrase)
                and '?' not in phrase and not (_EQUIPMENT_QUESTION.search(phrase) or _PRICE_QUESTION.search(phrase))
                and not re.search(r'отлича|расскаж|узнать|входит|включ[её]н', phrase)):
            suggestion = await gemini.classify_equipment_intent(phrase)
            ordinal = {'first': 'первый', 'second': 'второй'}.get(suggestion)
            if ordinal:
                return f'Вы имеете в виду {ordinal} вариант? Скажите: «беру {ordinal}».'
        self.presented_equipment = tuple(_EQUIPMENT)
        if re.search(r'разниц|отлича', phrase):
            return '. '.join(f'{row["name"]} {row["description"]}' for row in _EQUIPMENT) + '.'
        offers = ', '.join(f'{row["name"]} — {row["price_rub"]} рублей' for row in _EQUIPMENT)
        return f'Есть два роутера: {offers}. За роутер платите один раз.'

    async def respond(self, state: SessionState, text: str) -> str:
        equipment_text = text  # Keep punctuation and spoken self-corrections for equipment selection.
        text = text.strip(' .!?')
        greeting = _GREETING.match(text)
        while match := _GREETING.match(text):
            text = text[match.end():]
        spoken = text.casefold()
        if self.awaiting_budget and re.fullmatch(r'\d+(?: рублей| рубля| руб)?', text, re.IGNORECASE):
            text = f'до {text} {self.awaiting_budget}'
        phrase, has_budget, missing_budget = self._parse(state, text)
        if has_budget:
            self.awaiting_budget = None
        missing_budget = missing_budget or self.awaiting_budget
        prepared, self.prepared = self.prepared, None
        self.proposed_action = None  # Only an actual invitation on this turn is confirmable.
        self.lookup_trace = {}
        if phrase in {'как дела', 'как у тебя дела', 'как у вас дела'}:
            return 'Всё хорошо, спасибо! Чем могу помочь с интернетом?'
        if phrase in {'меня слышно', 'вы меня слышите', 'ты меня слышишь', 'слышно меня'}:
            return 'Да, я вас слышу. Чем могу помочь с интернетом?'
        if greeting and not phrase and not has_budget:
            return ('Да, я вас слышу. Чем могу помочь?' if greeting[1].casefold() in {'алло', 'алё'}
                    else 'Здравствуйте! Чем могу помочь?')
        if phrase in {'окей', 'okay', 'ok', 'хорошо'} and not has_budget:
            return 'Хорошо, я слушаю.'
        if phrase in {'ой', 'о', 'oh', 'ага'} and not has_budget:
            return 'Да, слушаю вас.'
        if (not state.address and _CONNECTION_INTENT.search(spoken)
                and re.search(r'\bинтернет\w*\b', spoken) and not _ADDRESS_WORDS.search(spoken)
                and not re.search(r'\bне\s+(?:хоч\w*|нуж\w*|подключить|провести)', spoken)):
            self.connection_requested = True
            welcome = 'Здравствуйте! ' if greeting else ''
            if self.pending_house:
                return f'{welcome}Проверю подключение. Дом и корпус я записал, подскажите улицу.'
            return f'{welcome}Проверю, можно ли подключить интернет у вас дома. Назовите улицу и номер дома.'
        if phrase in {'интернет', 'интернета', 'про интернет'} and not state.address and not self.pending_house:
            if self.connection_requested:
                return 'Да, про домашний интернет. Чтобы проверить подключение, подскажите адрес.'
            return 'Понял, речь про интернет. Хотите подключить его дома или нужна помощь с текущим подключением?'
        if re.search(r'\b(?:акци\w*|скидк\w*)\b', phrase) and not re.search(r'\bпосле (?:акции|скидки)\b', phrase):
            if not state.address:
                if self.pending_street:
                    return ('Помогу с акциями. Улицу уже записал. Подскажите номер дома, '
                            'и я проверю, какая скидка есть и сколько будет стоить потом.')
                return ('Помогу разобраться с акциями. Проверю, какие действуют по вашему адресу, '
                        'сколько длится скидка и какая цена будет потом. Назовите улицу и номер дома.')
            offer = state.active_offer
            if offer and offer.periods[0].months:
                first, last = offer.periods[0], offer.periods[-1]
                return (f'{first.monthly_price} рублей в месяц в течение {first.months} месяцев. '
                        f'Затем {last.monthly_price} рублей в месяц.')
            if self.coverage_status == 'available':
                return 'По вашему адресу доступен тариф без акции. Могу рассказать, сколько стоит подключение.'
            return 'По этому адресу ещё нужно проверить подключение. После проверки сможем уточнить акции.'
        if re.search(r'\bчто (?:вы|ты) (?:можете|умеете|можешь|умеешь)\b', phrase):
            return 'Могу подсказать условия подключения, объяснить цену и помочь оставить заявку. Что вас интересует?'
        if phrase == 'хочу узнать тарифы' and not state.address:
            return 'Подскажу. По какому адресу вы хотите подключить интернет?'
        if not state.address and re.search(r'\b(?:нужен|нужна|хочу) (?:домашний )?интернет\b|\bинтернет (?:домой|в квартиру|для работы)\b', phrase):
            if not re.search(r'\b(?:улица|дом|корпус|учебная|учебной)\b', phrase):
                return 'Конечно. Сначала проверю, что есть по вашему адресу. Назовите улицу и номер дома.'
        if phrase in {'второй', 'нет второй'} and self.base and not self.discussing_equipment:
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
        total_question = (state.address is not None and _PRICE_QUESTION.search(phrase)
                          and re.search(r'\b(?:вс[её]|всего|итого|целиком)\b', phrase))
        if (not total_question and not re.search(r'\b(?:корпус|строение|литера|дробь|улица|дом\s+\d)\b', phrase)
                and (equipment := await self.equipment_reply(state, equipment_text))):
            return equipment
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
        if _PRICE_QUESTION.search(phrase) and not re.search(r'\b(?:корпус|дробь|строение|литера|дом)\s+\d', phrase):
            if not state.address:
                return 'Цена зависит от адреса. Назовите улицу, дом и корпус, если он есть.'
            if self.coverage_status == 'unavailable' and not state.pending_clarification:
                return f'{self.describe(state.address)}: мы пока не подключаем этот дом, поэтому тариф назвать не могу.'
            if (not state.active_offer or state.active_offer.house_id != state.address.house_id
                    or state.pending_clarification):
                return f'{self.describe(state.address)}: по этому дому пока нет подтверждённой цены.'
            if re.search(r'дешевле|подешевле', phrase):
                return 'Какую сумму хотите платить в месяц? Посмотрю, что есть в этом бюджете.'
            offer = state.active_offer
            fees = sum(offer.mandatory_charges.values())
            if total_question:
                total = offer.advance + fees
                periods = ', затем '.join(f'{p.monthly_price} рублей в месяц' +
                    (f' первые {p.months} месяцев' if p.months else '') for p in offer.periods)
                answer = f'При подключении — {total} рублей, из них {offer.advance} рублей на счёт. Тариф — {periods}.'
                model = self.selected_equipment
                if model and not any(offer.mandatory_charges.get(k) for k in ('equipment', 'router')):
                    answer += (f' Выбранный роутер отдельно — {model["price_rub"]} рублей разово. '
                               f'Если берёте его, при подключении всего {total + model["price_rub"]} рублей.')
                return answer
            if re.search(r'подключени|сегодня|внести|сразу', phrase):
                return f'При подключении нужно внести {offer.advance + fees} рублей.'
            price = offer.periods[-1].monthly_price
            return (f'После акции цена {price} рублей в месяц.' if offer.periods[0].months
                    else f'{price} рублей в месяц.')
        if state.pending_clarification and not phrase:
            return 'Уточните корпус, строение или дробь и номер.'
        if phrase:
            query = self._address_query(phrase)
            street, detail = self.addresses.split_street(query)
            looks_like_address = street is not None or bool(re.search(
                r'^(?:(?:нет|ой|стоп)\s+)?(?:\d|восемнадцать|улица\b|дом\b|корпус\b|строение\b|литера\b|дробь\b)|\bдом\s+\d+', phrase
            ))
            if not looks_like_address:
                if self.pending_house:
                    return 'Дом и корпус я записал. Как называется улица?'
                if self.pending_street:
                    return f'Улицу помню — {self.pending_street}. Подскажите номер дома?'
                if state.pending_clarification:
                    return 'Подскажите, какой у дома корпус, строение или дробь?'
                self.unrecognized_turns += 1
                if self.unrecognized_turns == 1:
                    return 'Не совсем понял вопрос. Можете сказать чуть иначе?'
                return ('Что хотите узнать по этому адресу — цену, скорость или условия подключения?' if state.address
                        else 'Вы хотите подключить интернет или у вас вопрос по уже подключённому?')
            self.unrecognized_turns = 0
            self.discussing_equipment = False
            result = prepared[2] if prepared and prepared[:2] == (phrase, self.base) else await self._resolve(phrase)
            self.lookup_trace['address'] = {'query': query, 'result': result.model_dump(mode='json')}
            self.pending_street = None
            self.pending_component = ''
            self.pending_house = None
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
                return f'Для адреса {self.base} есть несколько вариантов. Какой корпус, строение или дробь указать?'
            if result.status != 'resolved':
                if result.status == 'error':
                    return 'Справочник сейчас не отвечает. Давайте попробуем ещё раз.'
                house, suffix = self.addresses.split_house(detail)
                if not street and house:
                    self.pending_house = f'дом {house}{suffix}'
                    return f'Записал: {self.pending_house}. Как называется улица?'
                self.pending_street = street
                if street and house is None:
                    component = re.search(r'\b(?:корпус|строение|литера|дробь)\s+[\w]+\s*$', detail)
                    self.pending_component = component[0] if component else ''
                    heard = f'{street}, {self.pending_component}' if self.pending_component else street
                    return f'Я услышал: {heard}. Какой номер дома?'
                return ('Не удалось найти этот дом. Уточните номер дома и корпус, пожалуйста.' if street
                        else 'Не удалось найти адрес. Можете повторить название улицы по буквам или назвать её целиком?')
            state.address = result.address
            self.base = f'{result.address.street} {result.address.house}'
        elif not has_budget and not missing_budget:
            if state.address:
                return 'Адрес у меня есть. Вас интересует цена, скорость или условия подключения?'
            return 'Подскажу. По какому адресу вы хотите подключить интернет?'
        if missing_budget:
            self.awaiting_budget = missing_budget
            state.active_offer = None
            return f'Назовите сумму ещё раз. Сколько готовы платить {missing_budget}?'
        if not state.address:
            return 'Бюджет учтён. Назовите адрес подключения.'
        address = state.address
        request = self._request(state, address)
        coverage, options = await self.reads.final(request)
        self.lookup_trace['coverage'] = {'house_id': address.house_id, 'status': coverage.status}
        if options is not None:
            self.lookup_trace['quote'] = {'request': request.model_dump(mode='json'),
                                          'result': options.model_dump(mode='json')}
        self.coverage_status = coverage.status
        description = self.describe(address)
        if self.coverage_status == 'unknown':
            self.proposed_action = 'coverage_request'
            return f'{description}. По этому дому пока нет данных о подключении. Отправить запрос на проверку дома?'
        if self.coverage_status == 'unavailable':
            return f'{description}: пока не можем подключить этот дом. Заявку не создаю.'
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
        tariff = self.quotes.offers[offer.offer_id]
        speeds = tariff.get('speed_options_mbps', [])
        spoken_tariff = f'вариант со скоростью до {max(speeds)} мегабит в секунду' if speeds else tariff['name']
        fees = sum(offer.mandatory_charges.values())
        if fees:
            upfront = (f'При подключении нужно внести {offer.advance + fees} рублей. '
                       f'Из них {offer.advance} рублей пойдут на счёт. Обязательные доплаты — {fees} рублей. ')
        else:
            upfront = f'При подключении нужно внести {offer.advance} рублей на счёт. Обязательных доплат нет. '
        prices = '. Затем '.join(f'{period.monthly_price} рублей в месяц' +
                                 (f' в течение {period.months} месяцев' if period.months else '') for period in offer.periods)
        equipment = ('Для этого варианта понадобится свой роутер. ' if 'собственный роутер' in offer.conditions
                     else f'{offer.conditions} ' if offer.conditions else '')
        return (f'Проверил адрес: {description}. Доступен {spoken_tariff}. '
                f'{upfront}Далее {prices}. {equipment}Оформить заявку?')

    @staticmethod
    def describe(address: Address) -> str:
        street = address.street[0].upper() + address.street[1:]
        parts = [('корпус', address.corpus), ('дробь', address.fraction),
                 ('строение', address.structure), ('литера', address.letter)]
        return ', '.join([street, f'дом {address.house}'] + [f'{label} {value}' for label, value in parts if value])
