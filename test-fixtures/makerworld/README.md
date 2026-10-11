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

## Credits and licences

Every file is its designer's work, credited below as its own metadata names them, and
kept here unchanged only to test that ScadBuddy reads real files correctly. Most are
under MakerWorld's Standard Digital File License or MakerWorld Exclusive License
(https://makerworld.com/en/makerworld-licenses), which do not grant redistribution;
the maintainer chose to include them. A designer who wants a file removed: open an
issue. Deleting it from the tree only stops new checkouts from using it: the LFS object
stays in history and in GitHub's LFS store until history is rewritten and GitHub
support purges the object, and a removal will do both.

| File | Title | Designer | Licence | Size | Covers |
|---|---|---|---|---|---|
| `2335-shaun-das-schaf-toilettenrollenhalter.3mf` | Shaun das Schaf Toilettenrollenhalter | BrickBeat | BY | 1.0 MB | single colour, one object (CC BY) |
| `2330-shaun-das-schaf-toilettenrollenhalter.3mf` | Shaun das Schaf Toilettenrollenhalter | BrickBeat | BY | 2.6 MB | single colour, two objects on two plates (CC BY) |
| `411-broccoli.3mf` | broccoli | ScadBuddy | not stated | 0.4 MB | five colours by part, one object, no paint |
| `400-blaubeere-fur-spielzeug-kaufladen-deko-blaubeere.3mf` | Blaubeere für Spielzeug Kaufladen - Deko Blaubeere | FörsterSepp | Standard Digital File License | 1.1 MB | two colours by object, 82 objects on two plates |
| `2167-pumpkin-fidget-twist.3mf` | Pumpkin Fidget Twist | JoshValjosh | BY-SA | 1.7 MB | painted (Bambu paint_color), one object, one part (CC BY-SA) |
| `2069-fidget-cubes-mc-edition.3mf` | Fidget Cubes MC Edition | PiNK_3DP | Standard Digital File License | 1.3 MB | painted and 47 coloured parts in one object |
| `688-carrot-garden.3mf` | Carrot Garden | 3DPmom | Standard Digital File License | 1.8 MB | painted, four objects: Carrot Garden, the leaf-to-body joint scenario of #2146 |
| `448-articulated-pumpkin.3mf` | Articulated Pumpkin | Fil&Form3D | Standard Digital File License | 10.3 MB | two colours by part, 758k triangles: the Articulated Pumpkin 300% stem scenario of #2146 |
| `1943-mini-articulated-ankylosaurs-keychain-version.3mf` | Mini Articulated Ankylosaurs \| Keychain Version | Flexi JIMGA | Standard Digital File License | 6.6 MB | negative parts and paint: the file Arrange refused (negative part) |
| `2154-plane-launcher-for-lightweight-glider.3mf` | Plane Launcher for Lightweight Glider | Tieq | Standard Digital File License | 1.0 MB | a negative part, no paint |
| `2302-love-letter-box.3mf` | Love letter box | ondisek00 | Standard Digital File License | 1.8 MB | a modifier part, seven objects on three plates |
| `2485-5g-lizard-5g.3mf` | 5g蜥蜴 lizard-关节可动蜥蜴-仅5g | 魔趣MOQU | Standard Digital File License | 2.7 MB | a support blocker and paint |
| `2178-find-100-mini-frogs-spot-search-game.3mf` | Find 100 Mini Frogs – Spot & Search Game | TinyOrbit3D | Standard Digital File License | 33.7 MB | 200 objects, painted (Arrange's max_objects is 200) |
| `2464-ghost-campfire-light.3mf` | Ghost Campfire Light | Three Hearts Creations | Standard Digital File License | 71.8 MB | 72 MB: painted, 16 objects on nine plates, 1.1M painted triangles |
| `381-olive-oil-bottle-passthrough.3mf` | Olive Oil Bottle Passthrough | RJ Design | Standard Digital File License | 98.3 MB | 98 MB: three colours by object on three plates |
| `2602-tiny-flexi-frog-animal-separated-colors-print.3mf` | Tiny Flexi Frog Animal  -  Separated Colors Print | 🟢 Flexi JIMGA | Standard Digital File License | 54.7 MB | 55 MB: negative parts and three colours by part |
