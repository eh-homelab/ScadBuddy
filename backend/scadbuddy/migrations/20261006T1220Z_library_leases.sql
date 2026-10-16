-- Render leases on library checkouts (#872), shared by the API and the render worker:
-- a removal refuses while a live lease names the checkout. A holder renews its rows
-- while it renders; a crashed holder's rows lapse at `expires_at` (the database's
-- clock), so they never block a removal for longer than one TTL.
CREATE TABLE library_leases (
    token uuid PRIMARY KEY,
    holder text NOT NULL,
    library text NOT NULL,
    commit text NOT NULL,
    taken_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX library_leases_checkout ON library_leases (library, commit);
