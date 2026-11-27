"""One module per area. Each exports BLOCKS: list[BlockSpec] and ROUTES: dict[path, RouteSpec]; the modules
are found with pkgutil, so a part of the work adds its file here and edits nothing else.
"""

import importlib
import pkgutil


def discover():
    """(blocks, routes) of every domain module."""
    blocks, routes = [], {}
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        mod = importlib.import_module(f"{__name__}.{info.name}")
        blocks.extend(mod.BLOCKS)
        routes.update(mod.ROUTES)
    return blocks, routes
