-- #2166: the nozzle plan of a project's last print (which side each filament printed
-- from, `bambuddy.nozzle_plan.NozzlePlan`), so the file Generate files into the project
-- states the same Manual map and a later print in the same spools reuses it (#317).
ALTER TABLE project_print_targets ADD COLUMN nozzle_plan jsonb;
