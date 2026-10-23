"""The program the hook runs: `python -m sereno.writer --config writer.json request.json response.json`.

Exit status 0 means a response was written: a ready candidate, a deliberate no_change, or a candidate the harness will
record as rejected. Any other status means the writer could not do its work and wrote no response (decision 405): 2 the
configuration or campaign limits, 3 the request, state or input, 4 the provider call, 5 an answer the hook contract
cannot carry, 1 anything unexpected. The reason goes to stderr as one line; the hook keeps it beside the step.
"""

import argparse
import json
import sys
from pathlib import Path

from sereno.writer.config import load_config
from sereno.writer.errors import WriterError
from sereno.writer.provider import Deadline
from sereno.writer.step import run_step


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="writer", description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--config", type=Path, required=True, help="writer.json")
    parser.add_argument("request", type=Path)
    parser.add_argument("response", type=Path)
    args = parser.parse_args(argv)
    # The deadline starts once the configuration is read, so building the view counts against it.
    try:
        config = load_config(args.config)
        summary = run_step(config, args.request, args.response, Deadline(config.deadline_seconds))
    except WriterError as error:
        print(f"writer: {error.code}: {error}", file=sys.stderr)
        return error.exit_code
    except Exception as error:
        print(f"writer: internal_error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
