from adaos.agent.core.event_bus import BUS


async def emit(topic: str, payload: dict, **kw):
    return await BUS.publish(topic, payload, **kw)


async def on(topic: str, handler):
    print("topic, handler", topic, handler)
    return await BUS.subscribe(topic, handler)
