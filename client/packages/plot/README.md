# `@fenix-spoon/plot`

`<fs-plot>` — a custom element that draws the curves a Fenix Spoon result carries.

Protocol 1.5 put one-dimensional results on the wire: a surface `C_p`, a parameter sweep, a
convergence history. Until this package the protocol carried the shape and **every consumer
wrote its own plot**. No protocol change here — every number it draws has been available since
1.5.

```html
<fs-plot series="surface_cp" invert-y legend interactive></fs-plot>
<script type="module">
  import '@fenix-spoon/plot';
  document.querySelector('fs-plot').result = await job.wait();
</script>
```

## What it takes

| Property | Meaning |
|---|---|
| `result` | A `JobResult` of either kind. A `series1d` payload *is* the curve set; a field result carries its curves in `series`. The element does not branch on `kind` — the SDK's `resultSeries` does. |
| `curves` | A `Series1DData` directly, for a page that assembled its own — a sweep, or two solves compared. Wins over `result`. |
| `series` | Which curve set to draw, by name. Absent draws the first. |
| `available` | Every curve set the current result holds, for a picker. |

| Attribute | Meaning |
|---|---|
| `invert-y` | Draw the y axis downwards. |
| `legend` | Show the trace legend. |
| `interactive` | Report the nearest point on pointer movement, as `fs-plot-hover`. |
| `x-scale`, `y-scale` | `linear` (default) or `log`. |
| `x-label`, `y-label` | Override the caption the payload's units would produce. |

## Three decisions worth knowing

**`invert-y` is never inferred.** A pressure coefficient is conventionally drawn with suction
upwards, and it would be easy to notice a trace called `cp_upper` and flip the axis for you.
That is the class of guess [ADR 0001](../../../docs/adr/0001-explorable-viewer.md) records
`<fs-viewer>` refusing — it will not infer a vector field from a scalar one, or a physical
meaning for an integrated curve. A name is not a quantity. The convention is real, so the
attribute exists and the airfoil demo sets it; the page says so, not the widget.

**Per-trace abscissae are honoured.** The protocol lets a curve set share one `x` *and* lets
any trace bring its own, because an airfoil's upper and lower surface are not sampled alike
unless somebody resampled them. A trace whose abscissa does not line up is **dropped, not drawn
short** — half a curve is worse than none, because nothing about it looks wrong.

**One y axis, and it says so.** When the traces disagree about units — a magnitude and a phase
— the axis goes uncaptioned and each unit appears in the legend beside its own curve. Labelling
the axis with the first trace's unit would be a caption that is wrong for every other curve on
it. Two axes means two `<fs-plot>` elements.

## Why a separate package

It shares a canvas with `@fenix-spoon/viewer` and almost nothing else. The viewer needs
colormaps, a viewport, contour extraction and glyph lattices; a plot needs scales, round ticks
and a legend. Folding this in would make every page that shows a temperature map carry axis
code it never calls, against the one property the viewer was built around.

## Refusals

`plot.capabilities` reports each thing as `{available, reason}`, the shape `<fs-viewer>` uses.
A log axis on data that reaches zero is refused with the value it reached, so a page offering
the toggle can grey it out rather than draw a frame that silently relocated half the curve.

## Accessibility

The canvas carries `role="img"` and a description of what it holds — how many curves, their
names, the axis captions, whether the y axis is inverted — kept current as the data changes,
and set **before** any painting, so an environment that cannot rasterise still announces the
content. Hovering writes the point into an `aria-live` region.
