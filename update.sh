#!/bin/bash
# Get the latest app code from GitHub and restart. Data is not touched.
set -e
cd "$(dirname "$0")"
git pull
venv/bin/pip install -q -r requirements.txt
sudo systemctl restart ledger
echo "Updated."
