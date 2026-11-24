-- The kept copy of Bambuddy's local presets (#4645) is read and written by nothing since
-- #7244: ScadBuddy's own filament presets replace it, and Bambuddy's come in only by an
-- import. The print workers of the builds before #7244 were gone from prod before this
-- ran (#7275), so no `…/held-presets` read can reach it.
DROP TABLE IF EXISTS held_local_presets;
