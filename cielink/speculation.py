"""Session-local, read-only lookahead. Only final() releases results to the caller."""
import asyncio
from collections import deque
from dataclasses import dataclass
from time import monotonic
from typing import Literal

from .domain import CoverageAdapter, CoverageResult, QuoteAdapter, QuoteRequest, QuoteResult

Mode = Literal['sequential', 'early', 'speculative']


class StaleRead(Exception):
    """A correction or shutdown overtook this final; do not publish a response."""


@dataclass(eq=False)
class _Read:
    operation: str
    revision: int
    request: QuoteRequest
    task: asyncio.Task | None = None
    requested_by_final: bool = False


class SpeculationCoordinator:
    """Own one per session, on one event loop. The two baselines do no partial I/O."""

    def __init__(self, coverage: CoverageAdapter, quotes: QuoteAdapter, *,
                 mode: Mode = 'speculative', max_tasks: int = 4, trace_limit: int = 128):
        if mode not in ('sequential', 'early', 'speculative'):
            raise ValueError('unknown speculation mode')
        if max_tasks < 1 or trace_limit < 1:
            raise ValueError('limits must be positive')
        self.mode = mode
        self.trace = deque(maxlen=trace_limit)
        self._start = monotonic()
        self._coverage = coverage
        self._quotes = quotes
        self._limit = max_tasks
        self._live: set[asyncio.Task] = set()  # includes retired tasks ignoring cancellation
        self._reads: dict[str, _Read] = {}
        self._revision = 0
        self._changed = asyncio.Event()
        self._previous_partial = None
        self._enabled = False
        self._closed = False

    def _record(self, event, read):
        self.trace.append(dict(event=event, operation=read.operation, revision=read.revision,
                               offset_ms=int((monotonic() - self._start) * 1000)))

    def _wake(self):
        self._changed.set()
        self._changed = asyncio.Event()

    def _retire(self, operation):
        read = self._reads.pop(operation, None)
        if read and read.task and not read.task.done():
            self._record('cancel', read)
            read.task.cancel()

    def invalidate(self) -> None:
        """Forget even a completed result on an unresolved address correction."""
        for operation in list(self._reads):
            self._retire(operation)
        self._previous_partial = None
        self._enabled = False
        self._wake()

    def _select(self, request):
        if self._closed:
            raise StaleRead('session closed')
        old = self._reads.get('quote')
        if old and old.request == request:
            return old
        snapshot = request.model_copy(deep=True)
        self._revision += 1
        if not old or old.request.house_id != request.house_id:
            self._retire('coverage')
            self._reads['coverage'] = _Read('coverage', self._revision, snapshot)
        self._retire('quote')
        self._reads['quote'] = _Read('quote', self._revision, snapshot)
        self._wake()
        return self._reads['quote']

    def partial(self, request: QuoteRequest | None) -> None:
        """Pass a resolved partial, or None for an unresolved address hypothesis."""
        if self._closed:
            return
        if request is None:
            self.invalidate()
            return
        read = self._select(request)
        stable = self.mode == 'speculative' and self._previous_partial == request.house_id
        self._enabled = read.requested_by_final or stable
        self._previous_partial = request.house_id
        if stable:
            self._record('stable', read)
            self._pump()

    def _pump(self):
        if not self._enabled or len(self._live) >= self._limit:
            return
        cov = self._reads['coverage']
        if cov.task is None:
            read = cov
        elif (cov.task.done() and not cov.task.cancelled() and not cov.task.exception()
              and cov.task.result().status == 'available'):
            read = self._reads['quote']
        else:
            return
        if read.task is not None:
            return
        self._record('start', read)
        coro = (self._coverage.check_coverage(read.request.house_id) if read.operation == 'coverage'
                else self._quotes.quote(read.request.model_copy(deep=True)))
        read.task = asyncio.create_task(coro)
        self._live.add(read.task)
        read.task.add_done_callback(lambda task: self._done(read, task))

    def _done(self, read, task):
        self._live.remove(task)
        failed = task.cancelled() or task.exception() is not None  # retrieve background errors
        if self._reads.get(read.operation) is not read:
            self._record('discard', read)
        else:
            self._record('error' if failed else 'ready', read)
        self._wake()
        self._pump()  # only latest demand, never a queue of obsolete hypotheses

    async def final(self, request: QuoteRequest) -> tuple[CoverageResult, QuoteResult | None]:
        """Join matching reads; recheck identity after every wake, including A→B→A."""
        quote = self._select(request)
        quote.requested_by_final = True
        self._previous_partial = None
        self._enabled = True
        self._pump()
        while self._reads.get('quote') is quote:
            coverage = self._reads['coverage']
            if coverage.task and coverage.task.done():
                result = coverage.task.result()
                if result.status != 'available' or (quote.task and quote.task.done()):
                    options = quote.task.result() if result.status == 'available' else None
                    self._record('used', coverage)
                    if options is not None:
                        self._record('used', quote)
                    return result.model_copy(deep=True), options.model_copy(deep=True) if options is not None else None
            await self._changed.wait()
        raise StaleRead('read superseded')

    async def close(self) -> None:
        """Reject new work, suppress all results, and drain every owned adapter call."""
        self._closed = True
        self.invalidate()
        if self._live:
            await asyncio.gather(*self._live, return_exceptions=True)
