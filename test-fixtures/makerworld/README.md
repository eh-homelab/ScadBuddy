# MakerWorld 3MFs for tests

Real project files, copied unchanged from the maintainer's Bambuddy library on
2026-10-11 (the library file id is each name's prefix), for #2146: every kind of
file Arrange and mesh models must read. `manifest.json` records each file's
MakerWorld metadata (`3D/3dmodel.model`: Title, Designer, DesignerUserId, License,
Origin, DesignModelId), its sha256, what it covers, and what Arrange's reader made
of it; `backend/tests/test_makerworld_fixtures.py` checks both.

The files are Git LFS (`.gitattributes`): `git lfs pull --include "test-fixtures/**"`.
They are outside `backend/` so no image carries them; CI mounts this directory into
the test container (`backend/tests/support/fixtures.py`).
