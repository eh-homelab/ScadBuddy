from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from scadbuddy.core.config import Config, load_config
from scadbuddy.core.fontconfig import conf_path, write_conf
from scadbuddy.render.diagnostics import MAX_DIAGNOSTICS
from scadbuddy.render.runner import (
    OpenSCADError,
    ParameterValueError,
    RenderTimeoutError,
    UnknownParameterError,
    build_defines,
    cached_schema,
    export_schema,
    format_scad_value,
    missing_file,
    plate_count,
    quote_string,
    render_3mf,
    run_openscad,
    template_note,
)
from scadbuddy.render.schema import CustomizerSchema, Option, Parameter, build_schema
from tests.conftest import FIXTURES, load_fixture_param, load_fixture_source


def _schema(*parameters: Parameter) -> CustomizerSchema:
    return CustomizerSchema(parameters=list(parameters))


def test_quote_string_escapes() -> None:
    assert quote_string('say "hi"') == '"say \\"hi\\""'
    assert quote_string("C:\\models") == '"C:\\\\models"'
    assert quote_string("a\nb\tc\rd") == '"a\\nb\\tc\\rd"'
    assert quote_string("Ré agan ✈ 日本") == '"Ré agan ✈ 日本"'
    assert quote_string("") == '""'


def test_format_scad_value_by_type() -> None:
    assert format_scad_value(Parameter(name="a", type="boolean"), True) == "true"
    assert format_scad_value(Parameter(name="a", type="boolean"), False) == "false"
    assert format_scad_value(Parameter(name="a", type="integer"), 4.0) == "4"
    assert format_scad_value(Parameter(name="a", type="number"), 1.25) == "1.25"
    assert format_scad_value(Parameter(name="a", type="slider"), 3) == "3.0"
    assert format_scad_value(Parameter(name="a", type="color"), "#1f6feb") == '"#1f6feb"'
    assert format_scad_value(Parameter(name="a", type="font"), "DejaVu Sans") == '"DejaVu Sans"'


def test_format_scad_value_select_follows_option_type() -> None:
    words = Parameter(name="a", type="select", options=[Option(name="x", value="x")])
    numbers = Parameter(name="b", type="select", options=[Option(name="1", value=1.0)])
    assert format_scad_value(words, "x") == '"x"'
    assert format_scad_value(numbers, 1) == "1.0"


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        (Parameter(name="a", type="boolean"), "true"),
        (Parameter(name="a", type="string"), 3),
        (Parameter(name="a", type="number"), "3"),
        (Parameter(name="a", type="number"), True),
        (Parameter(name="a", type="integer"), "4"),
    ],
)
def test_format_scad_value_rejects_wrong_type(parameter: Parameter, value: object) -> None:
    with pytest.raises(ValueError, match=r"parameter 'a'|expected a number"):
        format_scad_value(parameter, value)  # type: ignore[arg-type]


def test_build_defines_rejects_unknown_parameters() -> None:
    schema = _schema(Parameter(name="known", type="number"))
    with pytest.raises(UnknownParameterError, match="bogus, other"):
        build_defines(schema, {"known": 1, "bogus": 2, "other": 3})


def test_build_defines_emits_only_supplied_parameters_in_schema_order() -> None:
    schema = _schema(
        Parameter(name="name", type="string"),
        Parameter(name="size", type="integer"),
        Parameter(name="lid", type="boolean"),
    )
    assert build_defines(schema, {"lid": True, "name": 'a"b'}) == [
        "-D",
        'name="a\\"b"',
        "-D",
        "lid=true",
    ]


async def test_cached_schema_does_not_re_export(tmp_path: Path) -> None:
    source = load_fixture_source("plain")
    scad = tmp_path / "model.scad"
    scad.write_text(source, encoding="utf-8")
    meta = tmp_path / "model.json"
    config = Config(openscad="/nonexistent/openscad")

    schema = build_schema(load_fixture_param("plain"), source)
    from scadbuddy.render.schema import store_cached_schema

    store_cached_schema(meta, schema)
    assert await cached_schema(scad, meta, config=config) == schema


