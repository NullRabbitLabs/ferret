"""
Auto-import all chain modules so their ChainTools subclasses self-register
via ChainTools.__init_subclass__.

Adding a new chain requires no changes here — create the module and update
networks.json. pkgutil.iter_modules discovers it automatically at startup.
"""

import importlib
import pkgutil
from pathlib import Path

for _mod in pkgutil.iter_modules([str(Path(__file__).parent)]):
    if _mod.name != "base":
        importlib.import_module(f"src.tools.blockchain.{_mod.name}")
