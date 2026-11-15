-- #3710: an optional friendly name for each Claude credential. Settings shows it as the
-- row's title. It is not secret, and the list returns it. It is a display label only:
-- it is not in the sealed secret's AAD, and it does not have to be unique.
--
-- The check repeats src/credentials.ts `normaliseCredentialName`, so a row that no route
-- wrote still has a name the UI can show: NULL, or 1 to 60 characters with no space at
-- either end and no control character. The route also refuses format and separator
-- characters (bidi overrides, line separators); the table does not.
ALTER TABLE ai_credentials
  ADD COLUMN name text,
  ADD CONSTRAINT ai_credentials_name_check CHECK (
    name IS NULL OR (char_length(name) BETWEEN 1 AND 60 AND name = btrim(name) AND name !~ '[[:cntrl:]]')
  );
