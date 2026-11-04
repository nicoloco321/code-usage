#!/usr/bin/env python3
"""Build pi/emoji.pack, the colour emoji the display draws in its text, from Twemoji.

    python3 pi/build_emoji.py                          # downloads the version below
    python3 pi/build_emoji.py --tarball twemoji.tar.gz # one you've already got

You only need this to move to a newer Twemoji: the pack is in the repo, and the
display reads it as it is (see EmojiPack in claude_display.py). Standard library
only; the same tarball always makes the same pack.

The pack is its first line, JSON, and then Twemoji's 72 px PNGs one after the
other. It isn't a zip because a Pi 2 takes most of a second to open a zip of
4,000 files, and it'd do that with the screen frozen. The JSON says:

    "emoji"    an emoji's characters, without U+FE0F -> [where its PNG starts,
               counted from the end of the first line, and how long it is]
    "longest"  a first character -> how many characters the longest emoji
               starting with it has

Twemoji's graphics are (c) Twitter, Inc and other contributors, used under
CC-BY 4.0: the first line says so, and the README credits them. Keep both if
you change this.
"""

import argparse
import json
import os
import re
import sys
import tarfile
import tempfile
import urllib.request

VERSION = "17.0.3"
SOURCE = f"https://github.com/jdecked/twemoji/archive/refs/tags/v{VERSION}.tar.gz"
NOTICE = (f"Twemoji {VERSION} graphics (c) Twitter, Inc and other contributors, "
          "CC-BY 4.0 https://creativecommons.org/licenses/by/4.0/ - https://github.com/jdecked/twemoji")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "emoji.pack")
PNG = b"\x89PNG\r\n\x1a\n"
NAME = re.compile(r"[0-9a-f]+(-[0-9a-f]+)*")


def characters(name):
    """Twemoji's file name for an emoji -> its characters, leaving out U+FE0F
    (which it names only sometimes, and text only sometimes has)."""
    return "".join(chr(int(part, 16)) for part in name.split("-") if part != "fe0f")


def build(tarball, out):
    """Read the PNGs straight from the tarball (nothing's unpacked to disk) and
    write the pack beside `out`, then swap it in. Returns how many went in."""
    images = {}
    with tarfile.open(tarball, "r:gz") as tar:
        for member in tar:
            parts = member.name.split("/")
            if member.isfile() and len(parts) == 4 and parts[1:3] == ["assets", "72x72"]:
                name, ext = os.path.splitext(parts[3])
                data = tar.extractfile(member).read()
                if ext != ".png" or not NAME.fullmatch(name) or not data.startswith(PNG):
                    sys.exit(f"not an emoji PNG: {member.name}")
                images[characters(name)] = data
    if not images:
        sys.exit(f"{tarball} isn't a Twemoji tarball: it has no assets/72x72")

    emoji, longest, at = {}, {}, 0
    for chars in sorted(images):
        emoji[chars] = [at, len(images[chars])]
        longest[chars[0]] = max(longest.get(chars[0], 0), len(chars))
        at += len(images[chars])
    index = json.dumps({"notice": NOTICE, "emoji": emoji, "longest": longest},
                       ensure_ascii=False, separators=(",", ":"))
    temp = out + ".new"
    with open(temp, "wb") as f:
        f.write(index.encode() + b"\n")
        for chars in sorted(images):
            f.write(images[chars])
    os.replace(temp, out)
    return len(images)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tarball", help=f"a Twemoji source tarball (default: download v{VERSION})")
    ap.add_argument("--out", default=OUT, help="where to write it (default: emoji.pack beside this script)")
    args = ap.parse_args()
    if args.tarball:
        count = build(args.tarball, args.out)
    else:
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "twemoji.tar.gz")
            print(f"downloading {SOURCE}")
            urllib.request.urlretrieve(SOURCE, path)
            count = build(path, args.out)
    print(f"{args.out}: {count} emoji, {os.path.getsize(args.out) / 1048576:.1f} MB")


if __name__ == "__main__":
    main()
