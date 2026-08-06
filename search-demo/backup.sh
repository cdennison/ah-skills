#!/bin/bash
set -euo pipefail

rsync -avh --progress \
  --exclude='repos/' \
  --exclude='.git/' \
  --exclude='venv/' \
  --exclude='.venv/' \
  /Users/c/code/ah-skills/ /Volumes/Seagate/ah/ah-skills/
