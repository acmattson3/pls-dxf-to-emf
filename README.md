# pls-dxf-to-emf
A simple Python setup to bypass AutoCAD and the UCP printer software to directly turn a DXF file into an EMF file ready to send to a PLS6.150D laser cutter. I want to uninstall AutoCAD at all costs.

## Quickstart
- Install Python deps: `pip install ezdxf Pillow`.
- Use the provided template job to keep the correct pens/comments: `PebbleFredBase.emf` + its matching calibration DXF `PebbleFredBase.dxf`.
- Convert any DXF that shares the same coordinate system/origin as the calibration file:
  - `python dxf_to_emf.py --input-dxf my_part.dxf --output-emf my_part.emf`
  - The script auto-solves the affine mapping (scale + offsets) from the calibration DXF to the template EMF, then applies it to your geometry.
- If you want to skip calibration and force your own mapping, provide all four overrides:
  - `python dxf_to_emf.py --input-dxf my_part.dxf --output-emf my_part.emf --scale-x ... --scale-y ... --offset-x ... --offset-y ...`
- Geometry support includes LINE, LWPOLYLINE, POLYLINE, CIRCLE, ARC, SPLINE, plus additional entities rendered through ezdxf (including INSERT, TEXT, MTEXT, ELLIPSE, HATCH outlines, and 3D face outlines). Curves are discretized; tweak `--circle-segments` / `--arc-segments` / `--spline-segments` for the directly handled curves.
- Flags for picky consumers: `--preserve-header-counts` and `--zero-bounds` keep the header/record bounds and the nBytes/nRecords fields exactly like the template (some drivers prefer the zeros they emit).
- Units/orientation help: `--dxf-units auto|mm|in|cm|m|ft|yd|mi|km|mil` (auto reads `$INSUNITS`, including standard DXF unit codes; unitless or unknown defaults to mm). The transform auto-chooses the axis orientation/flip that best matches the template; override with `--swap-xy/--no-swap-xy/--flip-x/--flip-y` if needed.

Calibration estimates scale and offsets from the overall DXF and template geometry bounds, then picks the axis orientation with the closest X/Y scales. This works best when the calibration drawing spans the same intended work area as the template. For unusual or asymmetric calibration drawings, check the reported orientation and use the axis flags or explicit transform overrides if needed.

Output EMF keeps the template's ULS settings and metadata comments, removes its standard polylines, native `CIRCLE(...)` / `ARC(...)` / `ELLIPSE(...)` geometry comments, and private type-123 bulge-path records, then inserts new `EMR_POLYLINE` vectors. It updates header sizes and bounds and is intended to load in UCP/UCPX like a job produced through the original workflow.

Raster image pixels are not converted into vector paths. DXF entity colors and lineweights are also not yet mapped to separate ULS pens; generated geometry inherits the template's active pen. Multi-pen jobs need validation against an original driver output before use.
