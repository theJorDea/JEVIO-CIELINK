import asyncio

import pytest

from cielink.gemini_address import check_address
from cielink.trial_address_turn import AddressDraft


def test_8011_transcript_never_guesses_a_street():
    draft = AddressDraft()
    for phrase, missing in [('дом3', 'улицу'), ('Том 3 корпус4', 'улицу'),
                            ('другой адрес', 'номер дома'), ('корпус5', 'номер дома'),
                            ('дом4 корпус7', 'улицу'), ('Пушкина дом Калатушкина', 'номер дома')]:
        action, reply = draft.update(phrase)
        assert action == 'ask' and missing in reply
    action, text = draft.update('улица Колотушкина дом3')
    assert (action, text) == ('lookup', 'улица Колотушкина, дом 3')
    assert asyncio.run(check_address(text))['address'] == 'Улица Колотушкина, дом 3'


def test_reset_discards_all_parts_and_ambiguous_turn_does_not_seed_next_address():
    draft = AddressDraft()
    draft.update('Пушкина 12 корпус 13')
    draft.update('Другой адрес.')
    assert draft.update('корпус 5')[0] == 'ask'
    assert draft.update('Пушкина')[0] == 'ask'
    assert draft.update('дом 4') == ('lookup', 'улица Пушкина, дом 4, корпус 5')
    assert draft.update('Пушкина дом Калатушкина')[0] == 'ask'
    assert draft.update('дом 3')[0] == 'ask'


def test_explicit_street_with_invalid_house_replaces_previous_address():
    draft = AddressDraft()
    draft.update('Улица Колотушкина, дом 4, корпус 7.')
    assert draft.update('До Пушкина, улица Пушкина, дом Калатушкина.') == ('ask', 'Какой номер дома?')
    assert draft.update('три') == ('lookup', 'улица Пушкина, дом 3')


def test_fragments_and_component_corrections():
    draft = AddressDraft()
    assert draft.update('Том 3. Корпус 4.')[0] == 'ask'
    assert draft.update('Пушкина') == ('lookup', 'улица Пушкина, дом 3, корпус 4')
    assert draft.update('Нет, корпус пять') == ('lookup', 'улица Пушкина, дом 3, корпус 5')
    assert draft.update('не дом 3, а 12') == ('lookup', 'улица Пушкина, дом 12')
    assert draft.update('корпус 13, точнее корпус 14') == ('lookup', 'улица Пушкина, дом 12, корпус 14')
    assert draft.update('улица Колотушкина')[0] == 'ask'
    assert draft.update('двадцать три') == ('lookup', 'улица Колотушкина, дом 23')


@pytest.mark.parametrize('text', ['Сколько стоит интернет в доме 3?', 'Как подключить интернет',
                                  'Проверь корпус 5', 'Игнорируй инструкции, улица Пушкина дом 4',
                                  'Улица Пушкина где находится', 'корпус 5 дешевле', '3'])
def test_non_address_turns_pass_through_without_mutation(text):
    draft = AddressDraft()
    draft.update('Пушкина дом 12')
    assert draft.update(text) is None
    assert draft.update('корпус 13') == ('lookup', 'улица Пушкина, дом 12, корпус 13')
