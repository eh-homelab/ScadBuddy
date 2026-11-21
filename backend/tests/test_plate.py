from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from scadbuddy.render.plate import (
    DEFAULT_PLATE,
    EDGE_MARGIN,
    PRIME_TOWER_AUTO_BRIM,
    PRIME_TOWER_BRIM,
    TOWER_CLEARANCE,
    Placement,
    PlateFitError,
    PlateGeometry,
    Rect,
    TowerSettings,
    bed_types_for,
    fit_problem,
    min_tower_depth,
    place_on_plate,
    plate_for,
    tower_brim,
    tower_footprint,
)
from scadbuddy.render.plate_profiles import PLATE_PROFILES, TOWER_KEEP_OUTS

# The keychain of library file 62: 176 x 46 mm in XY, 3 mm tall.
KEYCHAIN = np.array([[-88.0, -23.0, -1.5], [88.0, 23.0, 1.5]])


def _bounds(width: float, depth: float, height: float = 3.0) -> np.ndarray:
    return np.array([[0.0, 0.0, 0.0], [width, depth, height]])


def _side(plate: PlateGeometry, height: float = 3.0) -> float:
    """The side of the square two-filament tower (#6735) for objects ``height`` tall."""
    return tower_footprint(plate, height).width


class TestBedTypes:
    """#83: the plates Bambu Studio's own bed picker offers for each printer."""

    @pytest.mark.parametrize("name", ["H2C", "Bambu Lab H2C", "Bambu Lab H2C 0.4 nozzle"])
    def test_the_h2c_takes_neither_the_cool_nor_the_smooth_pei_plate(self, name: str) -> None:
        # Its machine_model profile: not_support_bed_type "Cool Plate;Smooth PEI Plate /
        # High Temp Plate", which are labels for the values below.
        assert bed_types_for(name) == ("Engineering Plate", "Textured PEI Plate", "Supertack Plate")

    def test_the_a1_mini_refuses_the_cool_and_engineering_plates(self) -> None:
        assert bed_types_for("A1M") == ("High Temp Plate", "Textured PEI Plate", "Supertack Plate")

    @pytest.mark.parametrize("name", [None, "", "Elegoo Neptune"])
    def test_an_unknown_printer_is_offered_every_plate(self, name: str | None) -> None:
        assert bed_types_for(name) == (
            "Cool Plate",
            "Engineering Plate",
            "High Temp Plate",
            "Textured PEI Plate",
            "Supertack Plate",
        )


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

    @pytest.mark.parametrize(
        ("model", "strip"),
        [
            # Each profile's ``wrapping_exclude_area`` at BambuStudio f977235 (#122).
            ("H2C", Rect(145.0, 310.0, 251.0, 326.0)),
            ("H2D", Rect(145.0, 310.0, 256.0, 326.0)),
            ("H2DP", Rect(145.0, 310.0, 256.0, 326.0)),
            ("H2S", Rect(172.3, 302.0, 232.5, 322.0)),
            # Listed back edge first in the profile; the table keeps its bounding box.
            ("P2S", Rect(153.0, 235.0, 216.0, 256.0)),
        ],
    )
    def test_wrapping_detection_strip_per_model(self, model: str, strip: Rect) -> None:
        assert plate_for(model).wrapping_exclusions == (strip,)

    def test_printers_without_wrapping_detection_carry_no_strip(self) -> None:
        assert not plate_for("X1C").wrapping_exclusions
        assert not plate_for("A1").wrapping_exclusions


