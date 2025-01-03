#!/usr/bin/python3
"""Script that dumps all grill specifications as JSON to stdout.

Application credentials should be stored in a file called ".pitboss" in your
home directory. The format is an INI style like this:

[pitboss]
username = email@address.com
password = my-secret-password
"""

import configparser
import json
import logging
from pathlib import Path

import requests

logging.basicConfig(level=logging.DEBUG)  # Log all HTTP requests to stderr.
API_URL = "https://api-prod.dansonscorp.com/api/v1"
CONTROL_BOARDS = (
    "LBL",
    "LFS",
    "PBA",
    "PBC",
    "PBG",
    "PBL",
    "PBM",
    "PBP",
    "PBT",
    "PBV",
)


def login(username, password):
    params = {"email": username, "password": password}
    resp = requests.post(API_URL + "/login/app", params=params)
    resp.raise_for_status()
    token = resp.json()["data"]["token"]
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def get_grill_details(grill_id, auth):
    logging.info("Fetching grill details for grill_id: %s", grill_id)
    resp = requests.get(API_URL + f"/grills/{grill_id}", headers=auth)
    resp.raise_for_status()
    return resp.json()["data"]["grill"]


def get_control_board_grills(control_board, auth):
    logging.info("Fetching grills for control_board: %s", control_board)
    resp = requests.get(
        API_URL + f"/grills?control_board={control_board}", headers=auth
    )
    resp.raise_for_status()
    for grill in resp.json()["data"]["grills"]:
        yield get_grill_details(grill["id"], auth)


def main():
    cfg = configparser.ConfigParser()
    cfg.read(str(Path.home() / ".pitboss"))
    auth = login(cfg["pitboss"]["username"], cfg["pitboss"]["password"])
    grills = {}
    for i in range(1, 101):
        try:
            grill = get_grill_details(i, auth)
        except requests.HTTPError:
            continue
        grills[grill["name"]] = grill

    print(json.dumps(grills, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
