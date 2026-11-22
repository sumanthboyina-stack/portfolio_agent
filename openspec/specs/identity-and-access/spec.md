# identity-and-access Specification

## Purpose
Defines who may use APEX and how a verified person becomes the actor behind every private read and write. Covers Entra ID sign-in with an invite allow-list, access requests, admin role checks, the trusted request context, owner scoping, and the privacy boundary between private data and shared market artifacts. Baseline at HEAD `ca450dd`.

## Requirements

### Requirement: Every page requires sign-in except Unsubscribe
The system SHALL require a signed-in, invited, enabled user on every web page before any `portfolio_agent` data access. The only page reachable without sign-in SHALL be the email Unsubscribe page.

#### Scenario: Visitor not signed in
- **WHEN** a visitor who is not signed in opens a page that is not Unsubscribe
- **THEN** the system renders the landing screen with Sign in and Register actions
- **AND** the page stops before any private data is read

#### Scenario: Gate runs before data access
- **WHEN** the source of any non-public page is inspected
- **THEN** its login gate is called at module level before any call into `portfolio_agent`

<!-- tests: tests/test_web_auth_wiring.py::test_page_calls_require_login_before_any_portfolio_agent_call -->

### Requirement: Unsubscribe page is public but inert
The system SHALL let the Unsubscribe page run without sign-in only if it imports nothing from `portfolio_agent` except the signed-token unsubscribe function, and does not use the current user, the login gate, navigation or a local context.

#### Scenario: Public page stays minimal
- **WHEN** the Unsubscribe page source is inspected
- **THEN** the only name it imports from `portfolio_agent` is the token-verified unsubscribe function
- **AND** it does not reference the login gate, current user, current context or top navigation

<!-- tests: tests/test_web_auth_wiring.py::test_the_only_public_page_is_the_unsubscribe_link_and_it_can_do_nothing_else -->

### Requirement: Sign-in must return expected identity claims from the expected issuer
The system SHALL refuse a signed-in session that lacks an issuer or subject claim, or whose issuer differs from the issuer derived from the configured Entra metadata URL, and SHALL offer sign-out instead of continuing.

#### Scenario: Missing claims
- **WHEN** the identity provider returns a session without an issuer or subject
- **THEN** the page shows "Sign-in didn't return the expected identity claims." with a Sign out button
- **AND** the page stops

#### Scenario: Unexpected issuer
- **WHEN** the session issuer is not the issuer derived by removing the well-known suffix from the metadata URL
- **THEN** the page shows "Sign-in came from an unexpected identity provider." with a Sign out button
- **AND** the page stops

#### Scenario: Metadata URL malformed
- **WHEN** the configured metadata URL does not end with the OpenID well-known suffix
- **THEN** the issuer cannot be derived and the page raises an error instead of admitting the user

<!-- tests: NONE -->

### Requirement: Login resolves only through the invite allow-list
The system SHALL turn a signed-in identity into an app user only when an enabled allow-list row matches it, and SHALL give the same denial for an unknown address, a disabled row, and an address already bound to a different identity.

#### Scenario: Unknown address denied
- **WHEN** an identity signs in whose email has no allow-list row
- **THEN** resolution returns no user

#### Scenario: Denied user sees a uniform message
- **WHEN** resolution returns no user
- **THEN** the page shows "Your account isn't invited. Ask the admin." with a Sign out button
- **AND** the message does not say whether the cause was unknown, disabled or bound elsewhere

<!-- tests: tests/test_auth_users_db.py::test_unknown_email_denied -->

### Requirement: First login binds issuer and subject to the invited row
The system SHALL, on the first successful login for an invited enabled address with no bound identity, bind that login's issuer and subject to the row and record the last-login time.

#### Scenario: First login binds
- **WHEN** an invited address signs in for the first time
- **THEN** the user is returned with the invited owner and role, the issuer and subject bound, and a last-login time set

<!-- tests: tests/test_auth_users_db.py::test_invite_then_first_login_binds_issuer_and_subject -->

