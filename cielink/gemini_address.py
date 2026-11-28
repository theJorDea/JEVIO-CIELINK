"""Read-only address lookup for the experimental Gemini dialogue."""
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from .addresses import DemoAddressAdapter, DemoCoverageAdapter
from .consultation import Consultation
from .demo_storage import DemoAddressStore
from .domain import Offers, QuoteRequest
from .quotes import FixtureQuoteAdapter


async def check_address(text: str) -> dict:
    """Resolve a full address and return its verified offer in snapshot rubles, if any."""
    # DemoAddressStore materializes new houses. Keep those writes out of session data.
    with TemporaryDirectory() as scratch:
        store = DemoAddressStore(Path(scratch) / 'demo.sqlite3')
        match = await DemoAddressAdapter(store).resolve_address(text)
        result = dict(status=match.status, address='', coverage_status='unknown', offer=None)
        if match.status != 'resolved':
            return result

        address = match.address
        result['address'] = Consultation.describe(address)
        coverage = await DemoCoverageAdapter(store).check_coverage(address.house_id)
        result['coverage_status'] = coverage.status
        if coverage.status == 'available':
            quotes = FixtureQuoteAdapter(store)
            quote = await quotes.quote(QuoteRequest(house_id=address.house_id,
                                                   on_date=date.fromisoformat(quotes.version),
                                                   catalog_version=quotes.version))
            if isinstance(quote, Offers):
                result['offer'] = quote.offers[0].model_dump(mode='json')
        return result
