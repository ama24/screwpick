"""sortering-pnp: a Raspberry Pi turns a Klipper 3D printer into a pick-and-place screw sorter.

Photo -> the trained model (class, outline, length) -> plate millimetres -> printer X/Y -> pick the
part and drop it into the Gridfinity bin its rule points to; a new photo after every pick.
"""

__version__ = "0.1.0"
