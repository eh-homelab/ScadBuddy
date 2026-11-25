-- #7523: the setup a file listing's slice was priced with (`slice_price.py` `setup_key`:
-- the sha256 of the subject's kind and the slice request). Another listing of the same
-- file shows the row as its last price only for the same setup. NULL on a template's row
-- and on a row from before, which then matches no other listing.
ALTER TABLE storefront_slice_prices ADD COLUMN setup_key text;
