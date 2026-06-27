from .http import HTTPPlugin
class LuckyMinerPlugin(HTTPPlugin):
    name='luckyminer'
    paths=('/api/status','/api/system/info','/status')
