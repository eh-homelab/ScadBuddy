#!/bin/sh
# The fake CLI with FAKE_HANG=1: a committed wrapper, since tests share one environment
# and an executable written at run time can fail to exec (ETXTBSY).
FAKE_HANG=1 exec "$(dirname "$0")/../fake-bambu-studio.sh" "$@"
