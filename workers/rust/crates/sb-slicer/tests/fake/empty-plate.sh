#!/bin/sh
# The fake CLI failing a plate as Bambu Studio does (#4638): exit 206, its reason only on stdout.
FAKE_EXIT=206 FAKE_STDOUT_ERROR=1 exec "$(dirname "$0")/../fake-bambu-studio.sh" "$@"