### Requirement: Bound identity is recognised by issuer and subject alone
The system SHALL, once an identity is bound, look the user up by issuer and subject only, so a later email value is not needed.

#### Scenario: Later login without email
- **WHEN** a bound identity signs in again with no email claim
- **THEN** the same user is returned

<!-- tests: tests/test_auth_users_db.py::test_second_login_resolves_by_subject_alone -->

### Requirement: A bound row cannot be taken over
The system SHALL deny a login whose email matches a row already bound to a different issuer or subject, and SHALL log the denial using only a short hash of the email.

#### Scenario: Same email, different subject
- **WHEN** a second identity signs in with the email of a row bound to another subject
- **THEN** resolution returns no user
- **AND** the original bound identity still resolves

#### Scenario: Same subject, different issuer
- **WHEN** a login arrives with a bound subject but a different issuer
- **THEN** resolution returns no user

<!-- tests: tests/test_auth_users_db.py::test_same_email_different_subject_denied, tests/test_auth_users_db.py::test_issuer_mismatch_denied -->

### Requirement: Disabled users are denied
The system SHALL deny login for a disabled allow-list row, including one already bound.

#### Scenario: Disabled after binding
- **WHEN** an admin disables a bound user and that identity signs in again
- **THEN** resolution returns no user

<!-- tests: tests/test_auth_users_db.py::test_disabled_user_denied -->

### Requirement: Allow-list rows have a role of admin or member
The system SHALL accept only the roles `admin` and `member` when inviting, and SHALL treat a repeat invite for the same address (case-insensitive) as an update that sets owner and role, re-enables the row and leaves any bound identity unchanged.

#### Scenario: Unknown role refused
- **WHEN** an invite is made with role `superuser`
- **THEN** it fails with a value error and no row is written

#### Scenario: Re-invite keeps binding
- **WHEN** an already bound address is invited again with a different role
- **THEN** the role is updated and the row is enabled
- **AND** issuer, subject and last-login time are unchanged

<!-- tests: tests/test_auth_users_db.py::test_invite_user_rejects_unknown_role -->
<!-- re-invite scenario: NONE -->

### Requirement: Admins can list and disable users
The system SHALL list allow-list rows ordered by email and SHALL report whether a disable matched a row.

#### Scenario: Disable result
- **WHEN** an admin disables an address that exists, then one that does not
- **THEN** the first call returns true and the second returns false

<!-- tests: tests/test_auth_users_db.py::test_list_users_and_disable_returns_whether_a_row_matched -->

### Requirement: User management is available from the CLI
The system SHALL provide `--auth-invite EMAIL` (requiring `--owner`, optional `--role` of admin or member), `--auth-disable EMAIL` and `--auth-list`.

#### Scenario: Invite without owner
- **WHEN** `--auth-invite` is used without `--owner`
- **THEN** the command logs an error and exits with status 1

#### Scenario: Disable unknown address
- **WHEN** `--auth-disable` names an address with no row
- **THEN** the command logs an error and exits with status 1

<!-- tests: NONE -->

### Requirement: Allow-list status is re-checked periodically
The system SHALL cache the resolved user for a session and SHALL re-run allow-list resolution when the cache is older than 300 seconds or belongs to a different subject.

#### Scenario: Access revoked mid-session
- **WHEN** an admin disables a user whose session cache is older than 300 seconds and the page reloads
- **THEN** resolution runs again and the user is denied

#### Scenario: Within the interval
- **WHEN** the page reloads within 300 seconds for the same subject
- **THEN** the cached user is reused without a database lookup

<!-- tests: NONE -->

### Requirement: Sign-out clears the session
The system SHALL clear the cached app user and end the Streamlit auth session on sign-out.

#### Scenario: Sign out
- **WHEN** a user signs out
- **THEN** the cached app user is removed and the Streamlit logout is called

<!-- tests: NONE -->

### Requirement: Uninvited visitors can request access without gaining it
The system SHALL let a visitor submit a name, email and optional message from the landing page, SHALL require name and email, and SHALL store only a pending request that grants nothing.

