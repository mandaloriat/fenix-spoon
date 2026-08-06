"""A third-party adapter that works: the case everything else is measured against."""

import numpy as np
from pydantic import BaseModel, Field

from fenixspoon.geometry import Geometry
from fenixspoon.solvers import register
from fenixspoon.solvers.base import SolverContext, SolverResult


@register
class SamplePlugin2D:
    """Deliberately trivial physics — what is under test is the loading, not the maths."""

    name = "sample.plugin2d"
    title = "Sample third-party adapter"
    description = "A constant field, so the test is about the registry rather than a solve."
    physics = "sample"
    geometry_types = ("domain2d",)
    availability = "mock"
    version = "1.0.0"
    requires = ("numpy",)
    deterministic = True

    class Params(BaseModel):
        resolution: int = Field(default=16, ge=4, le=64, description="Cells per side.")

    def solve(self, geometry: Geometry, params: Params, ctx: SolverContext) -> SolverResult:
        n = params.resolution
        xmin, ymin, xmax, ymax = geometry.bounds
        return SolverResult(
            kind="grid2d",
            data={
                "bounds": [xmin, ymin, xmax, ymax],
                "shape": [n, n],
                "fields": {"u": np.ones(n * n).tolist()},
                "mask": np.zeros(n * n, dtype=int).tolist(),
            },
            stats={"cells": float(n * n)},
        )
