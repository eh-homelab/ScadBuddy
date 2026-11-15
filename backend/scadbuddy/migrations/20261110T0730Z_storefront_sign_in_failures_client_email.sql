-- verify_code counts wrong guesses per client and address (#5283): one index for that read.
CREATE INDEX storefront_sign_in_failures_client_email
  ON storefront_sign_in_failures (client, email, failed_at);
