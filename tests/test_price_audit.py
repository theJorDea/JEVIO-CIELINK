import json

from cielink.review import _review


QUOTE = {'advance': '800', 'mandatory_charges': {'installation': '100', 'router': '150'},
         'periods': [{'months': 3, 'monthly_price': '400.50'}, {'monthly_price': '890'}]}


def delivered(text, quote):
    return _review([('playback_started', 125, None), ('offer_verified', 620, json.dumps(quote)),
                    ('playback_ended', 620, text)], None)


def test_financial_fields_do_not_mask_one_another():
    findings = delivered('Сегодня внести 9\u202f999 рублей: аванс 9 000, обязательные доплаты 999. '
                         'Далее ежемесячно 400,50 рублей в течение 6 мес.; затем ежемесячно 890 рублей.', QUOTE)
    assert [(f['code'], f['message']) for f in findings] == [
        ('price_mismatch', 'Сегодня внести: озвучено 9999 руб.; в предложении 1050 руб.'),
        ('price_mismatch', 'Аванс: озвучено 9000 руб.; в предложении 800 руб.'),
        ('price_mismatch', 'Обязательные доплаты: озвучено 999 руб.; в предложении 250 руб.'),
        ('price_match', 'Цена по акции: озвучено 400.50 руб./мес.; в предложении 400.50 руб./мес.'),
        ('price_mismatch', 'Срок акции: озвучено 6 мес.; в предложении 3 мес.'),
        ('price_match', 'Цена после акции: озвучено 890 руб./мес.; в предложении 890 руб./мес.'),
    ]
    assert all((f['start_ms'], f['end_ms']) == (125, 620) for f in findings)


def test_promo_and_postpromo_are_not_interchangeable():
    findings = delivered('Ежемесячно 890 рублей в течение 3 месяцев; затем ежемесячно 400,50 рублей.', QUOTE)
    assert [f['code'] for f in findings] == ['price_mismatch', 'price_match', 'price_mismatch']


def test_actual_consultation_followup_templates():
    regular = {'periods': [{'months': None, 'monthly_price': '1500'}]}
    single = delivered('У этого тарифа нет акции: ежемесячная цена 1700 рублей.', regular)
    assert [(f['code'], f['message']) for f in single] == [
        ('price_mismatch', 'Ежемесячная цена: озвучено 1700 руб./мес.; в предложении 1500 руб./мес.')]
    post = delivered('После акции цена 400,50 рублей в месяц.', QUOTE)
    assert [(f['code'], f['message']) for f in post] == [
        ('price_mismatch', 'Цена после акции: озвучено 400.50 руб./мес.; в предложении 890 руб./мес.')]
    wrong_status = delivered('У этого тарифа нет акции: ежемесячная цена 890 рублей.', QUOTE)
    assert wrong_status[0]['code'] == 'price_mismatch' and '3 мес.' in wrong_status[0]['message']


def test_literal_ellipses_preserve_upfront_amount():
    for ending in ('…', '...'):
        finding, = delivered('Сегодня внести 9\u202f999 рублей' + ending, QUOTE)
        assert finding['code'] == 'price_mismatch'
        assert finding['message'] == 'Сегодня внести: озвучено 9999 руб.; в предложении 1050 руб.'


def test_unqualified_rate_and_labeled_postpromo():
    assert delivered('Ежемесячно 9\u202f999 рублей.', QUOTE)[0]['code'] == 'price_mismatch'
    assert delivered('Ежемесячно 890 рублей.', QUOTE)[0]['code'] == 'price_unverified'
    assert delivered('После акции 890 рублей в месяц.', QUOTE)[0]['code'] == 'price_match'
    no_promo = {'periods': [{'monthly_price': '890'}]}
    assert all(f['code'] == 'price_unverified' for f in delivered(
        'Ежемесячно 890 рублей в течение 3 мес.; затем ежемесячно 890 рублей.', no_promo))


def test_missing_and_interrupted_evidence_is_not_a_match():
    findings = delivered('Сегодня внести 800 рублей: аванс 800, обязательные доплаты 0. '
                         'Далее ежемесячно 400 рублей.', {'advance': 800})
    assert [f['code'] for f in findings] == ['price_unverified', 'price_match', 'price_unverified', 'price_unverified']
    events = [('offer_verified', 1, json.dumps(QUOTE)), ('playback_interrupted', 1, 'Аванс 800.'),
              ('text_displayed', 2, 'Аванс 800.'), ('text_displayed', 3, 'Аванс 800?')]
    assert [f.get('code') for f in _review(events, None)] == [None, 'price_unverified']
