#!/usr/bin/python3
"""Script that dumps all grill definitions as JSON to stdout.

Application credentails should be stored in a file called ".pitboss" in your
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
MODELS = ("LBL", "LFS", "PBL", "PBP", "PBC", "PBM", "PBV", "PBG")


def login(username, password):
    params = {"email": username, "password": password}
    resp = requests.post(API_URL + "/login/app", params=params)
    token = resp.json()["data"]["token"]
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def get_grill_details(grill_id, auth):
    resp = requests.get(API_URL + f"/grills/{grill_id}", headers=auth)
    return resp.json()["data"]["grill"]


def get_model_grills(model, auth):
    resp = requests.get(API_URL + f"/grills?control_board={model}", headers=auth)
    for grill in resp.json()["data"]["grills"]:
        yield get_grill_details(grill["id"], auth)


def main():
    cfg_txt = (Path.home() / ".pitboss").read_text()
    cfg = configparser.ConfigParser()
    cfg.read(str(Path.home() / ".pitboss"))
    auth = login(cfg["pitboss"]["username"], cfg["pitboss"]["password"])
    grills = []
    for model in MODELS:
        for grill in get_model_grills(model, auth):
            grills.append(grill)

    print(json.dumps({"grills": grills}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
