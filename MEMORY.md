# Project Memory

## Purpose

- Direct, serverless CLI for QOJ contests, forked from `Minstdfx/qoj-submitter`.
- Preserve DOMjudge-like single-file submission ergonomics without replacing the system `submit` command.
- Treat `qoj.ac` and `contest.ucup.ac` as separate origins; do not silently switch hosts or assume every contest exists on both.

## Implementation

- `qoj_submitter/cli.py`: command parsing, per-host configuration, cookie storage and output handling.
- `qoj_submitter/client.py`: QOJ login, live login status, contest parsing and submission protocol.
- `qoj_submitter/curl_adapter.py`: libcurl transport for `qoj.ac`; UCup uses requests' standard transport. Security challenges can still occur.
- `qoj_submitter/samples.py`: attachment downloads, safe ZIP inspection and DOMjudge-style archive creation.
- Credentials are entered interactively. Never persist passwords or expose cookies, CSRF tokens or credentials in logs or documentation.
- Keep account sessions and contest bindings per host; `qoj status` must verify login against the active host, not infer it from a cookie file.
- A submitted solution can reach QOJ even if the CLI cannot confirm its ID. Do not retry a submission automatically or claim success without evidence.

## Samples

- Default output is `samples.zip`, with `samples-<label>/<n>.in` and optional `.ans`. Do not overwrite existing outputs without explicit `--force`.
- Preserve unpaired inputs and original special attachments; report missing answers and missing attachments. Never invent official output.
- In contest 3387, the generated 40 files matched the existing reference archive by filename and bytes.
- Contest 3575 produced 35 input/answer pairs. Contest 2668 G contains interactive material without static answers and I has no attachment.
- Contest 4581 A contains nonstandard materials and D includes an input and test tool without a static answer.
- ZIP input is untrusted: reject unsafe paths, symlinks, duplicate names and oversized archives; do not extract attachments to the filesystem.

## Verification

- Run `python -m unittest discover -s tests -v`; GitHub Actions exercises Python 3.10, 3.12 and 3.13.
- Run `qoj status` and `qoj samples -c <contest> -o <empty-directory>` for live checks; avoid modifying a user's existing archives.
- Test submission only with explicit user authorization, and inspect the submission list before retrying an uncertain result.
- GitHub installation uses pipx against the repository; `pipx reinstall qoj-submitter-direct` refreshes that installation.
