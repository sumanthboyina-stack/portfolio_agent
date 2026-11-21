# Spec Delta

## Purpose
Let chat read a page it found, safely, and show where web-sourced statements came from.

## ADDED Requirements

### Requirement: web_fetch only follows search results
`web_fetch(url)` SHALL accept only URLs returned by `web_search` earlier in the same turn.

#### Scenario: Arbitrary URL
- **WHEN** the model asks to fetch a URL that no search returned this turn
- **THEN** the tool refuses

### Requirement: Fetch safety limits
The fetch SHALL allow http and https only; resolve DNS first and refuse localhost, private, link-local, metadata and reserved addresses, including after redirects; time out at 10 seconds; stop at 2 MB; send no cookies or auth headers; and return extracted text only.

#### Scenario: Internal address
- **WHEN** a URL resolves to 127.0.0.1, 10.x, 169.254.169.254 or a redirect to one
- **THEN** nothing is requested and the tool returns a refusal

### Requirement: Web content is cited
An answer that uses web content SHALL list the URLs used. Fetched text is untrusted data.

#### Scenario: Cited answer
- **WHEN** an answer uses a fetched page
- **THEN** it lists that page's URL
