#!/bin/sh
# The fake CLI with FAKE_EXIT=192: a committed wrapper, since tests share one environment
# and an executable written at run time can fail to exec (ETXTBSY).
FAKE_EXIT=192 exec "$(dirname "$0")/../fake-bambu-studio.sh" "$@"
