"""Per-call metering and budget reservation (docs/31 §4).

Every model call made by the harness goes through `MeteredModel.call`, which:

1. projects the call's worst-case cost (the same rule as production's 13 §3
   precheck: prompt chars/4 + the call's `max_tokens` at the entry's price);
2. **reserves** that amount against the run's `BudgetLedger` under a lock —
   so N parallel calls cannot all pass a check that only one of them fits
   (the race a sequential precheck would have);
3. makes exactly one provider call, bounded by its timeout;
4. settles the reservation with the actual cost and appends one `CallRecord`.

A retry is a new call with its own reservation and record. No composite
provider hides several calls behind one usage event.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Sequence

from server.models.provider import ChatMessage, ModelPricing, ModelProvider, ModelResult, ModelUnavailable


class BudgetRefused(Exception):
    """A call was refused before it was made: it does not fit the budget."""


@dataclass
class Reservation:
    reservation_id: int
    amount: float
    settled: bool = False


class BudgetLedger:
    def __init__(self, *, overall: float, per_call: float) -> None:
        self.overall = overall
        self.per_call = per_call
        self.spent = 0.0
        self.reserved = 0.0
        self.refusals = 0
        # Calls whose actual cost exceeded what was reserved for them (the
        # chars/4 estimate can under-count). Reported, never hidden.
        self.exceeded_reservation = 0
        self._lock = asyncio.Lock()
        self._ids = itertools.count(1)

    async def reserve(self, amount: float) -> Reservation:
        async with self._lock:
            if amount > self.per_call + 1e-12 or self.spent + self.reserved + amount > self.overall + 1e-12:
                self.refusals += 1
                raise BudgetRefused(f"projected {amount:.6f} does not fit (spent {self.spent:.6f}, "
                                    f"reserved {self.reserved:.6f}, overall {self.overall:.6f}, "
                                    f"per call {self.per_call:.6f})")
            self.reserved += amount
            return Reservation(next(self._ids), amount)

    async def settle(self, reservation: Reservation, actual: float) -> None:
        async with self._lock:
            if reservation.settled:
                return
            reservation.settled = True
            self.reserved -= reservation.amount
            self.spent += actual
            if actual > reservation.amount + 1e-12:
                self.exceeded_reservation += 1

    @property
    def overspent(self) -> bool:
        return self.spent > self.overall + 1e-12


@dataclass
class CallRecord:
    call_id: int
    task_id: str
    role: str                 # "central", "planner", "specialist:research", …
    catalog_id: str
    provider: str
    model: str
    status: str               # "ok" | "failed" | "timed_out" | "budget_refused" | "cancelled" | "blocked"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reserved: float = 0.0
    cost: float = 0.0
    latency_ms: float = 0.0
    prompt_chars: int = 0
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class CallLedger:
    records: list[CallRecord] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def next_id(self) -> int:
        return next(self._ids)

    def for_task(self, task_id: str) -> list[CallRecord]:
        return [r for r in self.records if r.task_id == task_id]


class MeteredModel:
    """One catalogue entry, metered. `provider` is any `ModelProvider` (a real
    adapter in live mode, a scripted one otherwise)."""

    def __init__(self, *, catalog_id: str, provider: ModelProvider, pricing: ModelPricing,
                 ledger: CallLedger, budget: BudgetLedger,
                 prompt_guard: Callable[[str], None] | None = None) -> None:
        self.catalog_id = catalog_id
        self.prompt_guard = prompt_guard
        self.provider = provider
        self.pricing = pricing
        self.ledger = ledger
        self.budget = budget

    def projected_cost(self, messages: Sequence[ChatMessage], max_tokens: int) -> float:
        if not self.pricing.is_paid:
            return 0.0
        chars = sum(len(m.content) for m in messages)
        return self.pricing.cost(prompt_tokens=chars // 4 + 1, completion_tokens=max_tokens)

    async def call(self, messages: Sequence[ChatMessage], *, task_id: str, role: str, max_tokens: int,
                   timeout: float, max_cost: float | None = None) -> ModelResult:
        spec = self.provider.spec
        record = CallRecord(call_id=self.ledger.next_id(), task_id=task_id, role=role, catalog_id=self.catalog_id,
                            provider=spec.provider, model=spec.model, status="budget_refused",
                            prompt_chars=sum(len(m.content) for m in messages))
        self.ledger.records.append(record)
        if self.prompt_guard is not None:
            try:
                for m in messages:
                    self.prompt_guard(m.content)
            except Exception as exc:
                record.status, record.error = "blocked", type(exc).__name__
                raise
        projected = self.projected_cost(messages, max_tokens)
        if max_cost is not None and projected > max_cost + 1e-12:
            # This assignment's own cap (e.g. a specialist's budget), on top of the run's.
            self.budget.refusals += 1
            raise BudgetRefused(f"projected {projected:.6f} exceeds this assignment's cap {max_cost:.6f}")
        reservation = await self.budget.reserve(projected)   # raises BudgetRefused: no call is made
        record.reserved = projected
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(self.provider.invoke(messages, timeout=timeout), timeout=timeout)
        except asyncio.TimeoutError:
            record.status, record.error = "timed_out", "timeout"
            await self.budget.settle(reservation, 0.0)
            raise
        except asyncio.CancelledError:
            record.status, record.error = "cancelled", "cancelled"
            await self.budget.settle(reservation, 0.0)
            raise
        except ModelUnavailable as exc:
            record.status, record.error = "failed", str(exc)[:200]
            await self.budget.settle(reservation, 0.0)
            raise
        finally:
            record.latency_ms = (time.perf_counter() - started) * 1000.0
        cost = self.pricing.cost(prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens)
        await self.budget.settle(reservation, cost)
        record.status = "ok"
        record.prompt_tokens, record.completion_tokens, record.cost = result.prompt_tokens, result.completion_tokens, cost
        return result


__all__ = ["BudgetLedger", "BudgetRefused", "CallLedger", "CallRecord", "MeteredModel", "Reservation"]
