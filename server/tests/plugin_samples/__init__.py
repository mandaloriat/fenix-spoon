"""Adapter packages that pretend to come from somewhere else (#105).

These stand in for a third-party distribution: nothing here is imported by the server, and the
plugin tests point the loader at them the way an entry point or `FENIXSPOON_SOLVER_MODULES`
would. Each module is one of the outcomes the loader has to classify — loaded, raised, collided,
raised halfway — because those are the cases where the tempting handling is the wrong one.
"""
