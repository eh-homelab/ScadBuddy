from __future__ import annotations

import numpy as np
import pytest

from scadbuddy.render.plate import (
    DEFAULT_PLATE,
    PRIME_TOWER_SIDE,
    PlateFitError,
    place_on_plate,
    plate_for,
)

# The keychain of library file 62: 176 x 46 mm in XY, 3 mm tall.
KEYCHAIN = np.array([[-88.0, -23.0, -1.5], [88.0, 23.0, 1.5]])


def _bounds(width: float, depth: float, height: float = 3.0) -> np.ndarray:
    return np.array([[0.0, 0.0, 0.0], [width, depth, height]])


class TestPlateLookup:
    def test_unknown_model_falls_back_to_256_square(self) -> None:
        assert plate_for(None) is DEFAULT_PLATE
        assert plate_for("Elegoo Neptune") is DEFAULT_PLATE
        assert DEFAULT_PLATE.size == (256.0, 256.0)

    @pytest.mark.parametrize(
        ("name", "model"),
        [
            ("H2C", "Bambu Lab H2C"),
            ("h2c", "Bambu Lab H2C"),
            ("Bambu Lab H2C", "Bambu Lab H2C"),
            ("Bambu Lab H2C 0.4 nozzle", "Bambu Lab H2C"),
            ("Bambu Lab H2C 0.2 nozzle", "Bambu Lab H2C"),
            ("X1C", "Bambu Lab X1 Carbon"),
            ("P1S", "Bambu Lab P1S"),
            ("A1 mini", "Bambu Lab A1 mini"),
            ("A1M", "Bambu Lab A1 mini"),
            ("H2DP", "Bambu Lab H2D Pro"),
        ],
    )
    def test_model_codes_and_preset_names_both_resolve(self, name: str, model: str) -> None:
        assert plate_for(name).model == model

    def test_h2c_usable_area_is_where_both_extruders_reach(self) -> None:
        """The H2C's bed is 330x320 but extruder 2 cannot reach x < 25."""
        plate = plate_for("H2C")
        assert plate.size == (330.0, 320.0)
        assert plate.extruders == 2
        assert (plate.usable.min_x, plate.usable.max_x) == (25.0, 325.0)
        assert (plate.usable.min_y, plate.usable.max_y) == (0.0, 320.0)
        assert plate.usable.centre == (175.0, 160.0)

    def test_single_extruder_usable_area_is_the_whole_bed(self) -> None:
        plate = plate_for("X1C")
        assert plate.extruders == 1
        assert plate.usable.centre == (128.0, 128.0)

    def test_x1c_carries_the_filament_cutter_exclusion(self) -> None:
        assert plate_for("X1C").exclusions
        assert not plate_for("H2C").exclusions


class TestPlacement:
    def test_keychain_centres_on_the_h2c_reachable_area_not_the_bed(self) -> None:
        """128,128 is the old hard-coded 256-plate centre; the H2C's is 175,160."""
        placement = place_on_plate(KEYCHAIN, plate_for("H2C"))
        assert placement.offset[:2] == (175.0, 160.0)
        assert placement.offset[2] == 1.5

    def test_the_object_sits_on_the_bed(self) -> None:
        placement = place_on_plate(_bounds(10.0, 10.0) + np.array([0.0, 0.0, 7.0]), DEFAULT_PLATE)
        assert placement.offset[2] == -7.0

    def test_tower_clears_the_object_and_every_extruder_reaches_it(self) -> None:
        plate = plate_for("H2C")
        placement = place_on_plate(KEYCHAIN, plate)
        x, y = placement.tower
        assert x >= plate.usable.min_x
        assert x + PRIME_TOWER_SIDE <= plate.usable.max_x
        assert y >= plate.usable.min_y
        assert y + PRIME_TOWER_SIDE <= plate.usable.max_y
        # The failing file put the tower at x=15..75, inside extruder 2's dead zone.
        assert x >= 25.0

    def test_tower_does_not_overlap_the_object(self) -> None:
        for model in ("H2C", "X1C", "A1", "H2D"):
            plate = plate_for(model)
            placement = place_on_plate(KEYCHAIN, plate)
            cx, cy = placement.offset[0], placement.offset[1]
            object_box = (cx - 88.0, cy - 23.0, cx + 88.0, cy + 23.0)
            tower = (
                placement.tower[0],
                placement.tower[1],
                placement.tower[0] + PRIME_TOWER_SIDE,
                placement.tower[1] + PRIME_TOWER_SIDE,
            )
            overlaps = (
                tower[0] < object_box[2]
                and tower[2] > object_box[0]
                and tower[1] < object_box[3]
                and tower[3] > object_box[1]
            )
            assert not overlaps, model

    def test_tower_avoids_the_filament_cutter_cutout(self) -> None:
        plate = plate_for("P1S")
        placement = place_on_plate(_bounds(60.0, 60.0), plate)
        x, y = placement.tower
        cutout = plate.exclusions[0]
        assert not (x < cutout.max_x and y < cutout.max_y)

    def test_a_large_object_moves_over_to_leave_the_tower_room(self) -> None:
        plate = plate_for("H2C")
        placement = place_on_plate(_bounds(280.0, 180.0), plate)
        assert placement.offset[1] != plate.usable.centre[1]

    def test_an_object_wider_than_the_plate_is_refused_naming_the_axis(self) -> None:
        with pytest.raises(PlateFitError, match=r"340.*on X.*300"):
            place_on_plate(_bounds(340.0, 40.0), plate_for("H2C"))

    def test_an_object_that_leaves_no_room_for_the_tower_is_refused(self) -> None:
        with pytest.raises(PlateFitError, match="prime tower"):
            place_on_plate(_bounds(295.0, 315.0), plate_for("H2C"))

    def test_a_single_colour_object_needs_no_tower(self) -> None:
        placement = place_on_plate(_bounds(295.0, 315.0), plate_for("H2C"), tower=False)
        assert placement.tower is None
