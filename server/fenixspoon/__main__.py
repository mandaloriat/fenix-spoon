"""`python -m fenixspoon …` — the same command as the `fenix-spoon` script.

Both spellings exist because they fail in different places. The console script is missing
when the package was added to `sys.path` without being installed; `python -m` is missing
nothing, but says nothing about which interpreter you meant. A caller spawning this as a
child process usually already knows its interpreter, and reaching it as
`[sys.executable, "-m", "fenixspoon", "rpc", "--stdio"]` cannot pick up a `fenix-spoon` from
some other environment on `PATH`.
"""

from .cli import main

raise SystemExit(main())
