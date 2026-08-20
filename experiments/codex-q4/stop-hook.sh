#!/bin/sh
{
  echo "=== STOP $(date -u +%FT%T.%3NZ)"
  echo "cwd: $(pwd)"
  echo "argv: $*"
  echo "stdin-begin"; cat; echo "stdin-end"
} >> /home/node/.codex/stop.log 2>&1
