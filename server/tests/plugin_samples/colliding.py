"""A third-party adapter that claims a name this repository already ships.

The plugin author's bug, and it must not become the operator's outage. The incumbent keeps the
name because `register` refuses the second claim, and the loader records the refusal.
"""

from fenixspoon.solvers import register


@register
class Impostor:
    name = "mock.laplace2d"
    title = "Not the potential flow adapter"
    physics = "sample"
    geometry_types = ("domain2d",)
    availability = "mock"
