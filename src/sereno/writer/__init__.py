"""The DeepSeek writer: the external program of the attack_adaptive arm's adaptive memory hook.

The hook (sereno.context_eval.adaptive) runs `command request.json response.json` before every session of a chain. This
package is such a program. It builds what the provider may see from an explicit allowlist, makes the provider call
under a shared deadline, checks the answer against the harness's own limits, keeps the writer's private notebook per
chain and records every request and answer. It imports the harness; the harness never imports it.
"""
