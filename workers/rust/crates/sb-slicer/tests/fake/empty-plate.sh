#!/bin/sh
# The fake CLI with FAKE_EMPTY_PLATE=1: a committed wrapper, since tests share one environment
# and an executable written at run time can fail to exec (ETXTBSY).
FAKE_EMPTY_PLATE=1 exec "$(dirname "$0")/../fake-bambu-studio.sh" "$@"
