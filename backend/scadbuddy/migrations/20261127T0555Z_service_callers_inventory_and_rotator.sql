-- #6949 K1: `inventory` and `fleet-rotator` are both service callers. Each one's migration
-- rewrote the caller lists without the other's name, and the one with the later timestamp
-- (`fleet-rotator`) dropped `inventory`. This file ends with both, whatever order the two
-- were applied in.
ALTER TABLE service_credentials DROP CONSTRAINT service_credentials_caller_check;
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch', 'camera', 'camera-relay', 'fleet', 'fleet-rotator',
                      'inventory'));

ALTER TABLE service_caller_suspensions DROP CONSTRAINT service_caller_suspensions_caller_check;
ALTER TABLE service_caller_suspensions ADD CONSTRAINT service_caller_suspensions_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch', 'camera', 'camera-relay', 'fleet', 'fleet-rotator',
                      'inventory'));
