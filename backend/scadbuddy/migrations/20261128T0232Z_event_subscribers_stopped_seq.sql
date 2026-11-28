-- #8074: the event a durable subscriber's cursor cannot pass (a payload that does not
-- fit its table), so the API's scrape can tell a stopped cursor from a slow one. The
-- subscriber sets it when it stops there, and sets it NULL once an apply stops at none.
ALTER TABLE event_subscribers ADD COLUMN stopped_seq bigint;