@pytest.mark.requires_openscad
async def test_export_schema_matches_fixture(tmp_path: Path) -> None:
    scad = tmp_path / "name_keychain.scad"
    shutil.copy(FIXTURES / "name_keychain.scad", scad)
    schema = await export_schema(scad, config=load_config())
    expected = build_schema(
        load_fixture_param("name_keychain"), load_fixture_source("name_keychain")
    )
    assert schema.parameters == expected.parameters


@pytest.mark.requires_openscad
async def test_render_3mf_writes_a_file(tmp_path: Path) -> None:
    scad = tmp_path / "name_keychain.scad"
    shutil.copy(FIXTURES / "name_keychain.scad", scad)
    schema = build_schema(load_fixture_param("name_keychain"), load_fixture_source("name_keychain"))
    out = tmp_path / "out.3mf"
    result = await render_3mf(scad, schema, {"name": 'Re"agan'}, out, config=load_config())
    assert out.stat().st_size > 0
    assert result.returncode == 0


@pytest.mark.requires_openscad
async def test_render_timeout_kills_openscad_and_keeps_the_log(tmp_path: Path) -> None:
    scad = tmp_path / "slow_forever.scad"
    shutil.copy(FIXTURES / "slow_forever.scad", scad)
    config = replace(load_config(), render_timeout=1.0)
    with pytest.raises(RenderTimeoutError) as caught:
        await render_3mf(scad, CustomizerSchema(), {}, tmp_path / "out.3mf", config=config)
    assert isinstance(caught.value, OpenSCADError)
    assert caught.value.returncode is None


ECHO_FONTCONFIG = """#!/bin/sh
echo "FONTCONFIG_FILE=${FONTCONFIG_FILE:-<unset>}"
"""


async def _fontconfig_seen_by(tmp_path: Path, data_dir: Path) -> str:
    """Run a stand-in 'openscad' that prints the variable the render inherits."""
    binary = tmp_path / "echo-openscad"
    binary.write_text(ECHO_FONTCONFIG, encoding="utf-8")
    binary.chmod(0o755)
    config = Config(openscad=str(binary), data_dir=data_dir)
    output = await run_openscad([], cwd=tmp_path, config=config)
    return "\n".join(output.log_tail)


async def test_the_render_inherits_the_data_volumes_fontconfig(tmp_path: Path) -> None:
    """Without this, `text(font = ...)` only ever sees the image's own families."""
    data_dir = tmp_path / "data"
    write_conf(data_dir)

    assert f"FONTCONFIG_FILE={conf_path(data_dir)}" in await _fontconfig_seen_by(tmp_path, data_dir)


async def test_no_fontconfig_file_is_set_before_one_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing at a missing config is a fatal fontconfig error, so it is left unset."""
    monkeypatch.delenv("FONTCONFIG_FILE", raising=False)
    seen = await _fontconfig_seen_by(tmp_path, tmp_path / "empty")
    assert "FONTCONFIG_FILE=<unset>" in seen


ECHO_SECRET = """#!/bin/sh
echo "SECRET=${SCADBUDDY_TEST_SECRET:-<unset>}"
echo "HOME=${HOME:-<unset>}"
"""


async def test_the_render_does_not_inherit_the_backends_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#281: whatever openscad runs can read its own environment, so it gets an
    allowlist of the parent's, not a copy."""
    monkeypatch.setenv("SCADBUDDY_TEST_SECRET", "hunter2")
    monkeypatch.setenv("HOME", "/home/scadbuddy")
    binary = tmp_path / "echo-openscad"
    binary.write_text(ECHO_SECRET, encoding="utf-8")
    binary.chmod(0o755)
    config = Config(openscad=str(binary), data_dir=tmp_path / "data")

    seen = "\n".join((await run_openscad([], cwd=tmp_path, config=config)).log_tail)

    assert "SECRET=<unset>" in seen
    assert "hunter2" not in seen
    assert "HOME=/home/scadbuddy" in seen


