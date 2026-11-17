#!/usr/bin/env bash
# Run as the Ubuntu login user in the uploaded project directory.
# Installs project dependencies only; does not upgrade the OS or start a service.
set -euo pipefail
cd /home/ubuntu/movie-booking

sudo -n apt-get update
sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends python3-venv ca-certificates

python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