#### Scenario: Submit request
- **WHEN** a visitor submits a name and email
- **THEN** a request with status `pending` is stored
- **AND** signing in with that email still resolves to no user

#### Scenario: Missing field
- **WHEN** the name or email is empty
- **THEN** submission fails with a value error and the landing form shows "Name and email are required."

<!-- tests: tests/test_access_requests_db.py::test_submit_request_is_pending_and_grants_nothing, tests/test_access_requests_db.py::test_submit_request_requires_name_and_email -->

### Requirement: Admin review of access requests
The system SHALL list requests newest first (pending by default), SHALL let an admin approve a pending request by inviting its email with a chosen owner and role, and SHALL let an admin deny a pending request. A request that is not pending SHALL NOT be approved again.

#### Scenario: Approve
- **WHEN** an admin approves a pending request
- **THEN** the email is invited, the request status becomes `approved`, and a login for that email resolves

#### Scenario: Approve twice
- **WHEN** an already approved request is approved again
- **THEN** the call fails with a value error

#### Scenario: Deny
- **WHEN** an admin denies a pending request
- **THEN** its status becomes `denied` and no allow-list row is created

<!-- tests: tests/test_access_requests_db.py::test_approve_request_invites_and_marks_approved, tests/test_access_requests_db.py::test_approve_request_twice_raises, tests/test_access_requests_db.py::test_deny_request_marks_denied_and_grants_nothing -->

### Requirement: User management screens are shown only to admins
The system SHALL show the Users and access-request sections of the Profile page only when the signed-in user's role is `admin`.

#### Scenario: Member views Profile
- **WHEN** a signed-in member opens the Profile page
- **THEN** the Users and access-request sections are not rendered

<!-- tests: NONE -->

### Requirement: Admin status for services is decided server-side
The system SHALL treat an owner as admin for service-layer checks only when an enabled allow-list row with role `admin` maps to that owner, and SHALL return false for an empty or unknown owner without raising.

#### Scenario: Enabled admin
- **WHEN** admin status is checked for an owner mapped by an enabled admin row
- **THEN** it is true

#### Scenario: Not an admin
- **WHEN** it is checked for an empty owner or an owner with only a member row
- **THEN** it is false

<!-- tests: tests/test_investor_profile.py::test_global_settings_are_admin_only_even_when_the_ui_is_bypassed -->

### Requirement: Identity can only come from verification
The system SHALL allow a verified principal to be constructed only through the identity verifier or the web login path, and SHALL raise an authentication error for any other construction attempt.

#### Scenario: Hand-built identity
- **WHEN** code builds an identity with its own proof object or no proof
- **THEN** an authentication error is raised

#### Scenario: Typed actor
- **WHEN** a request context is built with a bare actor string
- **THEN** a type error is raised because no such field exists

<!-- tests: tests/test_trusted_context_scoping.py::test_identity_cannot_be_constructed_without_the_verification_proof, tests/test_trusted_context_scoping.py::test_request_context_rejects_a_bare_typed_actor_kwarg, tests/test_trusted_context_scoping.py::test_mint_identity_for_tests_still_requires_the_real_proof_path -->

### Requirement: Web identity comes only from the allow-list owner
The system SHALL mint a web identity only in the login module, from the owner of the matched allow-list row, and SHALL NOT use a local context anywhere under `web/`.

#### Scenario: Reference check
- **WHEN** the repository source outside tests is scanned for the web-identity constructor
- **THEN** it appears only in the login module and its own definition

#### Scenario: No local context in web
- **WHEN** files under `web/` are scanned
- **THEN** none call the local-context constructor

<!-- tests: tests/test_web_auth_wiring.py::test_identity_from_verified_login_referenced_only_in_web_auth_and_tests, tests/test_web_auth_wiring.py::test_no_file_under_web_calls_local_context -->

