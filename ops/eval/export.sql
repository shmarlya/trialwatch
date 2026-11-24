-- Every answer and judgement of the gold evaluation's runs, as CSV for its artifact (eval-shared.yml):
-- what each question got from the model and which rule settled it, so a CI run can be read without
-- its database. Writes gold-eval-answers.csv, gold-eval-judgements.csv and gold-eval-runs.csv to the
-- current folder.
\set ON_ERROR_STOP on
\copy (SELECT a.run_id, (SELECT min(i.value) FROM core.trial_identifier i WHERE i.trial_id = a.trial_id AND i.scheme = 'eu-ct') AS ct_number, a.key, a.item_text, a.widened, a.evidence_id IS NOT NULL AS answered, a.found, a.kind, a.verified, a.page_no, a.quote, a.note, (s.data->'trace'->a.key->>'value') AS value, s.data->'trace'->a.key->>'rule' AS rule FROM checks.answer a LEFT JOIN checks.step s ON s.run_id = a.run_id AND s.trial_id = a.trial_id AND s.name = 'rules' ORDER BY a.run_id, ct_number, a.key) TO 'gold-eval-answers.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT j.run_id, (SELECT min(i.value) FROM core.trial_identifier i WHERE i.trial_id = j.trial_id AND i.scheme = 'eu-ct') AS ct_number, j.label, j.parts, j.error, j.served_by FROM checks.judgement j ORDER BY j.run_id, ct_number) TO 'gold-eval-judgements.csv' WITH (FORMAT csv, HEADER)
\copy (SELECT id, model, effort, prompt, requirements, params, status, started_at, finished_at, trials, errors FROM checks.run ORDER BY id) TO 'gold-eval-runs.csv' WITH (FORMAT csv, HEADER)
