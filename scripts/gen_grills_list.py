#!/usr/bin/python3.10
"""Generates a Markdown list of supported grill models."""

from os.path import abspath
from sys import path

path.append(abspath("../pytboss"))

from pytboss import grills  # pylint: disable=import-error,wrong-import-position


def main():
    """Main function."""
    for grill in grills.get_grills():
        print(f"*  [{grill.name}]({grill.json['image_url']})")


if __name__ == "__main__":
    main()