ECHO_OPENSCADPATH = """#!/bin/sh
echo "OPENSCADPATH=${OPENSCADPATH:-<unset>}"
"""


async def _openscadpath_seen_by(tmp_path: Path, config: Config) -> str:
    binary = tmp_path / "echo-openscad"
    binary.write_text(ECHO_OPENSCADPATH, encoding="utf-8")
    binary.chmod(0o755)
    output = await run_openscad([], cwd=tmp_path, config=replace(config, openscad=str(binary)))
    return "\n".join(output.log_tail)


async def test_the_render_sees_only_the_library_path_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#93: the model's own libraries, and not whatever the process inherited."""
    monkeypatch.setenv("OPENSCADPATH", "/somewhere/else")
    first, second = tmp_path / "a", tmp_path / "b"
    config = Config(data_dir=tmp_path / "data", library_path=(first, second))

    assert f"OPENSCADPATH={first}:{second}" in await _openscadpath_seen_by(tmp_path, config)


async def test_a_model_with_no_libraries_gets_no_inherited_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENSCADPATH", "/somewhere/else")
    config = Config(data_dir=tmp_path / "data")

    assert "OPENSCADPATH=<unset>" in await _openscadpath_seen_by(tmp_path, config)


# ── #204: file parameters and the missing-file signal ─────────────────────────

FILE_PARAMETER = Parameter(name="overlay", type="file", initial="sample.svg", accept=["svg"])


@pytest.mark.parametrize(
    "value", ["", "sample.svg", "_scadbuddy_solid_asset_0123456789abcdef.svg", "a" * 64]
)
def test_a_file_parameter_passes_a_bare_name(value: str) -> None:
    assert format_scad_value(FILE_PARAMETER, value) == quote_string(value)


@pytest.mark.parametrize(
    "value",
    ["/etc/passwd", "../model.scad", "sub/dir.svg", "..", "a\\b.svg", ".hidden", "x\n.svg", 3],
)
def test_a_file_parameter_never_passes_a_path(value: object) -> None:
    with pytest.raises(ValueError, match="expects an uploaded or sample file"):
        format_scad_value(FILE_PARAMETER, value)  # type: ignore[arg-type]


def test_missing_file_reads_both_messages() -> None:
    assert missing_file("ERROR: Can't open file '/data/models/x/pic.svg', import() at line 7") == (
        "pic.svg"
    )
    assert missing_file("WARNING: The file '/w/nope.png' couldn't be opened.") == "nope.png"
    assert missing_file('ECHO: "Can\'t open file"') is None
    assert missing_file("Total rendering time: 0:00:00.000") is None


MISSING_FILE_OPENSCAD = """#!/bin/sh
echo "ERROR: Can't open file '/models/demo/pic.svg', import() at line 2"
i=0
while [ $i -lt 80 ]; do echo "filler $i"; i=$((i+1)); done
echo "WARNING: The file '/models/demo/mask.png' couldn't be opened."
echo "ERROR: Can't open file '/models/demo/pic.svg', import() at line 9"
"""


async def test_a_run_reports_every_file_it_could_not_open(tmp_path: Path) -> None:
    """Exit 0, and the first message long gone from the log tail by the end."""
    binary = tmp_path / "missing-openscad"
    binary.write_text(MISSING_FILE_OPENSCAD, encoding="utf-8")
    binary.chmod(0o755)
    config = Config(openscad=str(binary), data_dir=tmp_path / "data")

    output = await run_openscad([], cwd=tmp_path, config=config)

    assert output.missing_files == ("pic.svg", "mask.png")
    assert not any("line 2" in line for line in output.log_tail)


