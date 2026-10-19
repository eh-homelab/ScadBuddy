#!/bin/sh
# The fake CLI with FAKE_HOLDER=1: a committed wrapper, as straggler.sh is.
FAKE_HOLDER=1 exec "$(dirname "$0")/../fake-bambu-studio.sh" "$@"
