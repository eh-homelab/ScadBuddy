-- Durable sessions (#1056, plan task 12 fix round 2), on the projector's stream row:
--   chain   the execution chain (the first run's id, which Continue-As-New keeps and a new
--           start does not) that next_offset counts in. A follower of another chain starts
--           at 0, so an offset can never be applied to a run it does not belong to.
--   sending the turn id of a send whose update-with-start has not answered yet. While it is
--           set, a closed run may be a new one still being started, so the projector does
--           not settle the session idle on it at once.
ALTER TABLE ai_durable_streams ADD COLUMN chain text, ADD COLUMN sending text;
