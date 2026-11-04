# CLAUDE.md — screwpick

screwpick runs on the Raspberry Pi next to Klipper and turns the printer into a pick-and-place
screw sorter. The camera and model come from
[supreme-screw-sortering](https://github.com/ama24/supreme-screw-sortering) (packages `screwsynth`
and `screwrig`), installed in the same environment. screwpick imports them; they never import
screwpick.

## Workflow
- All code lives on `main`. Each piece of work gets a short-lived branch (`feature/<name>`,
  `fix/<name>`), a pull request into `main`, and is deleted after merging. Never push to `main`
  directly.
- Run `pytest` and `ruff check .` (and `ruff format --check .`) before every commit.

## Design
- **Printer:** Klipper through Moonraker's HTTP API (`printer/moonraker.py`). The printer menu
  reaches the program through `action_call_remote_method("screwpick", ...)`, received over
  Moonraker's websocket (`MenuListener`). `printer/sim.py` is a full simulator (printer, parts,
  camera) used by the tests and by `screwpick serve --sim`.
- **Motion safety:** every move goes through `printer/motion.py`, which applies the tool offset
  and the soft limits. `preflight()` refuses to move unless Klipper is ready, not printing, the
  hotend is cold, the axes are homed, and `_SCREWPICK_TOOL.tool == "picker"`.
- **Frames:** plate frame = screwsynth's (mm, z = 0 on the TPU top). Printer frame = G-code X/Y
  of the bed. The plate is fixed to the bed, so one 2D similarity (`transform.py`, fitted from
  marker/tip pairs) maps one to the other. Z values in screwpick are printer Z of the TIP; the
  plate top is at `layout.plate_thickness_mm`.
- **Layout:** Gridfinity cells (42 mm), working area plus bins (`layout.py`). Bins carry ArUco
  DICT_4X4_250 markers, ids 100–249 (ids below 100 are left to the calibration). A marker's top
  edge = the bin's back (+Y). Bin dimensions are shared in `binspec.py` by the layout and the
  printable bins.
- **Rules:** `rules.py` (most specific rule wins; roles unknown, unsure, overflow, all, kit).
  Strategies are in `strategies.py` (sort, clear, kit). Add one by extending `Planner._decide`
  and `STRATEGIES`.
- **Models** are uploaded through the web page into a store (`models.py`, folder `paths.models`).
  Each upload gets its own folder with `screwpick.json`; uploads are never overwritten. One
  model is active per role (`active.json`): stage1 (segment/obb/detect), stage2 (pose),
  classifier (classify). `analysis.py` runs them, reusing screwsynth's measurement. Switching
  happens while idle and rolls back if loading fails. A `.pt` is a zip archive inside: only
  `.zip` uploads are unpacked.
- **The model** runs on the full calibrated ROI at its trained scale; parts outside the working
  area are filtered out afterwards. Never crop the ROI to the working area: that changes the
  scale the model sees.

## Conventions
- Lengths in mm, speeds in mm/min, angles in degrees.
- Configuration: `configs/machine.yaml`, loaded into dataclasses (`config.py`); unknown keys are
  errors. New settings get a default and a `#` comment.
- Type hints, logging instead of print (except the CLI's user messages), no hard-coded paths.
  Tests need no hardware: use the simulator or fakes.
- The web page is one static file (`web/index.html`, plain JavaScript) plus a JSON API in
  `web/server.py` (standard library only).
