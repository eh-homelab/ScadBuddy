# The phone app's APK as an image (#8376): build-app.yml builds the APK, then this
# image with only the APK and its SHA-256, so it sits on GHCR beside the other images.
# The build context is the folder the workflow puts those two files in, not app/.
# Get it out with `crane export` or `docker create` + `docker cp` (app/CLAUDE.md).
FROM scratch
COPY scadbuddy.apk scadbuddy.apk.sha256 /
