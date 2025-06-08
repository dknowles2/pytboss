#!/usr/bin/env python3
"""Updates README.md with the current list of supported grill models."""

from pathlib import Path
from sys import path

ME = Path(__file__)
path.append(str(ME.parent.parent))

from pytboss import grills  # noqa: E402

README = (ME.parent / Path("../README.md")).absolute()
START = "<!-- GRILLS START -->\n"
END = "<!-- GRILLS END -->\n"


def main():
    """Main function."""
    lines = []
    with README.open("r") as f:
        skip = False
        for line in f.readlines():
            if not skip:
                lines.append(line)
            if line == START:
                skip = True
                lines.append("\n")
                for grill in grills.get_grills():
                    lines.append(f"*  [{grill.name}]({grill.json['image_url']})\n")
                lines.append("\n")
                lines.append(END)
            elif line == END:
                skip = False

    with README.open("w") as f:
        f.writelines(lines)


if __name__ == "__main__":
    main()
