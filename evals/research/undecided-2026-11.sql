-- Why so many `undecided` labels: the queries behind evals/research/undecided-2026-11.md.
-- Run against a restore of the prod snapshot (data/pg-dump-all-1795643511-*.gz, check run 1).
-- Everything is created in a scratch schema `r`; nothing in the app's schemas is changed.
-- The gold table r.gold(key, label, participants, outcomes, cancelled) is loaded from
-- data/gold/bruckner/2026-03-16/items.jsonl (expected label and parts) with \copy.

create schema if not exists r;
create or replace function r.norm(t text) returns text immutable language sql
  as $$ select lower(regexp_replace(t, '[^[:alnum:]]', '', 'g')) $$;
create index if not exists page_file_idx on core.page (file_sha256, page_no);

-- 1. Labels, and which part is left open
select label, count(*) from checks.judgement where run_id = 1 group by 1;
select parts->>'participants' p, parts->>'outcomes' o, parts->>'cancelled' c, count(*)
from checks.judgement where run_id = 1 and label = 'undecided' group by 1, 2, 3 order by 4 desc;

-- 2. Every open question of an undecided trial, with the rule that left it open.
-- A question-level `cancelled` that the participants-count inference settled (trace key part:cancelled) is not a blocker.
drop table if exists r.qnone;
create table r.qnone as
select s.trial_id, t.key, case when t.key like 'endpoint:%' then 'endpoint' else t.key end qtype,
       regexp_replace(t.v->>'rule', 'verified (narrative_only|chart_only|redacted|not_analysed) in', 'verified <np-kind> in') rule
from checks.step s
join checks.judgement j on j.run_id = s.run_id and j.trial_id = s.trial_id and j.label = 'undecided'
cross join lateral jsonb_each(s.data->'trace') t(key, v)
where s.run_id = 1 and s.name = 'rules' and t.v->'value' = 'null'::jsonb and t.v->>'rule' <> 'a requirement is undecided'
  and not (t.key = 'cancelled' and s.data->'trace'->'part:cancelled'->>'value' is not null);

select qtype, rule, count(*) questions, count(distinct trial_id) trials from r.qnone group by 1, 2 order by 1, 3 desc;
-- trials that one rule alone keeps undecided
select rule, count(*) from (select trial_id, min(qtype || ': ' || rule) rule from r.qnone
  group by 1 having count(distinct qtype || rule) = 1) x group by 1 order by 2 desc;

-- 3. Pages: unreadable pages (< 200 letters) and size, per trial
drop table if exists r.tp;
create table r.tp as
select s.trial_id, j.label, (s.data->>'total_pages')::int total_pages,
  (select coalesce(sum(jsonb_array_length(d->'unreadable_pages')), 0) from jsonb_array_elements(s.data->'documents') d) unreadable,
  (s.data->>'read_whole')::bool read_whole
from checks.step s join checks.judgement j using (run_id, trial_id) where s.run_id = 1 and s.name = 'pages';

-- 4. Unverified quotes: where the quote really is (cited file only; whole-trial search is too slow on 3000-page reports)
with u as (
  select a.*, r.norm(a.quote) nq from r.qnone q
  join checks.answer a on a.run_id = 1 and a.trial_id = q.trial_id and a.key = q.key
  where q.rule = 'quote not on the cited page')
select case
  when u.file_sha256 is null or u.page_no is null then 'no cited file/page'
  when length(u.nq) < 10 then 'quote too short'
  when exists (select 1 from core.page p where p.file_sha256 = u.file_sha256 and p.page_no = u.page_no
               and r.norm(p.text) like '%' || u.nq || '%') then 'on cited page (punctuation/space only)'
  when exists (select 1 from core.page p where p.file_sha256 = u.file_sha256 and p.page_no between u.page_no - 2 and u.page_no + 2
               and r.norm(p.text) like '%' || u.nq || '%') then 'page off by 1-2'
  else 'not near cited page' end, count(*)
from u group by 1 order by 2 desc;

