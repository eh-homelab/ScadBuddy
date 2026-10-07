-- Pins in flight and library clones, shared by every process that pins (#1131), like
-- the render leases in `library_leases`. A pin holds a `library_pin_holds` row from
-- before its clone until the model records it, and a removal waits until none is live;
-- an install holds one of a fixed number of `library_install_slots`. A holder renews
-- its row while it works, and a crashed holder's row lapses at `expires_at` (the
-- database's clock).
CREATE TABLE library_pin_holds (
    token uuid PRIMARY KEY,
    taken_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE library_install_slots (
    slot integer PRIMARY KEY,
    token uuid NOT NULL,
    expires_at timestamptz NOT NULL
);
