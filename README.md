# screwpick

A Raspberry Pi turns a Klipper 3D printer (Ender 3, Prusa MK3, …) into a pick-and-place
**screw sorter**:

1. The camera photographs the parts thrown into the working area.
2. The [supreme-screw-sortering](https://github.com/ama24/supreme-screw-sortering) model finds
   every part: class (DIN912_M4, nut, washer, …), outline and length.
3. screwpick converts each position into printer X/Y, picks one part with a suction cup or
   gripper, and drops it into the **Gridfinity bin** whose rules say it belongs there.
4. A new photo after every pick, until the working area is empty.

Everything runs on the Pi next to Klipper. You control it from a web page
(`http://<pi>:8080`) or from the **printer's own display menu**. There is no OctoPrint: it talks
to Klipper through Moonraker, Klipper's built-in web API.

```
 ┌──┬──┬──┬──┬──┐   Gridfinity bins around the working area, each with a printed
 │B │B │B │B │B │   marker (a small black-and-white code) on a shelf: the camera
 ├──┼──┴──┴──┼──┤   finds every bin by itself, so you can rearrange them freely.
 │B │        │B │
 ├──┤  work  ├──┤   You choose what each bin holds: a part number (DIN912_M4x16),
 │B │  area  │B │   a class (DIN912_M4), a family (DIN912), wildcards, or a role:
 ├──┤        ├──┤   unknown parts, unsure, overflow, kit, everything.
 │B │        │B │
 ├──┼──┬──┬──┼──┤
 │B │B │B │B │B │
 └──┴──┴──┴──┴──┘
```

## Try it on a PC first (no printer)

```
pip install -e ".[dev]"
screwpick serve --sim
```

Open the address it prints. Everything works with a simulated printer, parts and camera: edit
the layout, give bins their rules, press **Start** and watch the parts get sorted.

## What you need

| Part | Notes |
|---|---|
| Printer running **Klipper + Moonraker** on the Pi | the usual Klipper install (KIAUH, MainsailOS); OctoPrint can stay or go |
| Pi Camera Module 3 over the bed | the same one, and the same calibration, as supreme-screw-sortering |
| A **picker** on the toolhead | vacuum cup, gripper, or both (see below); swappable with the hotend |
| Gridfinity **baseplate** + **bins** | printed from the web page's *Print bins* tab |
| A matte TPU plate in the working area | blue or green, as for the camera |

### Picker

- **Vacuum** (default): a small 12/24 V pump on the part-cooling-fan connector, with a
  spring-loaded suction cup. Grab = fan on, release = fan off. Works on every material (brass,
  stainless, aluminium, nylon), not only steel.
- **Gripper**: a servo on a free pin (the BLTouch servo pin works well). It can also turn (a
  second servo) so the fingers close across the part.
- **Both**: fingers with a suction cup between them.

The macros live in [klipper/screwpick.cfg](klipper/screwpick.cfg), so the Python code does not
care how the picker is wired.

### Swappable tool

Klipper remembers which tool is mounted. Run `TOOL_PICKER` (or *Screw sorter → Picker mounted* in
the printer menu) after mounting the picker, and `TOOL_HOTEND` after putting the hotend back. The
sorter refuses to move unless the picker is mounted and the hotend is cold. `TOOL_PICKER` also
switches the heaters off.

## Install on the Pi

screwpick uses the model and camera code from supreme-screw-sortering, so install it into the
**same virtual environment** as `screwrig`:

```bash
source ~/screwrig/bin/activate               # the venv screwrig runs in
cd ~ && git clone https://github.com/ama24/screwpick.git
cd ~/screwpick && pip install -r requirements-pi.txt
cp ~/screwpick/klipper/screwpick.cfg ~/printer_data/config/
echo "[include screwpick.cfg]" >> ~/printer_data/config/printer.cfg
```

Then restart Klipper (`FIRMWARE_RESTART`), and adapt the *PICKER HARDWARE* part of
`screwpick.cfg` to your wiring. Check everything with:

```bash
cd ~/screwpick && screwpick check
```

Start it with `screwpick serve`, and open `http://<pi>:8080` on the PC or phone.

> **Note:** `screwrig capture` parks the bed by talking to the printer's USB port directly.
> With Klipper running, Klipper owns that port, so use screwpick (which goes through Klipper)
> while Klipper is running.

## First-time setup

1. **Layout tab**: set the grid (5 x 5 cells fit an Ender 3 bed), drag out the working area, and
   add bins. Give each bin its rules (*holds*) or a role. Save.
2. **Print bins tab**: download `screwpick_print.zip` (every bin, the baseplate, paper labels,
   printing notes). Bins come as two-part 3MF files: the bin (white) and its marker (black).
   With one extruder, add a colour change at the rim height (21 mm for 3-unit bins).
3. Put the baseplate on the bed, the TPU plate in the cut-out, and the bins around it.
4. Mount the picker and run `TOOL_PICKER`. Set `tool.offset` in
   [configs/machine.yaml](configs/machine.yaml) if the picker tip is not where the nozzle was.
5. Set `printer.photo_position`: where the head waits during photos, out of the picture.
6. **Calibrate tab**: take a photo, then jog the tip onto the centre of 3 or more markers
   (spread over the bed), pressing **Tip is here** on each. Then **Fit**. Errors under 0.5 mm
   are good. **Check** moves the tip just above a marker to confirm.
7. **Layout tab → Find bins with the camera**: every bin moves to where its marker really is.
8. **Run tab**: pick a strategy and press **Start**, or use the printer menu.

## Strategies

| Strategy | What it does |
|---|---|
| **sort** | every part goes to the bin whose rule matches it best: part number > class > family > `*` |
| **clear** | everything into the bin with role *all* (or *overflow*), no sorting |
| **kit** | only the parts on `run.kit` (e.g. `{DIN912_M3x10: 4, DIN934_M3: 4}`) into the *kit* bin, then stop |

These rules apply to every strategy:
- Parts with free space around them are picked first, then the most confident, then the
  nearest.
- Detections below `run.min_score` go to the *unsure* bin.
- UNKNOWN parts go to the *unknown* bin.
- A part whose length decides its bin, but which can't be measured yet (tilted, covered), waits.
  After a few bed shakes it goes to *unsure*.
- When nothing can be picked, the bed is shaken (`run.shake`) to spread the clump. After
  `max_in_a_row` shakes, the sorter pauses and asks for a hand: spread the parts and press Resume.
- A pick that leaves the part in place is retried. After `max_failed_picks` tries at the same
  spot, that part is left alone.

## Printer menu

`screwpick.cfg` adds **Screw sorter** to the printer's display menu: *Sort*, *Clear plate*,
*Pick kit*, *One pick*, *Pause*, *Resume*, *Stop*, *Picker mounted*, *Hotend mounted*. You can also
type the commands in any Klipper console: `SCREWPICK CMD=start STRATEGY=sort`.

## How it works

- **Camera → plate mm:** supreme-screw-sortering's calibration and model. The model always runs
  on the full calibrated image, at the scale it was trained on. screwpick then ignores
  everything outside the working area, including the parts already in bins.
- **Plate mm → printer X/Y:** one 2D transform (rotation, scale, shift) fitted from the marker
  points you record in the Calibrate tab. The plate is fixed to the bed, so this works for
  moving-bed printers too. Bed markers are read at their real height (parallax-correct).
- **Bin markers:** ArUco 4x4 codes, ids 100–249, so they can't be confused with the calibration
  board. Each code's top edge points to the back of its bin, which gives both the bin's position
  and its rotation.
- **Safety:** every move is checked against the soft limits in `machine.yaml`. Moves happen at
  `travel_z`, the descent is slow, and the program refuses to run while the printer is
  printing, hot, unhomed or carrying the hotend. **STOP NOW** on the web page sends Klipper's
  emergency stop.

## Development

```
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check .
```

Code: [src/screwpick/](src/screwpick/). `runner.py` is the loop, `strategies.py` and `rules.py`
decide, `printer/` talks to Klipper (or the simulator), `vision.py` wraps the model, and
`printables/` makes the bins.
