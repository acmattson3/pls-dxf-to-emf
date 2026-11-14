# DXF to EMF for ULS PLS6.150D

This Python prototype converts DXF vector geometry into a ULS-style EMF by replacing geometry in an EMF template created by the ULS software. The output keeps the template's machine and job settings. It is intended for a PLS6.150D workflow; it has not been validated with a physical cut or engraving.

## Setup

### Requirements

- Python 3.10 or newer
- The ULS converter installed on a Windows workstation for the target laser
- A ULS-generated template EMF and the exact calibration DXF used to create it

Install the Python dependencies:

```bash
python -m pip install ezdxf Pillow
```

### Create a template and calibration pair

The template files are machine-specific and are **not included in this repository**. The filenames `PebbleFredBase.emf` and `PebbleFredBase.dxf` are historical defaults; neither file is present in this checkout.

On the workstation connected to the laser:

1. Confirm the ULS software and PLS6.150D driver are installed.
2. Create a small calibration DXF with known vector geometry, units, and origin. Use geometry that spans the area you want the calibration to represent.
3. Convert that DXF with the ULS software configured for the target machine. Save the resulting EMF and keep it with the exact source DXF.
4. Use that EMF as the template. Its pen, material, and other ULS settings will be carried into converted jobs.

On the PLS6.150D workstation used during development, the installed command-line converter generated the EMF with this PowerShell command (replace both calibration paths as needed):

```powershell
& 'C:\Program Files (x86)\ULS\DXF2ULS.exe' `
  '34' `
  '[C:\path\calibration.dxf]' `
  '[C:\path\calibration.emf]' `
  '[C:\ProgramData\ULSDAT\ConvertDXF.las]' `
  '0' '0' '0' '1.0' '0'
```

The bracket characters around the three file arguments are required by this converter's command-line parser. Model ID `34` and the settings-file path are specific to the PLS6.150D installation inspected; confirm the model and settings file on other workstations.

Keep the calibration DXF and template EMF paired. The automatic transform maps the calibration drawing's bounds to the template's vector bounds. A mismatched pair can produce incorrect scale or placement.

## Convert a DXF

Pass the template and its matching calibration DXF explicitly:

```bash
python dxf_to_emf.py \
  --template-emf "/path/to/calibration.emf" \
  --calibration-dxf "/path/to/calibration.dxf" \
  --input-dxf "/path/to/part.dxf" \
  --output-emf "/path/to/part.emf"
```

The program's default paths point to `PebbleFredBase.emf` and `PebbleFredBase.dxf` in the current directory. Since those files are not included here, either supply your own pair with the options above or put the files at those default paths.

For drawings without `$INSUNITS`, the script assumes millimeters. Set units explicitly when needed, for example `--dxf-units in` or `--dxf-units mm`. Automatic unit detection reads the DXF `$INSUNITS` header when present.

If you do not have a calibration DXF, provide all four transform values to skip calibration: `--scale-x SX --scale-y SY --offset-x OX --offset-y OY`. Use values measured for your template and machine. `--swap-xy`, `--no-swap-xy`, `--flip-x`, and `--flip-y` control orientation during automatic calibration. When equally good orientations fit the bounds, the solver prefers the normal X/Y axes and flips Y, matching the PLS6 driver's observed DXF-to-EMF mapping.

After conversion, open the EMF in UCP/UCPX and check the job size, placement, and active pen settings before using it. This repository's output has not yet been physically cut or engraved.

## Geometry and output

The script directly handles `LINE`, `LWPOLYLINE`, `CIRCLE`, `ARC`, and `SPLINE`. Legacy `POLYLINE` entities and additional entities are rendered through ezdxf; supported examples include inserts, text, mtext, ellipses, hatch outlines, solids, and 3D face outlines. Curves become polylines; adjust `--circle-segments`, `--arc-segments`, and `--spline-segments` to change their sampling.

The output retains the template's ULS comments and settings, updates the job name to the output filename by default, removes recognized template geometry (standard polylines, native `CIRCLE(...)` / `ARC(...)` / `ELLIPSE(...)` comments, and private type-123 bulge paths), and inserts new `EMR_POLYLINE` vectors. It updates header byte/record counts and bounds by default.

For compatibility with particular consumers:

- `--preserve-header-counts` leaves the template's `nBytes` and `nRecords` header fields unchanged. The template may contain zero or stale counts.
- `--zero-bounds` sets the EMF header bounds and generated polyline record bounds to zero.

The script does not convert raster image pixels to vector paths and does not map DXF colors or lineweights to ULS pens. Generated geometry uses the template's active pen settings. Multi-pen jobs need separate validation.

## Development and validation

The implementation was developed by comparing this script with the installed ULS DXF converter and its EMF output. Ghidra was used to inspect the converter's command-line arguments, model selection, and emitted record behavior; no driver code was incorporated into this project.

Ten synthetic DXFs were passed directly to the ULS converter on a PLS6.150D workstation: basic vectors, bulged and legacy polylines, splines and ellipses, text, blocks, hatches and filled shapes, dimensions and leaders, 3D geometry, and degenerate geometry. Each produced a nonempty EMF. The driver outputs showed standard polylines, native `CIRCLE(...)`, `ARC(...)`, and `ELLIPSE(...)` comments, and a private type-123 bulge-path record. In these fixtures, the driver omitted text and hatch outlines that our script can render.

Side-by-side previews were reconstructed from EMF geometry records because ordinary viewers do not reliably render these ULS-specific EMFs. They are not UCP screenshots. Validation so far covers synthetic fixtures, EMF structure parsing, PLS6 metadata preservation, and regression checks. No print jobs were queued and no physical laser job was validated. Test DXFs and preview images are not included in the repository.

## Regression checks

```bash
python -m unittest discover -s tests -v
```
