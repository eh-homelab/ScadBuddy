-- A render lease holds one row per checkout under its one token (#872), but the table's
-- key was the token alone, so a lease on two libraries failed on its second row
-- (`library_leases_pkey`). The key is the token and the checkout (#5609; #5298: the render
-- worker's lease route sends a take again under its token, and finds the same rows).
ALTER TABLE library_leases DROP CONSTRAINT library_leases_pkey;
ALTER TABLE library_leases ADD PRIMARY KEY (token, library, commit);
