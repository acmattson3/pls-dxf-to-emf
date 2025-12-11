# pls-dxf-to-emf
A simple Python setup to bypass AutoCAD and the UCP printer software to directly turn a DXF file into an EMF file ready to send to a PLS6.150D laser cutter. I want to uninstall AutoCAD at all costs.

## Quickstart
- Install Python deps (uses `ezdxf`): `pip install ezdxf`.
- Use the provided template job to keep the correct pens/comments: `PebbleFredBase.emf` + its matching calibration DXF `PebbleFredBase.dxf`.
- Convert any DXF that shares the same coordinate system/origin as the calibration file:
  - `python dxf_to_emf.py --input-dxf my_part.dxf --output-emf my_part.emf`
  - The script auto-solves the affine mapping (scale + offsets) from the calibration DXF to the template EMF, then applies it to your geometry.
- If you want to skip calibration and force your own mapping, provide all four overrides:
  - `python dxf_to_emf.py --input-dxf my_part.dxf --output-emf my_part.emf --scale-x ... --scale-y ... --offset-x ... --offset-y ...`
- Geometry supported: LINE, LWPOLYLINE, POLYLINE, CIRCLE, ARC, SPLINE (curves are discretized; tweak `--circle-segments` / `--arc-segments` / `--spline-segments`).
- Flags for picky consumers: `--preserve-header-counts` and `--zero-bounds` keep the header/record bounds and the nBytes/nRecords fields exactly like the template (some drivers prefer the zeros they emit).
- Units/orientation help: `--dxf-units in|mm|auto` (auto reads `$INSUNITS`, default mm). The transform auto-chooses the axis orientation/flip that best matches the template; override with `--swap-xy/--no-swap-xy/--flip-x/--flip-y` if needed.

Output EMF keeps every `EMR_COMMENT` from the template (pens/power tables, job metadata) and swaps in new `EMR_POLYLINE` records for your DXF vectors, updating header sizes and bounds. The resulting file drops straight into UCP/UCPX in place of one produced by AutoCAD.
