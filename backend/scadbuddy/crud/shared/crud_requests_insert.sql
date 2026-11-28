INSERT INTO crud_requests (caller, key, route, body_sha256, status, body)
VALUES (%s, %s, %s, %s, %s, %s) RETURNING body