# ── #281: free-text values that would steer import()/surface() off the model ──

TEXT_PARAMETER = Parameter(name="label", type="string", initial="/default/is/the/templates")
STRING_SELECT = Parameter(
    name="shape",
    type="select",
    initial="a",
    options=[Option(name="up", value="../up"), Option(name="a", value="a")],
)


@pytest.mark.parametrize(
    "value", ["", "Hello", "Wait...", "3/4 inch", "a/b.svg", "..hidden", "x..y", "AC/DC"]
)
def test_ordinary_text_passes(value: str) -> None:
    assert format_scad_value(TEXT_PARAMETER, value) == quote_string(value)


@pytest.mark.parametrize(
    "value",
    ["/etc/passwd", "/proc/self/environ", "..", "../model.scad", "a/../../b", "sub/.."],
)
def test_a_path_out_of_the_model_directory_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="looks like a file path"):
        format_scad_value(TEXT_PARAMETER, value)


@pytest.mark.parametrize("kind", ["font", "color"])
def test_every_free_text_type_is_guarded(kind: str) -> None:
    parameter = Parameter(name="p", type=kind, initial="")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="looks like a file path"):
        format_scad_value(parameter, "/etc/passwd")


def test_a_string_select_is_guarded_off_its_options() -> None:
    with pytest.raises(ValueError, match="looks like a file path"):
        format_scad_value(STRING_SELECT, "/etc/passwd")


def test_the_templates_own_values_pass() -> None:
    """The initial and the options are the template's, like a file parameter's default."""
    assert format_scad_value(TEXT_PARAMETER, "/default/is/the/templates")
    assert format_scad_value(STRING_SELECT, "../up") == quote_string("../up")


def test_build_defines_refuses_a_path_so_the_route_422s() -> None:
    with pytest.raises(ValueError, match="looks like a file path"):
        build_defines(_schema(TEXT_PARAMETER), {"label": "/proc/self/environ"})


DIAGNOSTIC_OPENSCAD = """#!/bin/sh
echo "WARNING: Ignoring unknown variable 'wdith' in file $(pwd -P)/model.scad, line 4"
i=0
while [ $i -lt 80 ]; do echo "ECHO: $i"; i=$((i+1)); done
echo "ERROR: Assertion 'false' failed in file model.scad, line 9"
exit "${FAKE_EXIT:-0}"
"""


async def test_a_run_parses_every_diagnostic_from_the_whole_log(tmp_path: Path) -> None:
    """#252: read off the whole log like the missing files, not just the tail."""
    binary = tmp_path / "diagnostic-openscad"
    binary.write_text(DIAGNOSTIC_OPENSCAD, encoding="utf-8")
    binary.chmod(0o755)
    config = Config(openscad=str(binary), data_dir=tmp_path / "data")

    output = await run_openscad([], cwd=tmp_path.resolve(), config=config)

    assert [(d.severity, d.file, d.line) for d in output.diagnostics] == [
        ("warning", "model.scad", 4),
        ("error", "model.scad", 9),
    ]
    assert not any("wdith" in line for line in output.log_tail)


async def test_a_failed_run_carries_its_diagnostics(tmp_path: Path) -> None:
    binary = tmp_path / "diagnostic-openscad"
    # The exit status is baked in: openscad gets an allowlisted environment (#281),
    # so a FAKE_EXIT set on this process would never reach the script.
    binary.write_text(DIAGNOSTIC_OPENSCAD.replace('"${FAKE_EXIT:-0}"', "1"), encoding="utf-8")
    binary.chmod(0o755)
    config = Config(openscad=str(binary), data_dir=tmp_path / "data")

    with pytest.raises(OpenSCADError) as raised:
        await run_openscad([], cwd=tmp_path, config=config)

    assert [d.message for d in raised.value.diagnostics] == [
        "Ignoring unknown variable 'wdith'",
        "Assertion 'false' failed",
    ]
    assert raised.value.diagnostics_dropped == 0


