-- Fleet chat channels named by GitHub references (#8149). A channel's name, at most 256
-- characters, is a GitHub issue or PR reference (owner/repo#number), an Actions run reference
-- (owner/repo/runs/id), or a name under Kubernetes' rules (a DNS-1123 subdomain: each
-- dot-separated label 1 to 63 lower-case alphanumerics and "-", starting and ending with an
-- alphanumeric). A name valid only under the rule before (^[a-z0-9-]{1,40}$, such as one with a
-- leading or trailing "-") stays valid here, so every existing row passes; core refuses it for
-- a new channel. tools/go/fleetchat/testdata/channel-vectors.json holds the cases, and core's
-- tests run them against this CHECK.
ALTER TABLE fleet_channels DROP CONSTRAINT fleet_channels_name_check;
ALTER TABLE fleet_channels ADD CONSTRAINT fleet_channels_name_check CHECK (
    char_length(name) <= 256 AND (
        name ~ '^[a-z0-9][a-z0-9-]{0,38}/[a-z0-9._-]{1,100}(?:#[1-9][0-9]{0,9}|/runs/[1-9][0-9]{0,19})$'
        OR name ~ '^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?)*$'
        OR name ~ '^[a-z0-9-]{1,40}$'
    )
);
