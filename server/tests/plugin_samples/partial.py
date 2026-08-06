"""A module that registers one adapter and then raises.

The state the loader cannot undo — the registry has no removal — so the report has to be honest
about it rather than pretending the module did nothing.
"""

from fenixspoon.solvers import register


@register
class SamplePartial2D:
    name = "sample.partial2d"
    title = "Registered before the module fell over"
    physics = "sample"
    geometry_types = ("domain2d",)
    availability = "mock"


raise RuntimeError("the second adapter in this module does not import")
