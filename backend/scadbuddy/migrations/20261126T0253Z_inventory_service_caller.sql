-- Inventory K1 (#6949): the inventory forwarders are a service caller, `inventory`, which
-- the API mints in its own process (`auth/service_auth.py` `ServiceCaller`). The lists keep
-- `fleet-rotator` (20261126T1743Z_fleet_virtual_senders): a database that applied that file
-- first may hold its rows, and a list without it would fail here and stop the API's start.
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
