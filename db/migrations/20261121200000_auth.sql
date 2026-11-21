-- migrate:up

-- Sign-in for the web interface (ADR-006). Each user has a password and one role (admin or
-- moderator). What a role may do is the chart in src/trialwatch/auth/actions.py, not data here.
-- Anyone not signed in can only browse. Each user is also a core.actor, so their writes are audited
-- under their own name.
SET LOCAL trialwatch.actor = '1';

CREATE SCHEMA auth;

CREATE TABLE auth.app_user (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username text NOT NULL UNIQUE CHECK (username ~ '^[a-z0-9][a-z0-9._-]{1,62}$'),
    -- argon2id; never written to the audit log
    password_hash text NOT NULL,
    role text NOT NULL CHECK (role IN ('admin', 'moderator')),
    actor_id bigint NOT NULL UNIQUE REFERENCES core.actor (id),
    disabled_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE auth.app_user IS 'people who can sign in to the web interface; their role picks their actions (auth/actions.py)';

-- The cookie holds a random token. Only its SHA-256 is kept, so reading this table gives no live sessions.
CREATE TABLE auth.session (
    token_sha256 bytea PRIMARY KEY CHECK (length(token_sha256) = 32),
    user_id bigint NOT NULL REFERENCES auth.app_user (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX session_user_idx ON auth.session (user_id);

CREATE TRIGGER app_user_audit AFTER INSERT OR DELETE OR UPDATE ON auth.app_user
    FOR EACH ROW EXECUTE FUNCTION core.audit('id', 'password_hash');


-- migrate:down
SET LOCAL trialwatch.actor = '1';
DROP SCHEMA auth CASCADE;
