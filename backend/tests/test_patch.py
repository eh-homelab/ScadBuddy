from __future__ import annotations

import pytest

from scadbuddy.library.patch import (
    MAX_EDITS,
    PatchError,
    SearchReplace,
    apply_edits,
    apply_unified_diff,
)

SOURCE = "a\nb\nc\nd\ne\n"


def test_a_git_diff_applies_with_its_headers() -> None:
    diff = (
        "diff --git a/model.scad b/model.scad\n"
        "index 1111111..2222222 100644\n"
        "--- a/model.scad\n"
        "+++ b/model.scad\n"
        "@@ -2,3 +2,3 @@\n"
        " b\n"
        "-c\n"
        "+C\n"
        " d\n"
    )
    assert apply_unified_diff(SOURCE, diff) == "a\nb\nC\nd\ne\n"


def test_several_hunks_apply_top_to_bottom_with_the_offset_the_first_leaves() -> None:
    diff = "@@ -1,1 +1,2 @@\n a\n+A\n@@ -4,1 +5,1 @@\n-d\n+D\n"
    assert apply_unified_diff(SOURCE, diff) == "a\nA\nb\nc\nD\ne\n"


def test_a_hunk_with_wrong_line_numbers_is_found_where_its_lines_are() -> None:
    assert apply_unified_diff(SOURCE, "@@ -40,2 +40,3 @@\n d\n+X\n e\n") == "a\nb\nc\nd\nX\ne\n"


def test_a_pure_insertion_goes_after_the_stated_line() -> None:
    assert apply_unified_diff(SOURCE, "@@ -0,0 +1 @@\n+top\n") == "top\n" + SOURCE
    assert apply_unified_diff(SOURCE, "@@ -2,0 +3 @@\n+mid\n") == "a\nb\nmid\nc\nd\ne\n"


def test_a_removed_line_that_starts_with_two_dashes_is_not_a_file_header() -> None:
    source = "x\n-- y\nz\n"
    assert apply_unified_diff(source, "@@ -1,3 +1,2 @@\n x\n--- y\n z\n") == "x\nz\n"


def test_no_trailing_newline_is_kept_as_the_source_had_it() -> None:
    diff = "@@ -2 +2 @@\n-b\n+B\n\\ No newline at end of file\n"
    assert apply_unified_diff("a\nb", diff) == "a\nB"


# "\ No newline at end of file" names the side of the line before it
# (https://www.gnu.org/software/diffutils/manual/html_node/Incomplete-Lines.html);
# review of #741: a change to the file's ending is applied, not dropped.
NO_NEWLINE = "\\ No newline at end of file\n"


def test_a_diff_that_drops_the_trailing_newline_drops_it() -> None:
    diff = "@@ -1,2 +1,2 @@\n a\n-b\n+B\n" + NO_NEWLINE
    assert apply_unified_diff("a\nb\n", diff) == "a\nB"


def test_a_diff_that_adds_the_trailing_newline_adds_it() -> None:
    diff = "@@ -1,2 +1,2 @@\n a\n-b\n" + NO_NEWLINE + "+B\n"
    assert apply_unified_diff("a\nb", diff) == "a\nB\n"


def test_only_the_trailing_newline_changes() -> None:
    assert apply_unified_diff("a\nb\n", "@@ -2 +2 @@\n-b\n+b\n" + NO_NEWLINE) == "a\nb"
    assert apply_unified_diff("a\nb", "@@ -2 +2 @@\n-b\n" + NO_NEWLINE + "+b\n") == "a\nb\n"


def test_a_marker_after_context_is_both_sides() -> None:
    diff = "@@ -1,2 +1,2 @@\n-a\n+A\n b\n" + NO_NEWLINE
    assert apply_unified_diff("a\nb", diff) == "A\nb"
    with pytest.raises(PatchError, match="source ends without a newline"):
        apply_unified_diff("a\nb\n", diff)


def test_an_old_side_marker_against_a_newline_is_a_conflict() -> None:
    diff = "@@ -2 +2 @@\n-b\n" + NO_NEWLINE + "+B\n"
    with pytest.raises(PatchError, match=r"hunk 1: .*source ends without a newline"):
        apply_unified_diff("a\nb\n", diff)


def test_a_marker_short_of_the_end_is_refused() -> None:
    diff = "@@ -1 +1 @@\n-a\n+A\n" + NO_NEWLINE
    with pytest.raises(PatchError, match="does not reach the end of the file"):
        apply_unified_diff("a\nb\n", diff)
    two = "@@ -1 +1 @@\n-a\n+A\n" + NO_NEWLINE + "@@ -2 +2 @@\n-b\n+B\n"
    with pytest.raises(PatchError, match="does not reach the end of the file"):
        apply_unified_diff("a\nb\n", two)


def test_a_misplaced_marker_is_refused() -> None:
    with pytest.raises(PatchError, match="follows no line"):
        apply_unified_diff("a\n", "@@ -1 +1 @@\n" + NO_NEWLINE + "-a\n+A\n")
    with pytest.raises(PatchError, match="comes after that side's"):
        apply_unified_diff("a\nb", "@@ -1,2 +1,2 @@\n-a\n" + NO_NEWLINE + "-b\n+A\n+B\n")


def test_context_that_is_not_there_is_a_conflict_naming_the_hunk() -> None:
    with pytest.raises(PatchError, match=r"hunk 1 .*not in the source"):
        apply_unified_diff(SOURCE, "@@ -2,2 +2,2 @@\n b\n-q\n+Q\n")


def test_lines_found_more_than_once_elsewhere_are_ambiguous() -> None:
    with pytest.raises(PatchError, match="occur 2 times"):
        apply_unified_diff("x\ny\nx\ny\n", "@@ -9,1 +9,1 @@\n-x\n+X\n")


def test_a_second_file_is_refused() -> None:
    diff = "--- a/one\n+++ b/one\n@@ -1 +1 @@\n-a\n+A\n--- a/two\n+++ b/two\n@@ -1 +1 @@\n-a\n+A\n"
    with pytest.raises(PatchError, match="more than one file"):
        apply_unified_diff(SOURCE, diff)


def test_text_that_is_not_a_diff_is_refused() -> None:
    with pytest.raises(PatchError, match="not a unified diff"):
        apply_unified_diff(SOURCE, "cube(10);\n")
    with pytest.raises(PatchError, match="no @@ hunks"):
        apply_unified_diff(SOURCE, "--- a/model.scad\n+++ b/model.scad\n")


def test_edits_apply_in_turn() -> None:
    edits = [SearchReplace(search="b\n", replace="B\n"), SearchReplace(search="B\nc", replace="X")]
    assert apply_edits(SOURCE, edits) == "a\nX\nd\ne\n"


def test_a_missing_or_repeated_search_is_refused_by_number() -> None:
    with pytest.raises(PatchError, match="edit 2: its search text is not in the source"):
        apply_edits(
            SOURCE, [SearchReplace(search="a", replace="A"), SearchReplace(search="q", replace="")]
        )
    with pytest.raises(PatchError, match="edit 1: its search text occurs 2 times"):
        apply_edits("cube(1);\ncube(1);\n", [SearchReplace(search="cube(1)", replace="cube(2)")])


def test_edits_are_bounded() -> None:
    with pytest.raises(PatchError, match="no edits"):
        apply_edits(SOURCE, [])
    with pytest.raises(PatchError, match=f"at most {MAX_EDITS}"):
        apply_edits(SOURCE, [SearchReplace(search="a", replace="a")] * (MAX_EDITS + 1))
