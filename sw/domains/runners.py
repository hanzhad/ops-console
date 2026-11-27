"""CI runners block: the runner containers, with the cpu and memory of the host block's docker sample."""

from sw import config  # noqa: F401  (puts bin/ on sys.path)
import runners as runners_mod
from sw.watch import BlockSpec

PERIOD = 10


def read_runners(store):
    return runners_mod.collect((store.block_data("host") or {}).get("stats", {}))


BLOCKS = [BlockSpec("runners", PERIOD, read_runners, after=("host",))]
ROUTES = {}
