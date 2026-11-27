import asyncio
from datetime import date

import pytest

from cielink.domain import CoverageResult, NoOffer, QuoteRequest
from cielink.speculation import SpeculationCoordinator, StaleRead


def request(house='A', **changes):
    return QuoteRequest(**(dict(house_id=house, on_date=date(2026, 11, 27),
                                catalog_version='v1') | changes))


class Provider:
    """Each call stays alive until the test releases it, even after cancellation."""

    def __init__(self):
        self.calls = asyncio.Queue()
        self.count = 0

    async def wait(self, operation, key):
        self.count += 1
        result = asyncio.get_running_loop().create_future()
        self.calls.put_nowait((operation, key, result))
        while not result.done():
            try:
                await asyncio.shield(result)
            except asyncio.CancelledError:
                pass
        return result.result()

    async def check_coverage(self, house_id):
        status = await self.wait('coverage', house_id)
        return CoverageResult(house_id=house_id, status=status)

    async def quote(self, req):
        reason = await self.wait('quote', req)
        return NoOffer(reason=reason)

    async def take(self, operation, key):
        actual, arg, result = await asyncio.wait_for(self.calls.get(), 2)
        assert (actual, arg) == (operation, key)
        return result


@pytest.mark.parametrize('mode', ['sequential', 'early', 'speculative'])
def test_stable_resolved_house_twice_and_matching_final(mode):
    async def run():
        p = Provider()
        s = SpeculationCoordinator(p, p, mode=mode)
        a, b = request(), request('B')
        for candidate in (a, None, b, a):
            s.partial(candidate)
        await asyncio.sleep(0)
        assert p.count == 0
        s.partial(a)
        s.partial(a)
        if mode == 'speculative':
            (await p.take('coverage', 'A')).set_result('available')
            quote = await p.take('quote', a)
        else:
            await asyncio.sleep(0)
            assert p.count == 0
        final = asyncio.create_task(s.final(a))
        if mode != 'speculative':
            cov = await p.take('coverage', 'A')
            s.partial(a)  # an identical partial must not stall an outstanding final
            cov.set_result('available')
            quote = await p.take('quote', a)
        quote.set_result('current')
        coverage, options = await asyncio.wait_for(final, 2)
        assert (coverage.house_id, options.reason) == ('A', 'current')
        assert p.count == 2
        await s.close()
    asyncio.run(run())


def test_aba_address_race_and_bounded_pending_work():
    async def run():
        p = Provider()
        s = SpeculationCoordinator(p, p, max_tasks=1, trace_limit=8)
        old = asyncio.create_task(s.final(request()))
        late = await p.take('coverage', 'A')
        for i in range(40):
            s.partial(request(str(i)))
            s.partial(request(str(i)))
            await asyncio.sleep(0)
        current = asyncio.create_task(s.final(request()))
        with pytest.raises(StaleRead):
            await asyncio.wait_for(old, 2)
        assert p.count == 1
        late.set_result('available')
        (await p.take('coverage', 'A')).set_result('unknown')
        coverage, quote = await asyncio.wait_for(current, 2)
        assert coverage.status == 'unknown' and quote is None and p.count == 2
        assert len(s.trace) == 8
        assert any(e['event'] == 'discard' for e in s.trace)
        await s.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', [dict(monthly_budget=500), dict(budget_today=0),
                                    dict(needs=['TV']), dict(catalog_version='v2'),
                                    dict(on_date=date(2026, 12, 1))])
def test_only_quote_inputs_changed_and_old_quote_arrives_last(change):
    async def run():
        p = Provider()
        s = SpeculationCoordinator(p, p)
        a = request()
        old = asyncio.create_task(s.final(a))
        (await p.take('coverage', 'A')).set_result('available')
        late = await p.take('quote', a)
        b = request(**change)
        current = asyncio.create_task(s.final(b))
        (await p.take('quote', b)).set_result('new')
        assert (await asyncio.wait_for(current, 2))[1].reason == 'new'
        with pytest.raises(StaleRead):
            await old
        late.set_result('outdated')
        await asyncio.sleep(0)
        assert (await s.final(b))[1].reason == 'new'
        assert p.count == 3
        await s.close()
    asyncio.run(run())


def test_shutdown_drains_ignored_cancellation_and_wakes_consumers():
    async def run():
        p = Provider()
        s = SpeculationCoordinator(p, p)
        final = asyncio.create_task(s.final(request()))
        late = await p.take('coverage', 'A')
        shutdown = asyncio.create_task(s.close())
        with pytest.raises(StaleRead):
            await asyncio.wait_for(final, 2)
        assert not shutdown.done()
        s.partial(request())
        with pytest.raises(StaleRead):
            await s.final(request())
        late.set_result('available')
        await asyncio.wait_for(shutdown, 2)
        await s.close()
        assert p.count == 1 and s.trace[-1]['event'] == 'discard'
    asyncio.run(run())
