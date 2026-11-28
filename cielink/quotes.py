"""Read-only quote calculation against a versioned, local training snapshot."""
import json
from datetime import date
from pathlib import Path

from .addresses import OFFER_PROFILES
from .demo_storage import DemoAddressStore
from .domain import NoOffer, Offer, Offers, PricePeriod, QuoteRequest, QuoteResult

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'


class FixtureQuoteAdapter:
    def __init__(self, demo_store=None):
        self.demo_store = demo_store if demo_store is not None else DemoAddressStore()
        snapshot = json.loads((FIXTURES / 'catalog.json').read_text(encoding='utf-8'))
        self.version = snapshot['snapshot_date']
        self.offers = {row['id']: row for row in snapshot['offers']}
        self.eligibility = {row['house_id']: row['eligible_offer_ids'] for row in snapshot['synthetic_house_eligibility']}
        self.house_terms = json.loads((FIXTURES / 'house-terms.json').read_text(encoding='utf-8'))

    async def quote(self, request: QuoteRequest) -> QuoteResult:
        if request.catalog_version != self.version:
            return NoOffer(reason='Версия каталога изменилась. Требуется новый расчёт.')
        candidates = []
        exclusions = []
        profile = OFFER_PROFILES.get(request.house_id, request.house_id)
        if (profile not in self.eligibility and request.house_id.startswith('demo-')
                and self.demo_store.status(request.house_id) == 'available'):
            profile = 'training-house-a'  # Borrow prices only; offers retain the requested house ID.
        for offer_id in self.eligibility.get(profile, []):
            row = self.offers[offer_id]
            terms = self.house_terms.get(profile, {}).get(offer_id, {})
            reason = self._exclude(row, terms, request)
            if reason:
                exclusions.append(f"{row['name']}: {reason}")
                continue
            advance = row['activation_account_advance_rub']
            advance = terms.get('advance_rub') if advance is None else advance
            periods = ([PricePeriod(months=row['promo']['duration_months'], monthly_price=row['monthly_rub']),
                        PricePeriod(monthly_price=row['promo']['after_monthly_rub'])] if row['promo']
                       else [PricePeriod(monthly_price=row['monthly_rub'])])
            candidates.append(Offer(offer_id=offer_id, house_id=request.house_id, catalog_version=self.version,
                                    advance=advance, periods=periods, mandatory_charges=terms['mandatory_charges'],
                                    services=['Домашний интернет'], conditions='Учебные условия дома: собственный роутер; суммы по дому не являются публичными условиями SkyNet.',
                                    source_url=row['source_url'], source_date=date.fromisoformat(self.version)))
        if not candidates:
            return NoOffer(reason='; '.join(exclusions) or 'Доступность тарифов по этому дому не подтверждена.')
        candidates.sort(key=lambda o: (o.periods[-1].monthly_price, o.advance + sum(o.mandatory_charges.values()), o.offer_id))
        return Offers(offers=candidates[:2])

    @staticmethod
    def _exclude(row, terms, request):
        if request.on_date < date.fromisoformat(request.catalog_version):
            return 'нет подтверждённых условий на эту дату'
        if row['connect_through'] and request.on_date > date.fromisoformat(row['connect_through']):
            return 'срок подключения по акции истёк'
        promo = row['promo']
        if promo and (promo['duration_months'] is None or promo['after_monthly_rub'] is None):
            return 'нужно уточнить цену и срок после акции'
        if promo and promo.get('new_subscribers_only') and not terms.get('new_subscriber_confirmed'):
            return 'не подтверждено право на акцию'
        regular = promo['after_monthly_rub'] if promo else row['monthly_rub']
        if request.monthly_budget is not None and max(regular, row['monthly_rub']) > request.monthly_budget:
            return 'превышен ежемесячный бюджет'
        advance = row['activation_account_advance_rub']
        if advance is None:
            advance = terms.get('advance_rub')
        charges = terms.get('mandatory_charges')
        if advance is None or charges is None:
            return 'нужно уточнить платёж и обязательные доплаты'
        if request.budget_today is not None and advance + sum(charges.values()) > request.budget_today:
            return 'превышен бюджет при подключении'
        if any(n.casefold() not in ('интернет', 'домашний интернет') for n in request.needs):
            return 'нужно уточнить запрошенные услуги'
        return None