### Requirement: Core package is independent of Streamlit
The system SHALL keep the `portfolio_agent` package free of Streamlit imports so authentication stays in `web/`.

#### Scenario: Import scan
- **WHEN** files under `portfolio_agent/` are scanned
- **THEN** none import `streamlit`

<!-- tests: tests/test_web_auth_wiring.py::test_no_file_under_portfolio_agent_imports_streamlit -->

### Requirement: Request context carries actor, source and request id
The system SHALL give every request context a read-only actor derived from its verified identity, a source label, and a request id, and a derived child context SHALL keep identity and source with a new request id.

#### Scenario: Batch and CLI actor
- **WHEN** a local or system context is created for a batch or CLI job
- **THEN** its actor is the configured local owner
- **AND** a system context's source is prefixed `system:`

#### Scenario: Child context
- **WHEN** a child context is derived
- **THEN** it has the same actor and source and a different request id

<!-- tests: tests/test_trusted_context_scoping.py::test_verify_identity_always_resolves_to_the_configured_local_owner -->
<!-- system prefix and child scenarios: NONE -->

### Requirement: A missing or fake context is refused
The system SHALL refuse every private operation that receives no verified context, or an object that only looks like one, with an authentication error rather than a lookup error or an unscoped result.

#### Scenario: None context
- **WHEN** a private read is called with no context
- **THEN** an authentication error is raised

#### Scenario: Spoofed context
- **WHEN** a dict or plain string is passed as the context
- **THEN** an authentication error is raised

<!-- tests: tests/test_trusted_context_scoping.py::test_missing_context_fails_with_a_clear_error_not_an_attributeerror, tests/test_trusted_context_scoping.py::test_missing_context_fails_for_a_spoofed_non_context_object -->

### Requirement: Private reads and writes are scoped to the verified actor
The system SHALL scope every service-layer private read and write (operations that take a request context) to the portfolio of the verified actor, and SHALL refuse an actor who has no portfolio instead of returning an empty result or another owner's data. Legacy code paths outside the service layer are not covered; see design.md.

#### Scenario: Stranger refused
- **WHEN** an identity with no portfolio lists holdings, accounts or cash, or creates an account
- **THEN** each call raises a not-authorized error

#### Scenario: Two owners stay separate
- **WHEN** two owners each hold positions and cash
- **THEN** each owner's holdings and cash listings contain only their own rows

#### Scenario: Another owner's account
- **WHEN** an actor acts on an account belonging to another owner
- **THEN** a not-authorized error is raised

<!-- tests: tests/test_trusted_context_scoping.py::test_cross_user_cannot_read_or_write_holdings, tests/test_trusted_context_scoping.py::test_a_second_verified_identity_with_no_portfolio_is_refused_not_given_an_empty_result, tests/test_trusted_context_scoping.py::test_list_holdings_for_never_mixes_two_owners_holdings -->

### Requirement: Each owner has a private profile row
The system SHALL keep one profile row per owner, SHALL create a default row for an owner seen for the first time, and SHALL ensure one owner's write never changes another owner's row.

#### Scenario: Stranger writes own profile
- **WHEN** a second identity reads and writes its profile
- **THEN** it sees an empty default first and its write leaves the other owner's row unchanged

<!-- tests: tests/test_trusted_context_scoping.py::test_cross_user_cannot_read_or_write_user_profile -->

### Requirement: Restricted list and watchlist stay owner-stamped across migrations
The system SHALL keep rows owner-stamped and unduplicated when schema setup runs repeatedly.

#### Scenario: Repeated setup
- **WHEN** schema setup runs several times after one restricted ticker and one watchlist ticker are added
- **THEN** each owner read returns exactly that one row

<!-- tests: tests/test_trusted_context_scoping.py::test_repeated_restricted_list_and_watchlist_migrations_preserve_owner_and_dont_duplicate, tests/test_trusted_context_scoping.py::test_repeated_user_profile_migrations_preserve_id_and_owner -->

