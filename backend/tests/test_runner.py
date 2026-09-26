from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from scadbuddy.core.config import Config, load_config
from scadbuddy.core.fontconfig import conf_path, write_conf
from scadbuddy.render.runner import (
    OpenSCADError,
    RenderTimeoutError,
    UnknownParameterError,
    build_defines,
    cached_schema,
    export_schema,
    format_scad_value,
    quote_string,
    render_3mf,
    run_openscad,
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
    assert format_scad_value(numbers, 4) == "4.0"


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
