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

Open the address it prints and create an account. Everything works with a simulated printer,
parts and camera: edit the layout, give bins rules (or leave them wild), press **Start** and
watch the parts get sorted.

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

A Raspberry Pi **4 or 5** (4 GB or more) with 64-bit Raspberry Pi OS (Bookworm or Trixie). A
Pi 3B has too little memory for Klipper, the camera and the model together.

```bash
git clone https://github.com/ama24/screwpick.git ~/screwpick
~/screwpick/install-pi.sh --service      # --service: start screwpick at every boot
cp ~/screwpick/klipper/screwpick.cfg ~/printer_data/config/
echo "[include screwpick.cfg]" >> ~/printer_data/config/printer.cfg
```

**Everything is pinned**, so installing again in a year gives exactly the same programs:
- [requirements-pi.lock.txt](requirements-pi.lock.txt) lists every Python package with its
  exact version **and a checksum of the file**. It's installed with `--no-deps --require-hashes`,
  so pip installs nothing else and refuses a file that differs in any way.
- supreme-screw-sortering, the model and camera code, is checked out at a fixed commit
  (`SUPREME_COMMIT` in [install-pi.sh](install-pi.sh)).
- The script puts everything in the same Python environment as `screwrig` (`~/screwrig`),
  checks that OpenCV has ArUco and that PyTorch is the CPU build, and says what to do next.

To upgrade on purpose: run `python scripts/lock_pi.py` and change `SUPREME_COMMIT`, test it on
the Pi, then commit.

Then restart Klipper (`FIRMWARE_RESTART`), and adapt the *PICKER HARDWARE* part of
`screwpick.cfg` to your wiring. Check everything with:

```bash
cd ~/screwpick && screwpick check
```

