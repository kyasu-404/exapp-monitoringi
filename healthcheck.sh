#!/bin/sh
set -eu

curl -fsS --unix-socket /tmp/exapp.sock http://localhost/heartbeat >/dev/null

