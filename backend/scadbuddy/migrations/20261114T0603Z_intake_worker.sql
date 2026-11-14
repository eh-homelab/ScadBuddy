-- #6133 PR 2: the intake worker is a service caller of its own, `intake`, with credentials
-- from the exchange only (`auth/service_auth.py` `ServiceCaller`).
ALTER TABLE service_credentials DROP CONSTRAINT service_credentials_caller_check;
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake'));

ALTER TABLE service_caller_suspensions DROP CONSTRAINT service_caller_suspensions_caller_check;
ALTER TABLE service_caller_suspensions ADD CONSTRAINT service_caller_suspensions_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake'));

-- The claim freezes each file: a server-side copy to a key no presigned URL names
-- (`orders/storefront/model_requests.py` `_claimable`). Its ETag is kept here; the check
-- reads the copy with `If-Match` of it, and the import (PR 5) copies it with
-- `CopySourceIfMatch`. NULL until the claim.
ALTER TABLE storefront_uploads ADD COLUMN frozen_etag text;

-- A checked file's preview is held by its source (`orders/storefront/intake.py`,
-- holder `order_item_source`). A source goes only by a cascade (its item's or its order's
-- delete), so the hold goes with it here, and the blob sweep takes the preview once
-- nothing else holds it.
CREATE FUNCTION order_item_sources_release_preview() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM blob_refs WHERE holder_kind = 'order_item_source' AND holder_id = OLD.id;
    RETURN OLD;
END;
$$;

CREATE TRIGGER order_item_sources_release_preview
    AFTER DELETE ON order_item_sources
    FOR EACH ROW EXECUTE FUNCTION order_item_sources_release_preview();
