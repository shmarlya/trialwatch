-- migrate:up

-- Bruckner et al.'s manual review of the 206 results documents, from their
-- ctis_merged_reviews_consolidated_with_comments.csv (on OSF, no licence), loaded by
-- `trialwatch gold bruckner-reviews`. The answer key per part for the results-document check;
-- the final verdict per trial stays in gold.bruckner_verdict. Never updated.

-- One row per results document (= per trial with results posted).
CREATE TABLE gold.bruckner_document (
    ct_number         text PRIMARY KEY REFERENCES ctis.trial,
    fetch_run_id      bigint NOT NULL REFERENCES ctis.fetch_run,
    source_file       text NOT NULL,
    -- when the results appeared in CTIS (their results_pub_date_DDMMYYYY)
    results_posted_on date NOT NULL,
    -- their agreement_flag: the first two reviewers agreed before any discussion
    reviewers_agreed  boolean NOT NULL,
    final_comment     text,
    content_hash      bytea NOT NULL CHECK (length(content_hash) = 32),
    -- the row as in the file. The file is not valid UTF-8 (text copied from CTIS was mangled
    -- before it reached us), so broken bytes are stored as U+FFFD; no label is affected.
    payload           jsonb NOT NULL
);
CREATE INDEX bruckner_document_fetch_run_idx ON gold.bruckner_document (fetch_run_id);

-- One row per reviewer per document: what each reviewer answered, part by part. NULL = the
-- reviewer left that part blank.
CREATE TABLE gold.bruckner_review (
    ct_number      text NOT NULL REFERENCES gold.bruckner_document,
    -- first: columns *_till_christie, every document; second: *_team, one of four team members
    -- (named in the row's reviewer_team); third: *_ghassan, 37 documents, mostly disagreements
    reviewer       text NOT NULL CHECK (reviewer IN ('first', 'second', 'third')),
    -- the document reports the number of participants
    participants   boolean,
    -- the document reports results for the primary outcome(s)
    outcomes       boolean,
    -- the trial was cancelled (no participants enrolled)
    cancelled      boolean,
    -- the reviewer asked for a second look
    asked_to_check boolean,
    comment        text,
    PRIMARY KEY (ct_number, reviewer)
);


-- migrate:down
DROP TABLE gold.bruckner_review;
DROP TABLE gold.bruckner_document;
