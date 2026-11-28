"""Bound the optional explanation path; everything else stays in local dialogue."""
import re

from . import gemini


_GREETING = r'(?:алло|алл[её]|здравствуйте|привет|добрый (?:день|вечер)|доброе утро)'
_BYE = r'(?:до свидания|до встречи|всего доброго|пока)'
_SOCIAL = re.compile(rf'^(?:{_GREETING}|{_BYE}|спасибо)\b[\s,.!?;:—-]*', re.I)
_PAYMENT = re.compile(r'\b(?:плат[её]ж\w*|оплат\w*|баланс\w*|задолженн\w*|списан\w*|списал\w*|пополн\w*|реквизит\w*|лицев\w* сч[её]т\w*)\b', re.I)
_PRICE = re.compile(r'\b(?:цен\w*|стоимост\w*|стоит|бюджет\w*|платить|внести|скидк\w*|акци\w*|тариф\w*|рубл\w*)\b', re.I)
_CRM = re.compile(r'\b(?:заявк\w*|оператор\w*|специалист\w*|оформ\w*|crm|срм)\b', re.I)

# Match the whole question, never a keyword in a longer turn containing a
# customer identifier, financial question, address or instruction to act.
_TOPICS = (
    (r'(?:wi[ -]?fi|вай[ -]?фай|беспроводн\w+ сет\w*)', 'Wi-Fi связывает устройства с роутером без кабеля, по радиосигналу.'),
    (r'(?:роутер\w*|маршрутизатор\w*)', 'Роутер соединяет домашние устройства в сеть и передаёт данные между ними и интернетом.'),
    (r'(?:пинг\w*|задержк\w*)', 'Пинг показывает, сколько времени сигнал идёт до узла сети и обратно.'),
    (r'(?:мегабит\w*(?: в секунду)?|мбит/с|скорост\w* интернета)', 'Скорость соединения показывает, сколько данных можно передать за секунду.'),
    (r'(?:оптоволок\w*|оптическ\w+ (?:кабел\w*|волокн\w*)|ethernet|проводн\w+ интернет\w*)', 'Кабель передаёт данные между устройствами. В оптоволокне для передачи используют свет.'),
    (r'(?:двухдиапазонн\w+ роутер\w*|(?:диапазон\w* )?(?:2[.,]4|5)(?: ггц)?)', 'У Wi-Fi в диапазоне 2,4 ГГц обычно больше дальность. На 5 ГГц скорость вблизи роутера часто выше.'),
)
_TOPIC = '(?:' + '|'.join(pattern for pattern, _ in _TOPICS) + ')'
_QUESTION = re.compile(r'(?:а )?(?:(?:подскажите|расскажите|объясни(?:те)?)(?:,? пожалуйста)?[, ]+)?'
                       rf'(?:(?:что такое|что значит|как работает|зачем нужен|для чего нужен|расскажи(?:те)? (?:про|о)) (?P<one>{_TOPIC})'
                       rf'|(?:чем отличается|какая разница между|в чем разница между) (?P<left>{_TOPIC}) (?:от|и) (?P<right>{_TOPIC}))')
# Fail closed on the answer too. A blacklist of currencies and CRM verbs
# would still let paraphrased promises through. Only descriptive technical
# vocabulary can be spoken; other valid model answers use the local gloss.
_TECH_WORD = re.compile(
    r'это|и|а|но|в|во|с|со|к|из|от|на|по|за|без|между|через|до|для|чем|'
    r'он|она|они|его|их|или|как|такой|что|котор(?:ый|ая|ое|ые)|обычно|также|'
    r'wi|fi|wifi|вай|фай|ethernet|2|4|5|ггц|мбит|мб|с|мс|'
    r'(?:домашн|беспроводн|проводн|локальн|оптическ|сетев|двухдиапазонн)\w*|'
    r'(?:роутер|маршрутизатор|устройств|интернет|кабел|сигнал|радиоволн|радиосигнал|'
    r'радиосвяз|технологи|данны|пакет|диапазон|частот|скорост|задержк|миллисекунд|'
    r'секунд|мегабит|пинг|оптоволок|волокн|свет|расстоян|стен|помех|покрыти)\w*|'
    r'сет[ьи]|сетей|сетью|сетям?и?|соединяет|связывает|объединяет|передает|'
    r'передают|передачи|работает|работают|показывает|измеряется|использует|'
    r'время|времени|доходит|идет|идти|обратно|возвращается|туда|ответ|назад|сервер\w*|узл\w*|'
    r'позволяет|обмениваться|доступ|проводов|быстрее|дальше|выше|ниже|лучше')


def _technical_answer(answer):
    if not isinstance(answer, str) or not re.fullmatch(r'[\w\s.,:;!?()—–/-]{1,450}', answer):
        return False
    words = re.findall(r'\w+', answer.casefold().replace('ё', 'е'))
    return bool(words) and all(_TECH_WORD.fullmatch(word) for word in words)


def split_social(text):
    """Return substantive text, or a local route for a purely social utterance."""
    rest = text.strip()
    if re.fullmatch(r'(?:хорошо[\s,!.]+)?спасибо[.!\s]*', rest, re.I):
        return '', 'local:thanks'
    if re.fullmatch(r'а[.!?\s]*', rest, re.I):
        return '', 'local:filler'
    bye = False
    while match := _SOCIAL.match(rest):
        if re.match(r'^пока\s+не\b', rest, re.I):
            break  # A temporary refusal is not a farewell.
        bye |= bool(re.match(_BYE + r'\b', match[0], re.I))
        rest = rest[match.end():]
    if not rest and bye:
        return '', 'local:farewell'
    if not rest and re.match(_GREETING + r'\b', text.strip(), re.I):
        return '', 'local:greeting'
    # A trailing farewell can follow the actual question. Require a clause
    # boundary: «пока» in «что доступно пока» is not necessarily a farewell.
    rest = re.sub(rf'[,;.!?]+\s*(?:спасибо[,!\s]+)?{_BYE}[.!?\s]*$', '', rest, flags=re.I)
    return rest or text, None


def local_route(text):
    if payment_request(text) or _PRICE.search(text):
        return 'local:payment'
    return 'local:crm' if _CRM.search(text) else 'local:consultation'


def payment_request(text):
    return bool(_PAYMENT.search(text))


def _local_facts(match):
    subjects = [subject for subject in match.groups() if subject]
    return ' '.join(fact for pattern, fact in _TOPICS if any(re.fullmatch(pattern, s) for s in subjects))


def payment_answer(text):
    # A mixed turn is entirely local, including any standalone technical clause.
    parts = re.split(r'[?!;]+|(?<!\d)\.(?!\d)|,?\s+(?:и|а)\s+', text.casefold().replace('ё', 'е'))
    facts = [_local_facts(match) for part in parts if (match := _QUESTION.fullmatch(part.strip(' ,.:')))]
    return ' '.join([*facts, 'У меня нет доступа к платежам и балансу лицевого счёта. Проверьте их в личном кабинете.'])


async def explanation(text):
    question = ' '.join(text.casefold().replace('ё', 'е').strip(' .!?').split())
    match = _QUESTION.fullmatch(question)
    if not match:
        return None
    answer = await gemini.explain(question)
    if _technical_answer(answer):
        return answer, 'gemini:education'
    return _local_facts(match), 'local:education_fallback'
