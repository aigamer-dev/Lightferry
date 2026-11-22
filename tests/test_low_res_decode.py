import os

import cv2
import numpy as np

from lightferry import channels, link, vision


def _camera_view(prof, px_per_module, blur):
    """A rendered code seen by a webcam at about px_per_module camera pixels per module, softened."""
    pk = [os.urandom(40)]
    img = channels.render_frame(pk, prof)
    h, w = img.shape[:2]
    n_modules = w / prof.module_px
    side = int(n_modules * px_per_module)
    small = cv2.resize(img, (side, side), interpolation=cv2.INTER_AREA)
    cam = np.full((720, 1280), 235, np.uint8)
    y, x = (720 - side) // 2, (1280 - side) // 2
    cam[y:y + side, x:x + side] = small
    return pk, cv2.GaussianBlur(cam, (0, 0), blur)


def test_tile_cells_upsample_low_res_codes_to_at_least_6_px_per_module():
    warped = np.full((325, 325), 200, np.uint8)  # ~4.3 px per module for the 76-module bootstrap frame
    ppms = {ppm for _, _, ppm in vision.tile_cells(warped, link.BOOTSTRAP.grid, 1)}
    assert ppms == {6}


def test_low_res_soft_code_decodes():
    ok = 0
    for blur in (0.6, 0.9, 1.2):
        pk, cam = _camera_view(link.BOOTSTRAP, 4.3, blur)
        if channels.parse_frame(cam, [link.BOOTSTRAP]) == pk:
            ok += 1
    assert ok >= 2, "a ~4.3 px/module code must decode for most mild blurs"