FLOODING_OPENSCAD = """#!/bin/sh
i=0
while [ $i -lt 205 ]; do echo "WARNING: number $i in file model.scad, line 1"; i=$((i+1)); done
"""


async def test_a_run_says_how_many_diagnostics_it_left_out(tmp_path: Path) -> None:
    binary = tmp_path / "flooding-openscad"
    binary.write_text(FLOODING_OPENSCAD, encoding="utf-8")
    binary.chmod(0o755)
    config = Config(openscad=str(binary), data_dir=tmp_path / "data")

    output = await run_openscad([], cwd=tmp_path, config=config)

    assert len(output.diagnostics) == MAX_DIAGNOSTICS
    assert output.diagnostics_dropped == 205 - MAX_DIAGNOSTICS


def test_template_note_reads_note_and_warning_echoes() -> None:
    assert template_note('ECHO: "NOTE: letter_size reduced from 25 to 10.9 mm"') == (
        "letter_size reduced from 25 to 10.9 mm"
    )
    # wifi-qr-plaque and flexi-fabric echo `WARNING:`; both prefixes reach the user.
    assert template_note('ECHO: "WARNING: plaque too thin for magnet pockets"') == (
        "plaque too thin for magnet pockets"
    )
    # OpenSCAD prints the string raw: an embedded quote is not escaped.
    assert template_note('ECHO: "NOTE: overlay_file "x.svg" ignored"') == (
        'overlay_file "x.svg" ignored'
    )


def test_template_note_ignores_everything_else() -> None:
    assert template_note('ECHO: "QRROW;3;0101"') is None
    assert template_note('ECHO: "NOTE:", 5') is None
    assert template_note("ECHO: COASTERS = [3, 2]") is None
    assert template_note('ECHO: "NOTE: "') is None
    # OpenSCAD's own warnings are about the template, not the parameters.
    assert template_note("WARNING: Ignoring unknown variable 'x'") is None
    assert template_note('ECHO: "note: lower case is a debug echo"') is None


NOTES_OPENSCAD = """#!/bin/sh
echo 'ECHO: "NOTE: letter_size reduced from 25 to 10.9 mm"'
i=0
while [ $i -lt 80 ]; do echo "ECHO: \"QRROW;$i\""; i=$((i+1)); done
echo 'ECHO: "WARNING: modules are 0.59 mm"'
echo 'ECHO: "NOTE: letter_size reduced from 25 to 10.9 mm"'
"""


async def test_a_run_reports_every_note_the_template_echoed(tmp_path: Path) -> None:
    """Exit 0, the first note long gone from the log tail, and a repeat reported once."""
    binary = tmp_path / "notes-openscad"
    binary.write_text(NOTES_OPENSCAD, encoding="utf-8")
    binary.chmod(0o755)
    config = Config(openscad=str(binary), data_dir=tmp_path / "data")

    output = await run_openscad([], cwd=tmp_path, config=config)

    assert output.notes == ("letter_size reduced from 25 to 10.9 mm", "modules are 0.59 mm")
    assert not any("letter_size" in line for line in output.log_tail[:-1])


@pytest.mark.requires_openscad
async def test_a_real_render_reports_the_notes_it_echoed(tmp_path: Path) -> None:
    scad = tmp_path / "notes.scad"
    scad.write_text(
        'echo(str("NOTE: overlay_file \\"", "x.svg", "\\" ignored"));\n'
        'echo("WARNING: plaque too thin");\n'
        'echo("NOTE:", 5);\n'
        "cube(1);\n",
        encoding="utf-8",
    )
    output = await run_openscad(
        ["-o", str(tmp_path / "out.stl"), scad.name], cwd=tmp_path, config=load_config()
    )
    assert output.notes == ('overlay_file "x.svg" ignored', "plaque too thin")


