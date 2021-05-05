#!/bin/bash

echo ""

set -e
set -x

# Overrides ~/node_modules/.bin/hs default short alias (symlink)
# installed in node modules binaries by http-server

http-server -c-1 $@