Start it with `screwpick serve`, and open `http://<pi>:8080` on the PC or phone. The first
visit asks you to create an account (see [Login](#login)).

> **Note:** `screwrig capture` parks the bed by talking to the printer's USB port directly.
> With Klipper running, Klipper owns that port, so use screwpick (which goes through Klipper)
> while Klipper is running.

## First-time setup

On a real printer, **Sort**, **One pick** and **Automatic** are refused, also from the printer
menu, until every step of the **Setup** tab is checked:

1. **Printer:** Klipper answers, `screwpick.cfg` is included, the picker is mounted, the hotend is
   cold. Only questions, nothing moves.
2. **Homing:** press *Home now* and watch. Z must come down over the middle of the plate, not
   over a bin. Then confirm.
3. **Machine:** limits, travel height and photo position, editable on the page. *Show me*
   buttons drive to each position (at travel height) so you can check them.
4. **Tip height:** checks itself when you measure it (Calibrate tab).
5. **Picker:** *Grab* and *Release* without moving, then confirm.
6. **Camera to printer:** checks itself when the calibration's worst error is under 1 mm.
7. **Layout:** checks itself when Resync finds the bins and a plate without problems.

Until then the machine moves only when you ask it to in these steps. Jogging and the calibration
photos never home by themselves. Changing the machine values, a worse calibration or a layout
with problems un-checks the matching step again. In the simulator (`--sim`) the setup isn't
required.

The steps below are the same work in more detail.

1. **Choose your bed:** a baseplate of any size (5 × 5 cells is the most for an Ender 3), a
   tray as the plate (3 × 3, or 4 × 4 for big jobs), and as many bins as you like.
2. **Print the bins and the baseplate:** ready-made plates of every size are in
   [prints/](prints/) (16 small bins per Ender 3 plate, with codes, and the slim 2.5 mm
   baseplate). The web page's **Print bins** tab makes the same for any other layout. Bins
   come as two-part 3MF files, the bin (white) and its code (black). With one extruder, add a
   colour change at the rim height (21 mm for 3-unit bins).
3. Put the baseplate anywhere on the bed, the tray and the bins anywhere on it (turned any way),
   and a TPU sheet cut to size in the tray. To hold the baseplate down, print the version that
   **covers the whole bed** (Ender 3 or Prusa MK3, slim or standard, in 4 pieces that fit the
   printer) and clamp it with binder clips, or on the Prusa stick it onto a spare steel sheet.
4. Mount the picker and run `TOOL_PICKER`. Set `tool.offset` in
   [configs/machine.yaml](configs/machine.yaml) if the picker tip is not where the nozzle was.
5. Set `printer.photo_position`: where the head waits during photos, out of the picture.
6. **Camera calibration** (supreme-screw-sortering, tool 03): lay the ChArUco board **in the
   tray**, where the parts will lie. Its surface is the height everything is measured from.
   **Calibrate tab**: take a photo, then jog the tip onto the centre of 3 or more markers
   (spread over the bed), pressing **Tip is here** on each. Then **Fit**. Errors under 0.5 mm
   are good. **Check** moves the tip just above a marker to confirm.
7. **Layout tab → Resync with the camera**: one photo finds where the baseplate lies, which
   bins stand where and which way round, and makes the **biggest bin the plate**. To use
   another bin as the plate, give it the role *work*; Resync keeps your choice from then on.
   Bins taken off the bed keep their rules and get them back when they return. Give the other
   bins rules (*holds*) or leave them wild.
8. **Run tab**: pick a strategy and press **Start**, or use the printer menu.

## Models: upload over WiFi, keep the old ones

The **Models** tab (`http://<pi>:8080/#models`) is where models arrive. Drag a file onto the drop
zone, or click it to choose one. It takes:

| File | What it is |
|---|---|
| **package zip** (recommended) | model-trainer's whole `package` folder zipped: `best.pt`, `model_card.json`, `exports/…` |
| zipped NCNN folder | e.g. `best_ncnn_model.zip` (contains `model.ncnn.param`, `model.ncnn.bin`, `metadata.yaml`) |
| `.onnx` or `.pt` | a single weights file |

Every upload is checked:
- **what kind of model it is** (from the model card or the NCNN metadata);
- **its classes**, compared with the catalogue: a model knowing none of them is refused, and
  missing or extra classes are listed;
- **its image size and score**.

Each upload is stored in its own folder (`models/`). **Old models are never replaced**: press
**Use** on any of them to switch back. The switch happens without a restart, and if the new
model fails to load, the old one stays in use. **Test** runs a stored model on the last photo
and shows its outlines, without switching. The first model of each kind is put in use by itself.

When a package holds several formats, the Pi uses **NCNN** (fastest on its CPU), full precision
first. You can pick another one from the list.

Kinds of models and what they do here:

| Model task | Role | What you get |
|---|---|---|
| **segment** | finds parts (stage 1) | outlines, classes, positions, lengths: everything |
| **obb** / **detect** | finds parts (stage 1) | classes and positions only; lengths are not measured, so part-number rules send parts to *unsure* |
| **pose** | measures length (stage 2, optional) | keypoints for a more precise length |
| **classify** | re-checks classes (optional) | looks at a close-up of every part and corrects the class when it is at least `models.classifier_conf` sure |

If the card has a recommended confidence, it is used for stage 1 (`models.use_recommended_conf`).

> Only upload models you trust: `.pt` files are Python pickles and can contain code.

## Wild bins

A **wild** bin has no rules. The first part of a kind that no other bin takes **claims** a free
wild bin, and from then on that bin collects that kind. The Layout tab sets what "a kind" is for
all wild bins:

- **part number** (DIN912_M4x16): the default, one class and length per bin. Screws wait
  until their length is measured; nuts and washers (no length) go one class per bin.
- **class** (DIN912_M4): any length.
- **family** (DIN912).

- **A layout of only wild bins** sorts by itself, with no setup. New bins start out wild, and a
  wild bin becomes a rule bin as soon as you type rules into it.
- **Rules come first.** Bins with rules (and the unknown/unsure roles) are tried before wild bins.
  When every wild bin is taken, new kinds go to *overflow*, or stay on the plate.
- **Claims are kept** across runs and restarts (`data/wild_bins.json`). Each bin shows on the
  Run and Layout tabs what it collects.

**Ready to take out.** After every photo screwpick checks each filled bin. When nothing left on
the plate goes into that bin, the bin is marked:
- in soft green with a check mark in the Layout tab;
- in a notice at the top of every tab;
- with a line on the printer display.

Take the bin out, empty it, and press **Emptied**. A wild bin is then free for the next new
kind. If you throw in more of its parts, the mark goes away again. In a later version the
machine could move complete bins aside by itself.

**Bins the camera sees.** At the start of every run the camera reads the bin markers
(`run.scan_bins_on_start`):
- bins that moved are updated in the layout;
- markers the layout doesn't know join as new **wild** bins;
- bins the camera can't see (covered, removed) are not used in that run (`run.only_seen_bins`).

So you can put a new printed bin on the bed and it's used straight away.

## Login

The page needs a user name and password (`web.require_login`, on by default):

- The **first visit** creates the first account.
- The **Users** tab adds or removes users and changes your password. Every user can do
  everything.
- **Passwords** are stored only as salted, slow hashes in `data/users.json`, which is readable
  only by the Pi's user.
- **Sessions** last `web.session_days` (30) per browser. **Log out** ends one, and changing a
  password ends all sessions of that user.
- **Ten wrong passwords** from one address block it for 5 minutes.
- **Locked out?** On the Pi: `screwpick user add NAME` sets a new password, and
  `screwpick user list` / `remove` manage the accounts.

The page is plain HTTP, so it is meant for your home network. Logging in keeps others off the
page; it is not protection against someone already listening on your WiFi. The printer-menu
buttons never need a login: they come from Klipper on the Pi itself.

## One cycle

1. **Pick** the part chosen from the last photo and lift it.
2. **Photo:** go to the photo position, holding the part, and take the photo (about 1 s). The
   part is in the picker, so the photo also shows whether the pick worked: if the part is still
   on the plate, the pick failed and is tried again.
3. **Drop:** carry the part to its bin and release it. **Meanwhile the Pi analyses the photo**,
   so its 1–3 s of thinking hide behind the move.
4. The next part is chosen from that photo. Back to 1.

The Run tab shows how long the last cycle took and how long the machine had to wait for the Pi
after the drop. On a Pi 4 or Pi 5 that wait should be about 0. A Pi 3B (1 GB) is too small for
Klipper and the model together.

- **The photo position must keep the head, and the part it holds, outside the working area in
  the picture**, ideally off the plate.
- **Loose parts and quick bed moves:** after the photo the bed moves to the bin and back, and
  fast moves can nudge loose parts. `printer.accel` sets a gentler acceleration while sorting.
- **Turning it off:** `run.pipeline: false` photographs only with an empty picker (slower).

## Automatic mode

Press **Automatic** on the Run tab (or *Screw sorter → Automatic* on the printer). The machine
then runs by itself until you press Stop:

1. **Standby:** the bed stays all the way out at the photo position (`printer.photo_position`)
   and the motors are switched off, so it's quiet (`standby.motors_off`). Every minute
   (`standby.interval_s`) the camera looks at the plate. With the motors off nothing moves:
   the bed is still parked where the camera was calibrated.
2. **Parts seen:** it waits `standby.settle_s` seconds and looks again. Only if the parts lie in
   the same places, with no hand still throwing or spreading them, does it start.
3. **Sorting:** it homes (see below), finds the bins, and sorts the largest group first, until
   the plate is empty. Then it goes back to standby, and complete bins are marked ready.
4. **Stuck parts** (a clump that shaking can't spread) are left alone. It tries again as soon
   as they've been moved.

Automatic mode **stays on after a restart** or power cut: when `screwpick serve` starts, it
carries on. Stop (or STOP) switches it off. After `standby.max_errors` errors in a row (printer
off, camera unplugged…) it switches itself off and says why on the page.

## Homing

Two dangers when homing a sorter that a plain `G28` doesn't handle:

- **Bins on the bed.** An Ender 3 homes Z at the front-left corner, which is a bin. The tool would
  come down onto the bin's rim.
- **The picker tip may reach lower than the nozzle did.** The Z switch triggers on the gantry
  height, so with a longer tool the tip touches the plate before the switch triggers.

**What screwpick does** (`klipper/screwpick.cfg`, `[homing_override]`):

1. Lift Z by 30 mm first (more than the bins are tall).
2. Home X, then Y.
3. Move the tip over the **middle of the working area**: flat TPU, no bins. screwpick sets this
   spot from the layout before every homing.
4. Home Z there, then lift again.

**For the tip that reaches lower: raise the Z switch.** Slide the Ender's Z endstop bracket up
until the switch triggers with the picker tip **2–3 mm above the plate**. Then:

1. In `printer.cfg`, allow Z a little below 0: `[stepper_z] position_min: -5`.
2. Home, then on the **Calibrate tab → Tip height**: jog the tip down over the plate in 0.1 mm
   steps until a sheet of paper just drags, and press **Tip touches the plate**. From then on,
   every Z that screwpick uses counts from that point. Repeat this whenever the switch or the
   picker is moved.

The X and Y offset of the tip needs no measuring: the camera calibration already uses the tip
itself.

**Printing again with the hotend:** the raised switch would stop the nozzle above the bed.
Either slide the switch back down when you swap tools (re-level as usual), or print a spacer
that clips under the switch together with the picker swap, so the switch height comes with the
tool. Choose the picker's length so that the spacer is about the same for every swap.

**Motors off in standby:** Klipper forgets the homing when the motors switch off (and after its
own `idle_timeout`, 10 minutes by default). That's fine: the bed stays where it was for the
standby photos, and the machine homes again before the next sort.

## Strategies

| Strategy | What it does |
|---|---|
| **sort** | every part goes to the bin whose rule matches it best (part number > class > family > `*`), else to a wild bin |
| **clear** | everything into the bin with role *all* (or *overflow*), no sorting |
| **kit** | only the parts on `run.kit` (e.g. `{DIN912_M3x10: 4, DIN934_M3: 4}`) into the *kit* bin, then stop |

These rules apply to every strategy:
- **The largest group first** (`run.order: largest_group`): the part number with the most
  pieces on the plate is picked first, then the next. Within a group, parts with free space
  around them come first, then the most confident, then the nearest. `nearest` skips the
  grouping.
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
