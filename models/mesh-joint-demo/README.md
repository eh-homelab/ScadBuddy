# Mesh joint demo

A mesh opened as a scene (#2146): a two-colour carrot (orange body, green neck and
leaves) cut at the neck with a keyed peg on the body and the matching socket in the
leaves, then the leaves laid on their side so the layer lines run along them.

- `scene.json` is the scene (the source of truth); `model.scad` is what it compiles to,
  and `meshes/` holds each part's geometry with no colour of its own.
- The one parameter, `leaves_x`, is where the leaves sit on the bed. A jointed cut is not
  exposed, since moving one side would leave the peg floating.
- `make.py` builds all of it from scratch. From `backend/`:
  `uv run --frozen python ../models/mesh-joint-demo/make.py`.
- `verify.sh` renders the model and checks the colours and the pieces of each.
