-- Inventory K1 (#6949): the inventory forwarders are a service caller, `inventory`, which
-- the API mints in its own process (`auth/service_auth.py` `ServiceCaller`).
ALTER TABLE service_credentials DROP CONSTRAINT service_credentials_caller_check;
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch', 'camera', 'camera-relay', 'inventory'));

ALTER TABLE service_caller_suspensions DROP CONSTRAINT service_caller_suspensions_caller_check;
ALTER TABLE service_caller_suspensions ADD CONSTRAINT service_caller_suspensions_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront', 'intake',
                      'profile-fetch', 'camera', 'camera-relay', 'inventory'));
