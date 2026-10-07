"""Sereno command line.

sereno context-eval ...   prompt injection and memory poisoning experiments on DeepSWE tasks
"""

import argparse
import sys


def main() -> None:
    from sereno.context_eval.cli import add_parser, execute

    parser = argparse.ArgumentParser(
        prog="sereno", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    add_parser(sub)
    sys.exit(execute(parser.parse_args()))
