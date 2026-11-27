"""One watcher thread per block. The watcher is the only writer of its block (through put_block)."""

import itertools
import threading
from dataclasses import dataclass
from typing import Any, Callable, Tuple

from sw import ctx

_seq = itertools.count(1)
_seq_lock = threading.Lock()


def seq() -> int:
    """Process-wide counter: a pass records it when it starts, a command when it finishes."""
    with _seq_lock:
        return next(_seq)


@dataclass(frozen=True)
class BlockSpec:
    name: str
    period: float
    read: Callable[[Any], Any]  # read(store) -> data
    after: Tuple[str, ...] = ()  # woken when the rev of any of these blocks changes


class Watchers:
    def __init__(self, store, on_pass):
        self.store = store
        self.on_pass = on_pass  # fn(name, pass_started_seq)
        self.specs = {}
        self.events = {}

    def start(self, specs):
        for spec in specs:
            self.specs[spec.name] = spec
            self.events[spec.name] = threading.Event()
        for spec in specs:
            threading.Thread(target=self._loop, args=(spec,), daemon=True, name="watch-" + spec.name).start()

    def wake(self, name):
        ev = self.events.get(name)
        if ev:
            ev.set()

    def step(self, spec):
        ev = self.events.get(spec.name)
        if ev is not None:
            ev.clear()  # a wake that arrives during the pass starts the next one at once
        wait = spec.period
        s = seq()
        try:
            data = spec.read(self.store)
            rev = self.store.put_block(spec.name, data, s, spec.period)
            self.on_pass(spec.name, s)
            if rev is not None:
                for other in self.specs.values():
                    if spec.name in other.after:
                        self.wake(other.name)
        except Exception as e:
            try:
                self.store.put_error(spec.name, f"{type(e).__name__}: {e}")
            except Exception as e2:  # a full disk must not end the thread of this block
                ctx.note(f"{spec.name}: put_error failed: {e2}"[:200])
            wait = min(spec.period, 15)
        return wait

    def _loop(self, spec):
        ev = self.events[spec.name]
        while True:
            ev.wait(self.step(spec))
