-- #4673: what a trigger tells a person about itself, such as a newer built-in flow it
-- was not moved to (orders/builtin_flow.py). Cleared when the trigger moves.
ALTER TABLE flow_triggers ADD COLUMN notice text;