class TestTowerBrim:
    """#5545: the brim reserved round the tower follows Bambu Studio's auto brim
    (``WipeTower::get_auto_brim_by_height``), never less than the 3 mm default."""

    @pytest.mark.parametrize(
        ("height", "brim"),
        [
            (3.0, 3.0),
            (21.0, 3.0),
            (37.5, 3.0),
            (50.0, 4.0),
            (100.0, 8.0),
            (133.0, 8.0),
            (325.0, 8.0),
        ],
    )
    def test_the_brim_grows_with_the_tower_to_8_mm(self, height: float, brim: float) -> None:
        assert tower_brim(height) == pytest.approx(brim)

    def test_an_unknown_height_reserves_the_widest_brim(self) -> None:
        assert tower_brim(None) == PRIME_TOWER_AUTO_BRIM == 8.0

    @pytest.mark.parametrize(
        ("size", "measured_fail_y"),
        [
            # basketball-trophy: 110 x 70 x 133 mm, 5 filaments. Sliced 2026-11-07 on
            # scadbuddy-slicer:sha-d54bcd4, the tower at x 145: y 6 fails with -104
            # (an extrusion at y -2.205), y 6.5 passes.
            ((110.0, 70.0, 133.0), 6.0),
            # basketball-coach-trophy: 150 x 150 x 220 mm, 8 filaments (ORD-2026-6): y 5
            # fails; y 10 passes, its lowest extrusion at y 1.809.
            ((150.0, 150.0, 220.0), 5.0),
        ],
    )
    def test_a_tall_multi_colour_h2c_plate_keeps_the_tower_off_the_front_edge(
        self, size: tuple[float, float, float], measured_fail_y: float
    ) -> None:
        placement = place_on_plate(_bounds(*size), plate_for("H2C"))
        assert placement.tower is not None
        x, y = placement.tower
        assert y > measured_fail_y
        assert y - PRIME_TOWER_AUTO_BRIM >= plate_for("H2C").usable.min_y + EDGE_MARGIN
        assert x - PRIME_TOWER_AUTO_BRIM >= plate_for("H2C").usable.min_x + EDGE_MARGIN

    def test_a_short_h2c_plate_keeps_the_tower_where_it_sliced(self) -> None:
        """The 21 mm, 2-filament nameplate of #5545 sliced with the tower at y 5."""
        placement = place_on_plate(_bounds(176.0, 46.0, 21.0), plate_for("H2C"))
        assert placement.tower is not None
        assert placement.tower[1] == 5.0

    @pytest.mark.parametrize("model", ["A1 mini", "A1", "X1C", "P1S", "H2D", "H2S", "P2S"])
    def test_a_tall_object_still_fits_beside_its_tower(self, model: str) -> None:
        """The wider brim costs 10 mm of band; a 60 mm, 150 mm tall object still has
        room on every plate, the A1 mini's 180 mm one included."""
        plate = plate_for(model)
        placement = place_on_plate(_bounds(60.0, 60.0, 150.0), plate)
        assert placement.tower is not None
        x, y = placement.tower
        brim = tower_brim(150.0)
        inset = EDGE_MARGIN + brim
        side = _side(plate, 150.0)
        assert plate.usable.min_x + inset <= x <= plate.usable.max_x - inset - side
        assert plate.usable.min_y + inset <= y <= plate.usable.max_y - inset - side


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
        assert placement.tower is not None
        x, y = placement.tower
        assert x >= plate.usable.min_x
        assert x + _side(plate) <= plate.usable.max_x
        assert y >= plate.usable.min_y
        assert y + _side(plate) <= plate.usable.max_y
        # The failing file put the tower at x=15..75, inside extruder 2's dead zone.
        assert x >= 25.0

    def test_tower_does_not_overlap_the_object(self) -> None:
        for model in ("H2C", "X1C", "A1", "H2D"):
            plate = plate_for(model)
            placement = place_on_plate(KEYCHAIN, plate)
            assert placement.tower is not None
            cx, cy = placement.offset[0], placement.offset[1]
            object_box = (cx - 88.0, cy - 23.0, cx + 88.0, cy + 23.0)
            tower = (
                placement.tower[0],
                placement.tower[1],
                placement.tower[0] + _side(plate),
                placement.tower[1] + _side(plate),
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
        assert placement.tower is not None
        x, y = placement.tower
        cutout = plate.exclusions[0]
        assert not (x < cutout.max_x and y < cutout.max_y)

    @staticmethod
    def _tower_footprint(placement: Placement, plate: PlateGeometry) -> Rect:
        assert placement.tower is not None
        reserved = _side(plate) + 2 * PRIME_TOWER_BRIM
        x = placement.tower[0] - PRIME_TOWER_BRIM
        y = placement.tower[1] - PRIME_TOWER_BRIM
        return Rect(x, y, x + reserved, y + reserved)

    @pytest.mark.parametrize(("width", "depth"), [(100.0, 180.0), (100.0, 250.0), (280.0, 200.0)])
    def test_h2c_tower_stays_out_of_the_wrapping_zone_when_the_front_is_blocked(
        self, width: float, depth: float
    ) -> None:
        """A tower in the clumping-detection strip (y >= 310, x 145..251) fails
        ``Print::validate`` whenever the slicing preset enables wrapping detection
        (#122). Every object here is deep enough that its footprint, grown by
        ``TOWER_CLEARANCE``, blocks the front strip (y 2..68) where it is centred.
        """
        plate = plate_for("H2C")
        wrapping = Rect(145.0, 310.0, 251.0, 326.0)
        placement = place_on_plate(_bounds(width, depth), plate)
        footprint = self._tower_footprint(placement, plate)
        assert not footprint.overlaps(wrapping), f"tower {footprint} is in the wrapping zone"

    @pytest.mark.parametrize("model", ["H2C", "H2D", "H2DP", "H2S", "P2S"])
    @pytest.mark.parametrize("size", ["keychain", "large"])
    def test_every_wrapping_printer_still_finds_a_tower_edge(self, model: str, size: str) -> None:
        """Avoiding the wrapping strip is only free if another edge is always left.

        ``large`` fills 90% of the reachable width and leaves just over the
        tower's band of depth, so the object has to move off centre and the tower
        is placed on the moved path.
        """
        plate = plate_for(model)
        band = _side(plate) + 2 * PRIME_TOWER_BRIM + TOWER_CLEARANCE + EDGE_MARGIN
        width, depth = (
            (60.0, 20.0)
            if size == "keychain"
            else (plate.usable.width * 0.9, plate.usable.depth - band - 5.0)
        )
        placement = place_on_plate(_bounds(width, depth), plate)
        footprint = self._tower_footprint(placement, plate)
        for zone in plate.wrapping_exclusions:
            assert not footprint.overlaps(zone), f"{model}: tower {footprint} is in {zone}"
        assert plate.usable.min_x <= footprint.min_x and footprint.max_x <= plate.usable.max_x
        assert plate.usable.min_y <= footprint.min_y and footprint.max_y <= plate.usable.max_y

    def test_a_back_tower_is_refused_in_the_wrapping_zone(self) -> None:
        """Forces the one line that keeps a tower out of the wrapping strip.

        On the shipping H2C the back strip is never the first to clear: the object
        is centred, and the front and back strips sit the same distance from the
        centre, so an object that blocks one blocks the other. The H2C plate is
        used here with a cutout over the front strip, so the back is tried next —
        and without the wrapping check it would take it, at x 142..208, y 252..318.
        """
        h2c = plate_for("H2C")
        front_blocked = dataclasses.replace(h2c, exclusions=(Rect(25.0, 0.0, 325.0, 70.0),))
        without = dataclasses.replace(front_blocked, wrapping_exclusions=())
        wrapping = h2c.wrapping_exclusions[0]

        unchecked = self._tower_footprint(place_on_plate(_bounds(100.0, 100.0), without), h2c)
        assert unchecked.overlaps(wrapping), "the back strip should have been chosen"

        checked = self._tower_footprint(place_on_plate(_bounds(100.0, 100.0), front_blocked), h2c)
        assert not checked.overlaps(wrapping), f"tower {checked} is in the wrapping zone"

    def test_a_large_object_moves_over_to_leave_the_tower_room(self) -> None:
        # Centred, 280 x 260 leaves 30 mm in front and behind, less than the
        # two-filament tower's 35.5 + brim + clearance; off centre it leaves 60, so the
        # object gives up the centre and the tower takes the strip.
        plate = plate_for("H2C")
        placement = place_on_plate(_bounds(280.0, 260.0), plate)
        assert placement.tower is not None
        centre_y = placement.offset[1] + 130.0
        assert centre_y > plate.usable.centre[1], "the object should have moved off centre"
        tower_top = placement.tower[1] + PRIME_TOWER_BRIM + _side(plate)
        assert placement.offset[1] - tower_top >= TOWER_CLEARANCE

    @pytest.mark.parametrize("depth", [46.0, 140.0, 170.0, 180.0, 190.0, 230.0])
    def test_the_tower_always_keeps_its_clearance_from_the_object(self, depth: float) -> None:
        # Whether the object keeps the centre or gives it up, the gap the constant
        # promises has to hold. The unmoved path used to skip the check: a 280 x 180
        # model on an H2C sat 2 mm from its tower and nothing failed.
        plate = plate_for("H2C")
        placement = place_on_plate(_bounds(280.0, depth), plate)
        assert placement.tower is not None
        corner = (
            placement.tower[0] - PRIME_TOWER_BRIM,
            placement.tower[1] - PRIME_TOWER_BRIM,
        )
        # ``tower`` names the tower itself; the brim sits outside it on every side,
        # so the footprint that has to stay clear is wider than the tower.
        reserved = _side(plate) + 2 * PRIME_TOWER_BRIM
        gaps = [
            placement.offset[1] - (corner[1] + reserved),
            corner[1] - (placement.offset[1] + depth),
            placement.offset[0] - (corner[0] + reserved),
            corner[0] - (placement.offset[0] + 280.0),
        ]
        assert max(gaps) >= TOWER_CLEARANCE, f"tower is too close to the object: {gaps}"

    def test_an_object_dodges_a_cutout_and_the_tower_strip_at_once(self) -> None:
        """The one combination no shipping profile has: a multi-extruder dead zone
        *and* a filament-cutter cutout, with an object big enough to need moving.

        Both avoidance rules are exercised separately elsewhere; this pins that
        they compose on a plate carrying both features — the object clears the
        cutout, stays inside the reachable area, and keeps its distance from the
        tower. (Here the object still fits centred, so the tower takes an edge;
        the nudge on the moved path is bounded by ``remainder`` rather than the
        whole reachable area, which no shipping profile can reach today.)
        """
        plate = PlateGeometry(
            model="Test Both",
            size=(300.0, 300.0),
            usable=Rect(25.0, 0.0, 300.0, 300.0),
            exclusions=(Rect(25.0, 0.0, 60.0, 50.0),),
            extruders=2,
            height=250.0,
        )
        placement = place_on_plate(_bounds(180.0, 120.0), plate)
        assert placement.tower is not None
        rect = (
            placement.offset[0],
            placement.offset[1],
            placement.offset[0] + 180.0,
            placement.offset[1] + 120.0,
        )
        for cut in plate.exclusions:
            assert (
                rect[0] >= cut.max_x
                or rect[2] <= cut.min_x
                or rect[1] >= cut.max_y
                or rect[3] <= cut.min_y
            ), f"object {rect} sits in the cutout {cut}"
        assert rect[0] >= plate.usable.min_x and rect[2] <= plate.usable.max_x
        assert rect[1] >= plate.usable.min_y and rect[3] <= plate.usable.max_y

        reserved = _side(plate) + 2 * PRIME_TOWER_BRIM
        corner = (
            placement.tower[0] - PRIME_TOWER_BRIM,
            placement.tower[1] - PRIME_TOWER_BRIM,
        )
        gaps = [
            rect[1] - (corner[1] + reserved),
            corner[1] - rect[3],
            rect[0] - (corner[0] + reserved),
            corner[0] - rect[2],
        ]
        assert max(gaps) >= TOWER_CLEARANCE, f"tower is too close to the object: {gaps}"

    @staticmethod
    def _cutout_plate(exclusions: tuple[Rect, ...]) -> PlateGeometry:
        return PlateGeometry(
            model="Test Cutout",
            size=(120.0, 256.0),
            usable=Rect(0.0, 0.0, 120.0, 256.0),
            exclusions=exclusions,
            extruders=2,
            height=250.0,
        )

    def test_a_tower_landing_on_a_cutout_falls_through_to_another_side(self) -> None:
        """Forces the one line standing between a tower and the filament cutter.

        The shipping-profile tests never reach it — on a P1S the front corner
        works out around x=95, nowhere near the 0-18 mm cutout — so deleting
        ``_tower_corner``'s exclusion check broke nothing. Here the front
        candidate lands squarely on the cutout, so falling through to another
        side is the only way to pass.
        """
        cutout = Rect(0.0, 0.0, 60.0, 70.0)
        without = place_on_plate(_bounds(40.0, 40.0), self._cutout_plate(()))
        assert without.tower is not None

        with_cutout = place_on_plate(_bounds(40.0, 40.0), self._cutout_plate((cutout,)))
        assert with_cutout.tower is not None
        reserved = _side(self._cutout_plate(())) + 2 * PRIME_TOWER_BRIM
        corner_x = with_cutout.tower[0] - PRIME_TOWER_BRIM
        corner_y = with_cutout.tower[1] - PRIME_TOWER_BRIM
        footprint = Rect(corner_x, corner_y, corner_x + reserved, corner_y + reserved)
        assert not footprint.overlaps(cutout), f"tower {footprint} sits in the cutout"
        assert with_cutout.tower != without.tower, "the cutout should have moved it"

    def test_an_object_within_the_edge_margin_of_full_width_still_fits(self) -> None:
        """``EDGE_MARGIN`` keeps the *tower* off the edge; it must not shrink the
        object's bound on the axis the tower does not compete for.

        A 298 x 200 mm two-colour model on an H2C (300 mm reachable) was refused
        with "no room for the prime tower" because the front/back remainder
        inherited the tower's inset on X, losing 2 x EDGE_MARGIN of width it had
        no claim to.
        """
        plate = plate_for("H2C")
        assert plate.usable.width == 300.0
        placement = place_on_plate(_bounds(298.0, 200.0), plate)
        assert placement.tower is not None
        assert plate.usable.min_x <= placement.offset[0]
        assert placement.offset[0] + 298.0 <= plate.usable.max_x

    def test_an_object_wider_than_the_plate_is_refused_naming_the_axis(self) -> None:
        with pytest.raises(PlateFitError, match=r"340.*on X.*300"):
            place_on_plate(_bounds(340.0, 40.0), plate_for("H2C"))

    def test_an_object_that_leaves_no_room_for_the_tower_is_refused(self) -> None:
        with pytest.raises(PlateFitError, match="prime tower"):
            place_on_plate(_bounds(295.0, 315.0), plate_for("H2C"))

    def test_a_large_single_colour_object_moves_clear_of_the_filament_cutter(self) -> None:
        # The X1/P1 cutter corner was screened against the tower and never against
        # the object, so a big single-colour model sat across it silently.
        plate = plate_for("X1C")
        assert plate.exclusions, "the X1C profile should carry the cutter cutout"
        placement = place_on_plate(_bounds(230.0, 210.0), plate, tower=False)
        rect = (
            placement.offset[0],
            placement.offset[1],
            placement.offset[0] + 230.0,
            placement.offset[1] + 210.0,
        )
        for cut in plate.exclusions:
            clear = (
                rect[0] >= cut.max_x
                or rect[2] <= cut.min_x
                or rect[1] >= cut.max_y
                or rect[3] <= cut.min_y
            )
            assert clear, f"object {rect} sits in the cutout {cut}"
        assert rect[0] >= plate.usable.min_x and rect[2] <= plate.usable.max_x

    def test_an_object_that_cannot_clear_the_cutter_is_refused(self) -> None:
        with pytest.raises(PlateFitError, match="filament cutter"):
            place_on_plate(_bounds(250.0, 250.0), plate_for("X1C"), tower=False)

    def test_a_small_object_is_left_centred(self) -> None:
        # The nudge must not perturb anything that already clears the cutout.
        plate = plate_for("X1C")
        placement = place_on_plate(_bounds(180.0, 180.0), plate, tower=False)
        assert (placement.offset[0] + 90.0, placement.offset[1] + 90.0) == plate.usable.centre

    def test_a_single_colour_object_needs_no_tower(self) -> None:
        placement = place_on_plate(_bounds(295.0, 315.0), plate_for("H2C"), tower=False)
        assert placement.tower is None


class TestTowerFootprint:
    """#6735: the tower the slice builds, by Bambu Studio's own estimate
    (``Print::wipe_tower_data``, Print.cpp L3243-3311 at v02.08.02.61)."""

    @staticmethod
    def _rib_side(volume: float, least: float) -> float:
        # Print.cpp L3289-3293: sqrt(volume / layer height x infill gap), at least the
        # depth for the height, plus rib width / sqrt(2).
        return float(max(least, (volume / 0.2 * 1.5) ** 0.5) + 8.0 / 2**0.5)

    def test_a_two_nozzle_tower_counts_every_filament_and_the_nozzle_change(self) -> None:
        h2c = plate_for("H2C")
        change = 12.0 * np.pi * 1.75**2 / 4
        for filaments in (2, 3, 4, 8):
            tower = tower_footprint(h2c, 20.0, TowerSettings(filaments=filaments))
            volume = 45.0 * filaments + change * (filaments // 2)
            least = 5.0 + 15.0 / 95.0 * 15.0
            assert tower.width == pytest.approx(self._rib_side(volume, least))
            assert tower.depth == tower.width
            assert tower.brim == 3.0
            assert tower.keep_out == 0.0

    def test_a_one_nozzle_tower_counts_one_filament_less(self) -> None:
        tower = tower_footprint(plate_for("X1C"), 20.0, TowerSettings(filaments=3))
        assert tower.width == pytest.approx(self._rib_side(45.0 * 2, 5.0 + 15.0 / 95.0 * 15.0))

    def test_eight_filaments_outgrow_the_old_60_mm_square(self) -> None:
        """The square reserved before #6735 was 60 mm; an eight-filament H2C tower is
        65 mm, so a plate that fitted the square no longer fits the tower."""
        h2c = plate_for("H2C")
        eight = TowerSettings(filaments=8)
        assert tower_footprint(h2c, 10.0, eight).width > 65.0
        assert fit_problem((300.0, 245.0, 10.0), h2c, tower=True) is None
        problem = fit_problem((300.0, 245.0, 10.0), h2c, tower=True, settings=eight)
        assert problem is not None and "prime tower" in problem

    def test_without_the_rib_wall_the_width_is_stated_and_the_depth_grows(self) -> None:
        tower = tower_footprint(
            plate_for("H2C"), 20.0, TowerSettings(filaments=4, rib_wall=False, width=35.0)
        )
        volume = 45.0 * 4 + 12.0 * np.pi * 1.75**2 / 4 * 2
        assert tower.width == 35.0
        assert tower.depth == pytest.approx(volume / (0.2 * 35.0) * 1.5)

    @pytest.mark.parametrize(
        ("height", "least"),
        [(3.0, 5.0), (5.0, 5.0), (100.0, 20.0), (175.0, 30.0), (250.0, 40.0), (400.0, 60.0)],
    )
    def test_the_least_depth_follows_the_height(self, height: float, least: float) -> None:
        """``WipeTower::min_depth_per_height`` (WipeTower.cpp L1587-1589)."""
        assert min_tower_depth(height) == pytest.approx(least)

    def test_no_sparse_layers_keeps_the_h2c_keep_out_from_tall_objects(self) -> None:
        """96 mm ``extruder_clearance_max_radius`` less 0.2, the spiral lift's reach
        for a 0.4 mm ``z_hop`` and 2 x 0.8 mm of padding (Print.cpp L3335-3433)."""
        h2c = plate_for("H2C")
        reach = 2 * 0.4 / (2 * np.pi * np.arctan(3 * np.pi / 180))
        no_sparse = TowerSettings(no_sparse_layers=True)
        tall = tower_footprint(h2c, 4.4, no_sparse)
        assert tall.keep_out == pytest.approx(95.8 + reach + 1.6)
        assert tall.clearance == tall.keep_out
        short = tower_footprint(h2c, 4.0, no_sparse)
        assert short.keep_out == pytest.approx(3.8 + reach + 1.6)
        assert tower_footprint(DEFAULT_PLATE, 4.4, no_sparse).keep_out == 0.0

    def test_a_no_sparse_plate_keeps_its_objects_out_of_the_keep_out(self) -> None:
        h2c = plate_for("H2C")
        settings = TowerSettings(no_sparse_layers=True)
        placement = place_on_plate(_bounds(60.0, 60.0, 10.0), h2c, settings=settings)
        assert placement.tower is not None
        tower = tower_footprint(h2c, 10.0, settings)
        x, y = placement.tower[0] - tower.brim, placement.tower[1] - tower.brim
        ox, oy = placement.offset[0], placement.offset[1]
        gaps = [
            oy - (y + tower.size[1]),
            y - (oy + 60.0),
            ox - (x + tower.size[0]),
            x - (ox + 60.0),
        ]
        assert max(gaps) >= tower.keep_out

    def test_a_full_keychain_plate_has_no_room_with_no_sparse_layers(self) -> None:
        """The skill's 12-tag plate, 218 x 317 mm and 4.4 mm tall: the slicer refuses it
        with "relative position of the model and the prime tower"."""
        h2c = plate_for("H2C")
        size = (218.0, 317.0, 4.4)
        assert fit_problem(size, h2c, tower=True) is None
        no_sparse = TowerSettings(no_sparse_layers=True)
        problem = fit_problem(size, h2c, tower=True, settings=no_sparse)
        assert problem is not None and "No sparse layers" in problem


def test_every_two_nozzle_printer_has_a_tower_keep_out() -> None:
    """#6757: `_keep_out` reads `TOWER_KEEP_OUTS` by the plate's model, and a model that
    is missing gets no keep-out. A regenerated table that drops a printer with two
    nozzles would then let a "No sparse layers" tower stand too close to the objects."""
    two_nozzles = [model for model in PLATE_PROFILES if plate_for(model).extruders >= 2]
    assert "Bambu Lab H2C" in two_nozzles
    for model in two_nozzles:
        assert plate_for(model).model == model
    assert [model for model in two_nozzles if model not in TOWER_KEEP_OUTS] == []