-- 5. Simulation: re-apply the rules to the stored trace with some rules changed, against prod and gold.
-- Sanity: with every flag false it gives exactly the stored labels (836 / 291 / 597).
drop table if exists r.qv;
create table r.qv as
select s.trial_id, t.key, case when t.key like 'endpoint:%' then 'outcomes' else t.key end part,
  (t.v->>'value')::bool val, t.v->>'rule' rule
from checks.step s cross join lateral jsonb_each(s.data->'trace') t(key, v)
where s.run_id = 1 and s.name = 'rules' and (t.key like 'endpoint:%' or t.key in ('participants', 'cancelled'))
  and not (t.key in ('participants', 'cancelled')
           and t.v->>'rule' in ('a requirement is undecided', 'every requirement reported', 'a requirement is not reported'));
-- mark the unverified quotes found on or near the cited page (section 4) as recoverable
update r.qv set rule = '__quote_recoverable' where rule = 'quote not on the cited page' and (trial_id, key) in (
  select a.trial_id, a.key from r.qnone q join checks.answer a on a.run_id = 1 and a.trial_id = q.trial_id and a.key = q.key
  where q.rule = 'quote not on the cited page' and a.file_sha256 is not null and length(r.norm(a.quote)) >= 10
    and exists (select 1 from core.page p where p.file_sha256 = a.file_sha256 and p.page_no between a.page_no - 2 and a.page_no + 2
                and r.norm(p.text) like '%' || r.norm(a.quote) || '%'));

create or replace function r.sim2(pr boolean, cnt boolean, qt boolean) returns table (trial_id bigint, label text)
language sql as $$
with v as (select trial_id, part, case
    when pr and rule like 'verified partial_result%' then true
    when cnt and rule = 'verified quote without a count' then true
    when qt and rule = '__quote_recoverable' then true
    when rule = '__quote_recoverable' then null
    else val end val from r.qv),
p as (select trial_id, part, case when bool_or(val is false) then false when bool_and(val is true) then true end val from v group by 1, 2),
w as (select p.trial_id, max(case when part = 'participants' then val::int end) pa,
  max(case when part = 'outcomes' then val::int end) ou, max(case when part = 'cancelled' then val::int end) ca from p group by 1),
w2 as (select w.trial_id, pa, ou,
  case when (s.data->'trace'->'part:cancelled') is not null then (s.data->'trace'->'part:cancelled'->>'value')::bool::int
       when ca is null and pa = 1 and (cnt or qt) then 0 else ca end ca
  from w join checks.step s on s.run_id = 1 and s.name = 'rules' and s.trial_id = w.trial_id)
select w2.trial_id, case when pa = 0 or ou = 0 or ca = 1 then 'not full'
                         when pa = 1 and ou = 1 and ca = 0 then 'full' else 'undecided' end
from w2 join checks.judgement j on j.run_id = 1 and j.trial_id = w2.trial_id and j.label is not null $$;

create or replace view r.gj as
select g.*, j.trial_id, j.label our, j.parts from r.gold g
join core.trial_identifier t on t.scheme = 'eu-ct' and t.value = g.key
join checks.judgement j on j.run_id = 1 and j.trial_id = t.trial_id;

select sc, count(*) filter (where s.label = 'undecided') undecided,
  round(100.0 * count(*) filter (where s.label = 'undecided') / count(*), 1) pct,
  count(*) filter (where g.label is not null and s.label <> 'undecided') gold_decided,
  round(100.0 * count(*) filter (where g.label is not null and s.label = g.label)
        / nullif(count(*) filter (where g.label is not null and s.label <> 'undecided'), 0), 1) agree_pct
from (values ('0 now', false, false, false), ('A count parser', false, true, false),
             ('B quote verify', false, false, true), ('A+B', false, true, true),
             ('A+B+partial reported', true, true, true)) sc(sc, a, b, c)
cross join lateral r.sim2(a, b, c) s left join r.gj g on g.trial_id = s.trial_id
group by 1 order by 1;
