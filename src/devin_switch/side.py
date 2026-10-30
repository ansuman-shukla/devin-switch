"""Side chats: questions answered from a GUI chat's context without entering that chat.

With `cognition.ai/chains` offered at initialize, the native runs a `session/prompt` whose
`_meta` says `cognition.ai/chain: "side"` as a read-only fork of the chat's main conversation,
even beside a running turn, and tags each notification of that answer the same way.
"""

CHAIN = "cognition.ai/chain"
SIDE = {CHAIN: "side"}
CAPABILITY = "cognition.ai/chains"


def is_side(message: dict) -> bool:
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(meta, dict) and meta.get(CHAIN) == "side"
