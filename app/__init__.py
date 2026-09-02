"""Root-level shim so that ``app.main:app`` resolves from the repository root.

The real package lives at ``backend/app``. The canonical way to run it is::

    uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port $PORT

but a host configured without ``--app-dir backend`` (for example a Start
Command typed into a dashboard) would fail with ``ModuleNotFoundError: No
module named 'app'``. Rather than leave that as a footgun, this module
redirects the ``app`` package's search path at ``backend/app`` so the plain
``uvicorn app.main:app`` also works from the repo root.

Because ``__path__`` is rebound before any submodule is imported, every
intra-package import inside the real package (``from app.services import ...``)
resolves through this same shim to exactly one set of modules. There is no
duplicate-module hazard: ``backend/app/__init__.py`` is empty, so nothing is
skipped by this file taking its place.
"""

import os as _os

__path__ = [_os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "backend", "app")]
