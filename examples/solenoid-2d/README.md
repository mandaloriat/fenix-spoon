# Solenoid 2D demo

A single self-contained HTML page showing the **`regions2d`** side of the toolkit: geometry
where the physics varies by *material*, not by a hole cut out of the domain.

The problem is a solenoid cross-section — an iron core between two coil sections carrying
opposite-signed current density (the two sides of one winding cut by the plane). The server
solves for the magnetic vector potential,

```
-div( (1/μ) grad A_z ) = J_z ,      B = ( dA_z/dy , -dA_z/dx )
```

and the page renders |B| with field lines drawn as contours of A_z.

## What it exercises

1. **`regions2d` geometry**: named regions with material properties (`mu_r`, `current_density`)
   over a background, sent as JSON.
2. **Region-tagged meshing**: `dolfinx.magnetostatics2d` gives every region its own Gmsh physical
   group, so the iron/air interface lands exactly on element edges rather than being staircased
   onto a raster. Switch the field selector to **μᵣ** to see the tagging.
3. **`mesh2d` rendering**: the actual FEM triangulation, drawn on canvas with per-triangle fill
   plus marching-triangle contours for the field lines.
4. **Solver interchangeability**: the dropdown lists every installed solver accepting `regions2d`.
   Without FEniCSx you get `mock.magnetostatics2d` (NumPy, Cartesian grid, harmonic-mean face
   reluctivities); with it you get the FEM adapter. Same protocol, same page.

## Run it

```bash
cd server && pip install -e . && uvicorn fenixspoon.main:app
```

then open <http://localhost:8000/demo/solenoid-2d/index.html>.

## Interacting

- **Drag the white handle** on the core's right edge to change the core width.
- **μᵣ slider** sweeps core permeability from 10 to 10⁴ — watch flux crowd into the iron as it rises.
- **field selector** switches between |B|, A_z and the material map.
