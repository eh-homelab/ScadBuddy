"""The agent's data behind the CRUD service (#5299, #5342). Go's core (``tools/go/core``)
serves most of the agent's CRUD routes (#7667). Here is what the API's own routes and its
start still run: the re-wrap of the agent's sealed rows under a new KEK (``rotation.py``), a
subject's forget (``forget.py``), the workspace's sealed files (``workspace.py``), the
command record (``operations.py``), the package mirrors and the plugin writes.

The agent's tables are the agent's migrations' own; each function here is the SQL the agent
ran itself before, as one transaction. The KEK opens and seals here (D5), and the agent
never gets it.
"""