### Requirement: Cached private results are re-authorized on every call
The system SHALL check the actor's authorization before consulting or filling the private-result cache, so a warm entry is never served once access is gone, and cache entries SHALL be keyed by actor and resource key.

#### Scenario: Revoked access with warm cache
- **WHEN** an actor's result is cached, the actor's portfolio is then removed, and the same key is requested
- **THEN** a not-authorized error is raised and the result is neither served nor recomputed

<!-- tests: tests/test_trusted_context_scoping.py::test_revoked_access_cannot_retrieve_a_warm_private_cache -->

### Requirement: Investor data never reaches shared forecasts or the prediction pipeline
The system SHALL keep investor profile, reserve, custom allocation, goals, policy and quiz data out of the shared market context, forecast writer, APEX run, weight engine, prediction storage, instrument reference and validation engine, both by source reference and by import graph.

#### Scenario: Source and import boundary
- **WHEN** the shared forecast and pipeline modules are inspected and imported
- **THEN** none reference private investor names and none load the investor service, profile, settings, policy or quiz modules

#### Scenario: Forecast ignores personal choices
- **WHEN** a person picks a profile, sets a reserve, adds a goal and sets a custom allocation
- **THEN** a market forecast context built before and after is identical in digest and payload

<!-- tests: tests/test_investor_profile.py::test_shared_market_and_pipeline_code_never_reads_the_private_investor_data, tests/test_investor_profile.py::test_a_forecast_context_is_identical_whatever_a_person_chose -->

### Requirement: Private terms from every owner are detected before content is shared
The system SHALL collect private terms of at least 5 characters from every owner's profile name and employer, account nicknames and numbers (skipping a nickname equal to its broker name), and goal names (skipping generic goal-type words), and SHALL match them case-insensitively as substrings.

#### Scenario: Another owner's name
- **WHEN** a second owner has a profile name and employer
- **THEN** both owners' names and employers are in the collected terms
- **AND** text mentioning the second owner's name is flagged

#### Scenario: Case-insensitive match
- **WHEN** text contains a private term in different letter case
- **THEN** the term is reported as found

<!-- tests: tests/test_privacy_multi_owner.py::test_collect_private_terms_includes_every_owners_name_and_employer, tests/test_privacy_multi_owner.py::test_find_private_terms_catches_a_non_local_owners_name, tests/test_market_context_boundary.py::test_private_terms_detected_case_insensitively_and_error_never_echoes_them -->
<!-- account-nickname/number and goal-name collection, generic-word skip, 5-char minimum: NONE -->

### Requirement: Shared content containing a private term is refused without echoing it
The system SHALL raise a private-data error when text destined for a shared artifact contains a private term, and the error SHALL name the artifact kind and the count only, never the term. A redaction helper SHALL replace terms with `[private]`.

#### Scenario: Refusal message
- **WHEN** shared text contains a private term
- **THEN** a private-data error is raised that does not include the term

#### Scenario: Shared writers refuse
- **WHEN** a shared forecast or market context is built over text containing a private term
- **THEN** the build or write is refused and the private text is not logged

<!-- tests: tests/test_market_context_boundary.py::test_private_terms_detected_case_insensitively_and_error_never_echoes_them, tests/test_market_context_boundary.py::test_market_context_refuses_to_build_over_private_text, tests/test_market_context_boundary.py::test_private_text_refusal_never_logs_the_private_text, tests/test_market_context_boundary.py::test_write_shared_forecast_refuses_text_containing_a_private_term -->
<!-- redact helper: NONE -->

### Requirement: No order execution or broker connectivity
The system SHALL NOT place orders or connect to any broker or brokerage account; every output is research only, and the pre-login page SHALL state "Not investment advice." in its footer.

#### Scenario: Landing footer
- **WHEN** the pre-login page renders
- **THEN** its footer reads "APEX — internal use only. Not investment advice."

#### Scenario: No execution surface
- **WHEN** the authenticated services are inspected for brokerage clients or order calls
- **THEN** none exist; holdings enter only through imports, manual entry and ledger records

<!-- tests: NONE -->
