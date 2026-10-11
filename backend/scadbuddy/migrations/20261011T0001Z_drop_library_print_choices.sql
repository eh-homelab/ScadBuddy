-- #1963: since #1754 (#1954) nothing reads or writes library_print_choices; its rows
-- were copied into model_print_choices under 'library:<file id>' by
-- `20261009T0541Z_print_choices_by_subject.sql`, which kept the table only so an image
-- rolled back past #1754 still found it. That rollback is no longer possible, so it goes.
-- Irreversible: an image older than #1954 fails reading a library file's choices.
DROP TABLE IF EXISTS library_print_choices;
