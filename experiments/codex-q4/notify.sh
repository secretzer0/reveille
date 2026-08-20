#!/bin/sh
echo "=== NOTIFY $(date -u +%FT%T.%3NZ) cwd=$(pwd) arg=$1" >> /home/node/.codex/notify.log
