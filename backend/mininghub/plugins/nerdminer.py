from .http import HTTPPlugin
class NerdMinerPlugin(HTTPPlugin):
    name='nerdminer'
    paths=('/api/status','/status','/')
