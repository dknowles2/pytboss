#!/usr/bin/python3
"""Script that dumps all grill specifications as JSON to stdout."""

import json
import logging

import requests

logging.basicConfig(level=logging.DEBUG)  # Log all HTTP requests to stderr.
API_URL = "https://api-prod.dansonscorp.com/api/v1"


def get_grill_details(grill_id):
    logging.info("Fetching grill details for grill_id: %s", grill_id)
    resp = requests.get(API_URL + f"/grills/{grill_id}")
    resp.raise_for_status()
    return resp.json()["data"]["grill"]


def main():
    grills = {}
    for i in range(1, 150):
        try:
            grill = get_grill_details(i)
        except requests.HTTPError:
            continue
        grills[grill["name"]] = grill

    print(json.dumps(grills, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
