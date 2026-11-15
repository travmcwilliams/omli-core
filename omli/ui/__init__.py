"""The local UI: a server on 127.0.0.1 that only a logged-in user can reach.

Install it with ``pip install "omli[ui]"`` and run ``omli-ui init`` once, then
``omli-ui serve``. Importing this package needs nothing beyond the core: the web
stack is imported only by ``omli.ui.server``.

* ``auth``     — each user's own ed25519 login key, challenges and sessions
* ``projects`` — the user's project list, as append-only events under OMLI_HOME
* ``server``   — the FastAPI app and its request guard
"""
