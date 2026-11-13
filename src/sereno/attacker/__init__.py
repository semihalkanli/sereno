"""A model-agnostic adaptive attacker agent.

The harness calls this package as an external program of the run-local attacker protocol
(`sereno.context_eval.run_attacker`): one request in, one response out, no state of its own. The program reads the
repository's code, the agent's recorded transcript and the carried memory through bounded tools, stages memory actions,
updates its strategy and notebook, and lets the provider's model decide what to do. The operator's brief is a file;
this package writes no attack text.

Modules:
    config     the operator's brief, the provider block and the runtime bounds
    providers  the model APIs behind one interface: openai-compatible, anthropic
    observation  the request's observation as a bounded envelope plus paged transcript access
    tools      the bounded tool catalog the model drives
    agent      the tool loop that turns a request into a protocol response
    protocol   request validation and the response's form
    cli        `python -m sereno.attacker --live --config ... request.json response.json`
"""
