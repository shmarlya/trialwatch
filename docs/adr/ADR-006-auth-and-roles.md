# ADR-006: Sign-in and an action → role chart

**Status:** accepted · **Date:** 2026-11-21 · **Session:** — · **Cards:** — · **Exam:** — · **Supersedes:** none · **Narrowed by:** ADR-007

> **Narrowed by ADR-007 (2026-11-21):** the public no longer browses every page. Anyone not signed in sees the
> home page, the sign-in page, published judgements with their trace (`view.published`) and, since
> ADR-008, the pages of trials that recruit now (`view.recruiting`). Everything else, `view` included, needs
> a signed-in admin or moderator. The action chart, sessions and roles below are unchanged.

## Question

How do several people use the web interface so that anyone can read, only admins and moderators act, and what each role may do is decided in one place?

## Context

- The web interface had no login: it was safe only because it answered on this machine and took form posts only from its own pages.
- To run it for others, the public must be able to browse trials, sponsors and judgements but start, change and spend nothing, in the pages or by calling the routes (curl, a script).
- Two kinds of people act: admins (sources, model calls that cost money, users) and moderators (curating trials and their evidence). More roles may come.
- Every write is already audited per core.actor, but every web write was recorded as the account running the server.
- No external services: it runs in Docker Compose next to Postgres.

## Options

### A. Role checks inside each route

How it works: each route compares the signed-in user's role with names written in it (if role == "admin").

- **+** smallest change
- **–** the rule is spread over every route; a route without a check is open and nothing notices
- **–** a new role means editing every route
- **–** nobody can see who may do what

### B. One action → role chart in code

How it works: auth/actions.py lists every action and the roles allowed it. Each route that changes something names its action (Depends(Requires(...))); a test fails if one names none. /admin/roles draws the chart. Users, argon2id password hashes and sessions are a new auth schema; each user is a core.actor, so writes are audited as them.

- **+** one file to read and change; default deny
- **+** the chart's history is git's
- **–** changing the chart is a code change and a deploy

### C. The chart as data

How it works: roles and role_action tables, edited on an admin page.

- **+** rules change without a deploy
- **–** more tables and pages to build
- **–** a wrong row opens an action silently; tests can't see it
- **–** rule changes need versioning (none before production)

## Criteria × options

| Criterion | Option A | Option B | Option C |
|---|---|---|---|
| Safety: nothing left unguarded | – a route is easy to forget | + a test fails on an unguarded route | 0 the test can't see the rows |
| Clarity: who may do what | – read every route | + one file, one page | + one page |
| Effort: to build and to maintain | + least | 0 one module, one test | – tables, admin pages |
| Traceability: who changed a rule | 0 git | + git; writes as the person | – needs its own audit |
| Undo: how hard to reverse later | 0 | + drop the auth schema | 0 |

## Decision

We choose B. Anyone may browse (view); every other action needs a signed-in user whose role the chart allows. Admins may do everything. Moderators curate: add trials, upload documents, edit sponsors, transfer evidence, run dry-run checks and stop their own jobs. Only admins sync and configure sources, run checks that call the model (they cost money), stop other people's jobs and manage users. Sign-in is an email and password (argon2id) in auth.app_user; a session is a random token in an HttpOnly, SameSite=Lax cookie, kept in auth.session only as its SHA-256, ending after 12 hours idle or 7 days. No sign-up: the first admin comes from TRIALWATCH_ADMIN_EMAIL/_PASSWORD or `user add`. No API tokens yet: the same routes answer a script with 401 or 403. The command line is not limited by roles, since whoever runs it holds the database password. Decided by: safety and clarity.

## Consequences

**Easier:** a new role or action is one line in the chart, and /admin/roles and the tests follow; the audit log names the person who acted.

**Harder:** serving it on a network needs HTTPS in front (the cookie is Secure off this machine) and TRIALWATCH_HOSTS; a forgotten password needs an admin.

## Revisit when · Lab

**Revisit when:** scripts need to act (API tokens on the same chart); a role should differ per source or trial; sign-in should go through an organisation's identity provider.

**Lab:** tests/test_auth.py checks the chart, that every changing route names an action, each role against the routes, sessions ending, the last admin staying, and writes recorded as the person.

## Session notes

Decided with the user on 2026-11-21: the public browses read-only; password sign-in with sessions in the database; moderators curate data, admins hold sources, model spend and users; no API tokens for now.