# -- the customizer's range and options (#432) -------------------------------------


def _slider(**changes: object) -> Parameter:
    return Parameter(name="n", type="slider", min=1, max=64, step=1).model_copy(update=changes)


@pytest.mark.parametrize("value", [1, 64, 32.5])
def test_a_value_inside_the_range_is_taken(value: float) -> None:
    format_scad_value(_slider(), value)


@pytest.mark.parametrize(
    ("parameter", "value", "message"),
    [
        (_slider(), 65, "'n' must be between 1 and 64, got 65"),
        (_slider(), 0, "'n' must be between 1 and 64, got 0"),
        (_slider(type="integer"), 1000, "'n' must be between 1 and 64, got 1000"),
        (_slider(max=None), -1, "'n' must be at least 1, got -1"),
        (_slider(min=None), 100, "'n' must be at most 64, got 100"),
    ],
)
def test_a_value_outside_the_range_is_refused_by_name(
    parameter: Parameter, value: float, message: str
) -> None:
    with pytest.raises(ParameterValueError, match=message) as refused:
        format_scad_value(parameter, value)
    assert refused.value.parameter == "n"


def test_the_step_is_not_enforced() -> None:
    # plant-label ships `thickness = 2.5; // [1.6:0.2:5]`, off its own grid.
    assert format_scad_value(_slider(min=1.6, max=5, step=0.2), 2.5) == "2.5"


def test_a_select_value_has_to_be_an_option_or_retired() -> None:
    words = Parameter(
        name="kind",
        type="select",
        options=[Option(name="Auto", value="auto"), Option(name="PNG", value="png_threshold")],
        retired=["image_threshold"],
    )
    assert format_scad_value(words, "image_threshold") == '"image_threshold"'
    with pytest.raises(ParameterValueError, match='must be one of "auto", "png_threshold"'):
        format_scad_value(words, "stl")
    numbers = Parameter(
        name="count", type="select", options=[Option(name="1", value=1), Option(name="2", value=2)]
    )
    assert format_scad_value(numbers, 2.0) == "2.0"
    with pytest.raises(ParameterValueError, match="'count' must be one of 1, 2, got 999"):
        format_scad_value(numbers, 999)


# ── #289: a template states its plate count with `echo(plates = N)` ───────────


def test_plate_count_reads_only_the_plates_echo() -> None:
    assert plate_count("ECHO: plates = 2") == 2
    assert plate_count("ECHO: plates = 1") == 1
    assert plate_count('ECHO: "plates = 2"') is None
    assert plate_count("ECHO: plates = 2, lid = true") is None
    assert plate_count("ECHO: my_plates = 3") is None


PLATES_OPENSCAD = """#!/bin/sh
echo "ECHO: plates = 1"
echo "ECHO: plates = 3"
i=0
while [ $i -lt 80 ]; do echo "filler $i"; i=$((i+1)); done
"""


async def test_a_run_reports_the_last_plate_count_even_out_of_the_tail(tmp_path: Path) -> None:
    binary = tmp_path / "plates-openscad"
    binary.write_text(PLATES_OPENSCAD, encoding="utf-8")
    binary.chmod(0o755)
    config = Config(openscad=str(binary), data_dir=tmp_path / "data")

    output = await run_openscad([], cwd=tmp_path, config=config)

    assert output.plates == 3
    assert not any("plates" in line for line in output.log_tail)


async def test_a_run_that_echoes_no_plate_count_reports_none(tmp_path: Path) -> None:
    binary = tmp_path / "quiet-openscad"
    binary.write_text("#!/bin/sh\necho 'ECHO: \"MAZE\", 4'\n", encoding="utf-8")
    binary.chmod(0o755)
    config = Config(openscad=str(binary), data_dir=tmp_path / "data")

    assert (await run_openscad([], cwd=tmp_path, config=config)).plates is None
