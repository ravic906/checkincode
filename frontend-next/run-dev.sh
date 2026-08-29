#!/bin/bash
export PATH="$HOME/.local/nodejs/bin:$PATH"
cd "$(dirname "$0")"
exec npm run dev -- --port 5174
