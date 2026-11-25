-- #7682: the newest weighing's drift (its data.drift_g), null when it has no breakdown.
ALTER TABLE filament_spools ADD COLUMN last_drift_g double precision;
