"""Server-side client for Bambuddy's REST API and the send/queue flows built on it.

Import from the submodules (``client``, ``models``, ``errors``, ``send``) rather than
from here: ``settings_store`` imports :mod:`scadbuddy.bambuddy.models`, so a
re-exporting ``__init__`` would close an import cycle.
"""
