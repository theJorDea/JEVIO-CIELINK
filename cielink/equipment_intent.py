"""Local grammar for choosing one of the routers just presented in training mode."""
import re


# Ordinals refer to presentation order; model descriptions refer to fixture IDs.
_REFERENCES = {
    'first': r'(?:перв(?:ый|ую|ого|ая)|1(?:\s?й)?|один)',
    'second': r'(?:втор(?:ой|ую|ого|ая)|2(?:\s?й)?|два)',
    'basic': r'(?:базов(?:ый|ую|ого|ая)|(?:тот что )?подешевле)',
    'dual-band': r'(?:двух\s?диапазонн(?:ый|ую|ого|ая)|(?:тот что )?ловит на двух частотах)',
}
_PREFIX = (r'(?:(?:я понял[аи]?|понятно|ну|а|да|тогда|все таки|ладно|пожалуй|лучше|'
           r'я|мне бы|мне|давайте|давай|беру|возьму|возьмем|выберу|выбираю|хочу|'
           r'нужен|подойдет|оставьте|запишите|остановимся на|пожалуйста|тот) )*')
_NOUN = r'(?:вариант|модель|роутер)'


def _reference(clause: str) -> str:
    for label, ref in _REFERENCES.items():
        if re.fullmatch(_PREFIX + rf'(?:{_NOUN} (?:номер )?)?{ref}'
                        rf'(?: (?:wi fi|wifi|вай фай))?(?: {_NOUN})?(?: пожалуйста)?', clause):
            return label
    return 'unknown'


async def classify_equipment(phrase: str) -> str:
    """Return a reference label, never a catalog row or an operation."""
    question = '?' in phrase
    phrase = ' '.join(re.findall(r'\w+', phrase.casefold().replace('ё', 'е')))
    # A polite request may end in '?'; a bare or informational question isn't a choice.
    if request := re.fullmatch(r'(?:хорошо )?можно (.+)', phrase):
        return _reference(request[1])
    if question:
        return 'unknown'
    phrase = re.sub(r'^я понял[аи]? что у вас (?:2|два) варианта\b', '', phrase).strip()
    # Accept an explicit repair only when it replaces an actual selection clause.
    clauses = re.split(r'\b(?:(?:а )?нет|ой|точнее|вернее|поправка)\b', phrase)
    for discarded in clauses[:-1]:
        if discarded.strip() and _reference(discarded.strip()) == 'unknown':
            return 'unknown'
    clause = clauses[-1].strip()
    if contrast := re.fullmatch(r'не (.+?) а (.+)', clause):
        if _reference(contrast[1]) == 'unknown':
            return 'unknown'
        clause = contrast[2]
    return _reference(clause)
