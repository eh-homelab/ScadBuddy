-- #5282 (storefront phase 3): the storefront edge is a service caller of its own,
-- `storefront`, with credentials from the exchange only (`auth/service_auth.py`
-- `ServiceCaller`). Its credentials and its suspension are rows like the others'.
ALTER TABLE service_credentials DROP CONSTRAINT service_credentials_caller_check;
ALTER TABLE service_credentials ADD CONSTRAINT service_credentials_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront'));

ALTER TABLE service_caller_suspensions DROP CONSTRAINT service_caller_suspensions_caller_check;
ALTER TABLE service_caller_suspensions ADD CONSTRAINT service_caller_suspensions_caller_check
    CHECK (caller IN ('agent', 'print', 'projects', 'render', 'storefront'));
