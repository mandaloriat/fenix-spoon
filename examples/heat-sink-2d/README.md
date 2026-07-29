# Heat sink 2D demo

A finned aluminium heat sink with a chip underneath, cooled by air moving over its surfaces.
This is the example where **the parameter form is generated, not written**: every control below
the geometry sliders is built from the `params_schema` the server publishes for the selected
solver.

## What it exercises

1. **Forms driven by `params_schema`.** `GET /api/v1/solvers` returns a JSON Schema per solver,
   produced by pydantic from the adapter's `Params` model. The page turns each property into a
   control — bounds become slider limits, `enum` becomes a `<select>`, `boolean` becomes a
   checkbox, and the field's `description` becomes its tooltip. Add a parameter to a solver and a
   control appears here without anyone touching HTML. This is the payoff of making
   `params_schema` part of the protocol rather than documentation.
2. **A solver that does *not* solve the background.** `regions2d` carries a `background`
   material, and `mock.magnetostatics2d` solves it as another region. `mock.heat2d` does the
   opposite: the region set *is* the solid, everything else is fluid, and the fluid enters only
   as a convective boundary condition on exposed faces. The result's `mask` marks the cells that
   were never solved and the viewer greys them out.

   That choice is the whole reason the example works. Model the air as a conducting region
   instead and the fins stop doing anything — air conducts at 0.026 W/(m·K) against aluminium's
   205, so heat cannot leave through them, and the chip climbs past 220 °C. Real heat sinks are
   analysed the same way: the fluid is a coefficient, not a mesh.
3. **A result worth looking at.** Fin count against chip temperature rise, measured through this
   page:

   | fins | 0 | 2 | 5 | 9 | 12 |
   |---|---|---|---|---|---|
   | rise over ambient | 83.0 K | 48.8 K | 30.4 K | 20.6 K | 16.7 K |

   Roughly 5× from bare base to twelve fins. Drag `h` from 25 to 400 W/(m²·K) — still air to
   forced air — and the 5-fin case drops from 30.4 K to 3.3 K.

## Run it

```bash
npm --prefix client install && npm --prefix client run build   # once
python -m uvicorn fenixspoon.main:app --app-dir server --reload
```

Then open <http://localhost:8000/demo/heat-sink-2d/index.html>.

The page needs the built widget bundles (`@fenix-spoon/client` and `@fenix-spoon/viewer`, served
from `/packages/`) and says so if they are missing.

## Notes for anyone borrowing the form generator

It is about sixty lines in the page and deliberately handles only the shapes pydantic emits.
Three things in it are less obvious than they look:

- **Set `min`/`max` before `value`.** A `<input type="range">` defaults to `max=100`, so
  assigning a larger default first silently clamps it — `resolution` (160) and `iterations`
  (3000) both became 100, and the demo then solved at a hundredth of the intended sweep count
  while looking entirely healthy.
- **JSON Schema has exclusive bounds; HTML inputs do not.** `relaxation` is `< 2.0`, and a
  slider that can reach exactly 2.0 submits a value the server rejects with a 422. Exclusive
  bounds get nudged inward by one step.
- **An unknown property type gets a text box, not nothing.** Skipping it would make the
  parameter unreachable with no indication why.
- **`default` is optional in JSON Schema, and a control must not invent one.** Every value a
  form puts in the request is submitted as though the user chose it, so a fabricated one
  silently overrides whatever the server would have applied. Measured with this solver's
  defaults stripped: sliders sat at the midpoint of their bounds and sent `resolution: 264`,
  `h: 5001`, `iterations: 20005`; empty number boxes read back as `0` (`Number('')` is zero,
  not `NaN`), which tripped `report_every`'s `minimum: 1` and failed the job with a 422; and
  `write_vtk` quietly turned itself off. A control with no default now renders unset — an
  empty number box, a blank `(server default)` option, an indeterminate checkbox — and is
  omitted from the request, so the server's own default applies and a genuinely required
  parameter fails with a message naming it.

Promoting this to a real `<fs-params>` widget is the obvious follow-up; it is kept in the page
for now so it can be read top to bottom.
