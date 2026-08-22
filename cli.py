# -*- coding: utf-8 -*-
"""Remove name tags from CS2 demo files.

Usage:
    python cli.py <input.dem> [output.dem] --tag "TEXT TO REMOVE"
"""
from __future__ import annotations

import argparse
import os
import sys

from tag_strip import strip_file


def default_output(path: str) -> str:
    base, ext = os.path.splitext(path)
    return base + "_cleaned" + ext


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Remove name tags from a CS2 demo file."
    )
    parser.add_argument("input", help="input .dem file")
    parser.add_argument("output", nargs="?",
                        help="output .dem file (default: <input>_cleaned.dem)")
    parser.add_argument("--tag", action="append", default=[], metavar="TEXT",
                        required=True,
                        help="the text to remove (repeatable to remove several)")
    args = parser.parse_args(argv)

    src = args.input
    if not os.path.isfile(src):
        parser.error(f"input file not found: {src}")
    dst = args.output or default_output(src)
    if os.path.abspath(src) == os.path.abspath(dst):
        parser.error("input and output must be different files")

    tags = [t.encode("utf-8") for t in args.tag if t.strip()]
    if not tags:
        parser.error("--tag must not be empty")
    print(f"input:  {src}")
    print(f"output: {dst}")
    print(f"tags:   {[t.decode('utf-8', 'replace') for t in tags]}")

    def progress(done, total):
        print(f"\rscanning... {done}/{total} frames", end="", flush=True)

    n, removed, delta, left = strip_file(src, dst, tags=tags, progress=progress)
    print()
    print(f"done: removed {removed} tag(s) from {n} frame(s), size delta {delta:+d} bytes")
    if left:
        print(f"warning: {left} occurrence(s) left untouched (unrecognised structure)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
