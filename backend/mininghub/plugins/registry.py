from .bitaxe import BitaxePlugin
from .cgminer import CGMinerPlugin
from .http import HTTPPlugin
from .luckyminer import LuckyMinerPlugin
from .nerdminer import NerdMinerPlugin
PLUGINS={p.name:p for p in [HTTPPlugin(), BitaxePlugin(), NerdMinerPlugin(), LuckyMinerPlugin(), CGMinerPlugin()]}
def get_plugin(name: str): return PLUGINS[name]
def list_plugins(): return sorted(PLUGINS)
