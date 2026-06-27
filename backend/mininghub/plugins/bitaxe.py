from .http import HTTPPlugin
class BitaxePlugin(HTTPPlugin):
    name='bitaxe'
    paths=('/api/system/info','/api/status','/api/swarm/info')
