#!/usr/bin/python3
"""Script that fetches the newest firmware files for a grill model.

Application credentials should be stored in a file called ".pitboss" in your
home directory. The format is an INI style like this:

[pitboss]
username = email@address.com
password = my-secret-password
"""

import configparser
import logging
from pathlib import Path
import sys

import requests

logging.basicConfig(level=logging.DEBUG)  # Log all HTTP requests to stderr.
API_URL = "https://api-prod.dansonscorp.com/api/v1"
CONTROL_BOARD_ID = 5
CONTROL_BOARD_NAME = "PBV"


def login(username, password):
    params = {"email": username, "password": password}
    resp = requests.post(API_URL + "/login/app", params=params)
    resp.raise_for_status()
    # Example response:
    # {
    #      "status": "success",
    #      "message": null,
    #      "errors": null,
    #      "data": {
    #          "token": "xxx",
    #          "token_expiration": YYYY-MM-DDTHH:MM:SSZ"
    #      }
    # }
    token = resp.json()["data"]["token"]
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }


def get_grills(auth):
    resp = requests.get(API_URL + "/customer-grills", headers=auth)
    resp.raise_for_status()
    return resp.json()["data"]["customer_grills"]


def get_firmware_grill(auth, control_board_name):
    resp = requests.get(
        API_URL + "/grills", headers=auth, params={"control_board": control_board_name}
    )
    resp.raise_for_status()
    return resp.json()["data"]["grills"][0]


def get_firmware_metadata(auth, grill):
    control_board_id = grill["id"]
    resp = requests.get(
        API_URL + f"/firmware-platforms/{control_board_id}", headers=auth
    )
    resp.raise_for_status()
    return resp.json()["data"]["firmwarePlatform"]


def fetch_firmware(auth, control_board_name, grill, output_dir):
    metadata = get_firmware_metadata(auth, grill)
    firmware_base_url = metadata["firmware_base_url"]
    firmware_version_path = metadata["firmware_version"].replace(".", "-")
    for file in metadata["platform_files"]:
        filename = file["filename"]
        fp = Path(filename)
        url = f"{firmware_base_url}{firmware_version_path}/"
        if file["board_specific"] > 0:
            filename = f"{fp.stem}_{control_board_name}{fp.suffix}"
        url += filename
        (output_dir / fp).write_text(fetch_file(url))


def fetch_file(url):
    resp = requests.get(url)
    resp.raise_for_status()
    return resp.text


def main(argv):
    if len(argv) < 2:
        print("Must provide an output path for firmware files", file=sys.stderr)
        sys.exit(1)
    cfg = configparser.ConfigParser()
    cfg.read(str(Path.home() / ".pitboss"))
    auth = login(cfg["pitboss"]["username"], cfg["pitboss"]["password"])
    grills = get_grills(auth)
    control_board_name = grills[0]["board_id"].split("-")[0]
    firmware_grill = get_firmware_grill(auth, control_board_name)
    fetch_firmware(auth, control_board_name, firmware_grill, Path(argv[1]))


if __name__ == "__main__":
    main(sys.argv)
