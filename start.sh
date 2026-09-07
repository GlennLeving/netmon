#!/usr/bin/env bash
# Start netmon. Brug --host 0.0.0.0 hvis UI skal kunne naas fra andre maskiner.
cd "$(dirname "$0")"
exec python3 netmon.py "$@"
