-- #3785 review: a promote claims its (attachment, template) before it copies, so two
-- concurrent promotes cannot both copy, and `promoted_to` is set only once the copy has
-- landed. A claim is {"token": <run's token>, "at": <when>}; a run killed mid-copy never
-- clears its own, so one older than the promote's run timeout is free to take again.
ALTER TABLE print_attachments ADD COLUMN IF NOT EXISTS promoting jsonb NOT NULL DEFAULT '{}';
