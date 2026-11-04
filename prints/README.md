# Ready-made prints

Every screwpick bin size as full build plates for a 220 × 220 mm bed (Ender 3), plus the
baseplate and paper labels. Each bin carries its own black-and-white code on a shelf at the
back. The camera reads it to find the bin and to know its size.

## Bin sizes

The code number tells screwpick the size, so a new bin can simply be put on the bed: the
camera recognises it and knows how tall its rim is.

| Codes | Size | Use | Plates (bins per plate) |
|---|---|---|---|
| 100–139 | 1 × 1 cell, 3 units (21 mm) | **sorting**, on the printer: one part number per bin | [100–115](bins/1x1x3u/bins_1x1x3u_100-115.3mf), [116–131](bins/1x1x3u/bins_1x1x3u_116-131.3mf), [132–139](bins/1x1x3u/bins_1x1x3u_132-139.3mf) (16) |
| 140–159 | 1 × 2 cells, 3 units | **sorting**: long screws, threaded rod, brackets | [140–147](bins/1x2x3u/bins_1x2x3u_140-147.3mf), [148–155](bins/1x2x3u/bins_1x2x3u_148-155.3mf), [156–159](bins/1x2x3u/bins_1x2x3u_156-159.3mf) (8) |
| 160–179 | 1 × 1 cell, 6 units (42 mm) | **storage**: small parts | [160–175](bins/1x1x6u/bins_1x1x6u_160-175.3mf), [176–179](bins/1x1x6u/bins_1x1x6u_176-179.3mf) (16) |
| 180–199 | 1 × 2 cells, 6 units | **storage**: long parts | [180–187](bins/1x2x6u/bins_1x2x6u_180-187.3mf), [188–195](bins/1x2x6u/bins_1x2x6u_188-195.3mf), [196–199](bins/1x2x6u/bins_1x2x6u_196-199.3mf) (8) |
| 200–219 | 2 × 2 cells, 6 units | **storage**: large quantities | [200–203](bins/2x2x6u/bins_2x2x6u_200-203.3mf) … [216–219](bins/2x2x6u/bins_2x2x6u_216-219.3mf) (4) |
| 220–229 | 3 × 3 cells, 3 units: a tray | **the plate**: parts are thrown in here | [220](bins/3x3x3u/bins_3x3x3u_220-220.3mf), [221](bins/3x3x3u/bins_3x3x3u_221-221.3mf) (1) |
| 230–239 | 4 × 4 cells, 3 units: a tray | **the plate** for big jobs (167 mm: fits the bed alone) | [230](bins/4x4x3u/bins_4x4x3u_230-230.3mf), [231](bins/4x4x3u/bins_4x4x3u_231-231.3mf) (1) |
| 240–249 | any other size | set by hand in the Layout tab, printed from the web page | |

A 1 × 1 bin is 41.5 mm square, inside about 39 mm: screws up to about 35 mm long. Longer parts
(threaded rod up to 80 mm, 40-series brackets) go in 1 × 2 bins.

**To start:** the 3 × 3 tray 220 (the plate) and one plate of 1 × 1 sorting bins (100–115):
together they fill the default 5 × 5 layout. Add 1 × 2 bins for long parts. For big jobs use
the 4 × 4 tray. **Resync** makes the biggest bin the plate, unless you choose another.

**In the tray** lay a TPU sheet cut to its floor (about 121 × 107 mm for 3 × 3, behind the
shelf): parts don't bounce and the camera sees them well. Do the camera calibration with the
board lying in the tray, at the height the parts will lie.

## Printing

- **Bins are 3MF files in two parts:** *bin* and *marker*.
  - **Multi-colour printer** (AMS, MMU…): give *marker* black and *bin* white or any light
    colour.
  - **One extruder:** print in white and add a colour change at the rim: **21 mm** for 3-unit
    bins, **42 mm** for 6-unit bins. All bins on a plate are the same height, so one change
    makes every code black.
- **Settings:** PLA, 0.2 mm layers, 2 walls, 15 % infill, no supports. The shelf under the code
  is a 45° slope and prints without supports.
- **The codes must stay sharp:** black cells of 1.6 mm. Don't scale the files.
- **Baseplate**, a plain grid: 5 × 5 cells is 210 × 210 mm, the most an Ender 3 bed takes. Lay
  it anywhere on the bed; **Resync** finds it.
  - [slim, 2.5 mm](baseplate/baseplate_5x5_slim.stl): the thinnest that still holds the bins;
  - [standard, 4.65 mm](baseplate/baseplate_5x5_standard.stl).

  The bins stand on the bed in both. Other sizes (e.g. 4 × 5) come from the Print bins tab.
- **Baseplate clamped to the whole bed:** the same 5 × 5 grid with a flat border out to the
  bed's edges, so it's held down like a glass plate. Each is cut on cell lines into 4 pieces that
  fit the printer (no socket is cut), with notches at the corners for the bed screws, and a
  README saying how to lay them out.

  | Printer | Bed | Slim (2.5 mm) | Standard (4.65 mm) |
  |---|---|---|---|
  | Creality Ender 3 / Pro / V2 | 235 × 235 mm, binder clips on every outer edge | [ender3/](baseplate/ender3/) `…_slim_*.stl` | [ender3/](baseplate/ender3/) `…_standard_*.stl` |
  | Prusa MK3 / MK3S | steel sheet 254 × 241 mm; back 10 mm free for the pins | [mk3/](baseplate/mk3/) `…_slim_*.stl` | [mk3/](baseplate/mk3/) `…_standard_*.stl` |

  On the Prusa, a **spare steel sheet** works best: stick the pieces onto it with double-sided
  tape, and the sheet snaps onto the magnetic bed in the same place every time. Binder clips also
  work, but their jaws under the sheet keep it from lying flat. Measure your bed if it isn't
  stock: the sizes are in `src/screwpick/printables/beds.py`.
- **Holders for the printer's top bar** ([mounts/](mounts/)), both fixed with M5 screws and
  T-nuts in the 2020 top bar of an Ender 3:
  - [screen_holder.stl](mounts/screen_holder.stl): a thin L for the 7" Waveshare HDMI LCD (H)
    (M2 holes 175 × 99 mm), leaning back 20° towards you; the Raspberry Pi 4 screws to its
    back (M2.5, 49 × 58 mm). Put it on one side of the bar.
  - [camera_arm.stl](mounts/camera_arm.stl): at the bar's centre, reaching 135 mm forward and
    60 mm down, so the Camera Module 3 looks straight down at the bed's centre with the bed
    all the way out (where a print ends).

  Your printer's sizes differ? Measure the reach and the bar's height above the plate and
  make your own in the web page (Print bins → Holders for the top bar) or with
  `screwpick print mounts --reach … --bar-height …`. See [mounts/MOUNTS.txt](mounts/MOUNTS.txt).
- **Labels** ([labels/](labels/)): paper codes for plain bins. Print at 100 % and glue them on
  the shelf.

For another layout (another baseplate size or more bins) the web page's **Print bins** tab
makes the matching baseplate and bins.

These files are made by `screwpick print catalogue --out prints`. A test checks that they
match the code.
