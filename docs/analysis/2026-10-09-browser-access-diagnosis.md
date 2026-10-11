# Browser access: confirmed original denial and successful fresh-chat recovery

## Conclusion

Date: 2026-10-09, Asia/Seoul. **Recovery succeeded in the user-authorized fresh
chat `01a11c68-dc66-7b60-b39d-0c14a6716e8a`.** The sanctioned Chrome extension
opened both `http://127.0.0.1:7667/` and `https://ref.theseeker.io/r/7526`.
Desktop and 390 x 844 responsive portal checks are now executed; the detailed
results below supersede the earlier unexecuted recovery checkpoint. No browser
permission file, product code, data archive or source-polling ledger was changed.

The original chat `01a100b3-6786-7330-82bf-3a4bb6f44ef4` had persisted denials for
the two requested origins. At its diagnosis checkpoint global browser approval was
set to allow without prompting. The installed browser runtime explicitly gives
conversation denials precedence over that global permission. This explains why
changing the visible default, restarting the app and creating another Chrome
window did not remove the original chat's navigation denial.

The cause is confirmed by the actual permission bytes and installed decision
code, not inferred merely from the error text. Who or which earlier approval
response created the denial is not established. No permission was changed here.

## Scope and baseline

- Repository: C:/2026_project/wisdome-super-writer; branch codex/policy-funding,
  HEAD db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc plus the existing uncommitted
  funding, portal and monthly changes. This diagnosis changes documentation only.
- Authorities: project AGENTS, shared analysis policy and
  [portal permission history](2026-10-08-topic-portal-progress.md).
- User requested root-cause investigation after repeated browser failures.
  Read-only installed-runtime/configuration/process checks are within that scope.
- Purpose: recover direct browser acceptance for the three-topic operator portal;
  its source implementation and successful in-process tests are separate from
  actual Chrome/mobile acceptance.
- Running Windows app: OpenAI.Codex_26.1002.7124.0_x64__2p2nqsd0c76g0.
  Browser runtime: cua_node/3dd31cfff853001c, @oai/browser-desktop.

## Actual configuration evidence

Global file `C:/Users/강지혜/.codex/browser/config.toml` contains:

```toml
history_approval_mode = "never_ask"
approval_mode = "never_ask"
download_approval_mode = "never_ask"
full_cdp_access_enabled = true
iab_history_approval_mode = "never_ask"
```

Its measured modification time is October8 20:37:40 KST. The runtime's origin
permission code treats `approval_mode="never_ask"` as global approval when the
managed persistence checks permit it; it is not itself an origin denial.

The conversation-specific file
`C:/Users/강지혜/.codex/browser/sessions/01a100b3-6786-7330-82bf-3a4bb6f44ef4.toml`
contains exactly:

```toml
[origins]
denied = [
    "https://ref.theseeker.io",
    "http://127.0.0.1:7667",
]
```

Its measured modification time is October8 20:27:32 KST. The subsequent global
default update therefore left the older conversation-specific deny intact. The
original diagnosis's CUA inventory codexSessionId matches this file's conversation ID.
No wildcard or guessed host is needed: both denied origins are exact matches.

## Installed decision code

Read-only inspection of `@oai/browser-desktop/scripts/browser-service.mjs` shows
`maybeAutoAnswerBrowserUseRequest` reading both global and conversation snapshots.
Its return expression checks a matching conversation `origins.denied` entry
first and returns `decision:"deny", scope:"conversation"` with source
`browser-use-persisted-state`. The global `never_ask` approve branch comes later.

The same installed runtime and client map `persisted_user_denied` to the observed
message `A saved user permission setting blocks this action.` with decisionSource
`user_persisted_setting` and retryable=false. The session file plus this precedence
is sufficient to explain the observed failure. A hypothetical cache bug or broken
global-setting save is not needed to explain it.

This also fits the supplied about:blank screenshot: window creation happened,
while navigation remained denied. That exact launch/navigation ordering is an
inference from the screenshot and error, not a new browser execution trace.

## Other checked boundaries

- Sanctioned `cua.getState()` succeeded and returned only the Codex in-app browser
  and MCP Apps; Chrome was absent. Windows process inspection also found no
  chrome.exe. Chrome currently being closed is a separate present connection
  condition; it does not explain the earlier persisted-origin denial.
- The main `.codex/config.toml` has no browser_use override. Checked user and
  two likely ProgramData requirements.toml paths do not exist. Cloud/other managed
  requirements were not fully enumerated, so do not claim all policy is absent.
- Installed app/process paths were inspected without command-line credentials.
  Recent app log filenames were located, but the selected browser-permission
  string search did not yield a decisive app log trace. Configuration and runtime
  source, rather than those logs, establish the diagnosis.
- The Plugin Management permissions connector explicitly excludes Chrome/code
  plugins; it was not used to query or modify Chrome permissions.
- No auth files, cookies, browsing history or unrelated site permission contents
  were read. No alternate browser/CDP/native/HTTP path accessed a denied origin.

## Historical recovery recommendation and limits at the original diagnosis

The required recovery is a supported renewal/reset of this conversation's origin
permissions. Changing the global default again will not remove a stronger saved
conversation deny. App or Chrome restart also leaves this persisted file intact.

Official [browser extension troubleshooting](https://learn.chatgpt.com/docs/chrome-extension)
includes starting a new ChatGPT Work or Codex chat to clear chat-specific state,
using the browser/profile where the extension is installed, and submitting
`/feedback` with the chat ID if the issue persists. A user-initiated fresh chat
provides a fresh permission context; use the sanctioned browser tool and its normal
authorization flow there. Do not transplant or directly edit permission files,
force a new context programmatically to evade a denial, or bypass browser security.

Chrome connection also needed restoration in the authorized browser context because
it was closed at that checkpoint. No new chat, Chrome navigation, permission reset, app
restart or reinstall was performed as part of this diagnosis. Recovery is a
recommended next action at that historical checkpoint, not an executed fix there.

## Durable evidence and handoff

Private diagnostic helpers/results are under `.local/reviews/browser-access/`:
`inspect_local.py`, `local-inventory.json`, `inspect_browser_code.py`, and
`browser-code-evidence.json`, plus `confirm_diagnosis.py` and
`confirmed-diagnosis.json`. Their reads are local and do not automate a browser.
The portable conclusion and exact relevant values are in this Markdown file.

The confirmation helper passed assertions against the actual global/session
configuration, their update order and the installed runtime's decision order.
Final Markdown verification passed14 UTF-8 files,140 local links and balanced
fences. `git diff --check` passed with existing Windows newline warnings. One
earlier inline check failed from PowerShell backtick interpretation; the saved
checker then verified the actual Markdown. No product test rerun was required
for this read-only configuration diagnosis and documentation change.

For future work, read this report before repeating browser retries or default
permission instructions. Recheck current conversation identity and its persisted
origin decisions before treating historical denials as current. Preserve the
[monthly acceptance](2026-10-08-monthly-data-update.md) and outstanding direct
Chrome/mobile acceptance; the preview remains task-owned on127.0.0.1:7667.

Update log: October9 confirmed the conversation override and corrected the earlier
unresolved settings-versus-tool mismatch hypothesis. No product or global policy
change, external message, commit or push occurred.

## Executed fresh-chat recovery and Chrome acceptance — 2026-10-09 KST

### Scope, baseline and effective permission evidence

- Fresh chat identity is reported by the sanctioned CUA inventory as
  `01a11c68-dc66-7b60-b39d-0c14a6716e8a`; Chrome is extension browser `3`.
  Its dedicated session was named "Browser recovery" (displayed in Korean).
- Repository remains `codex/policy-funding` at
  `db5edec35aad39ae85e3aca7b6f9e1b9f2e3a0dc`, with existing uncommitted funding,
  portal, monthly source/tests and documentation preserved. This recovery only
  adds evidence and refreshes documentation. Earlier 1,689-test evidence remains
  historical; no full suite, migration, collection or publishing action ran here.
- `Get-NetTCPConnection -State Listen -LocalPort 7667` returned the existing
  loopback listener owned by PID 30240. The earlier preview session 22363 was
  reused without starting or stopping another server.
- The normal `cua.createBrowserTab("chrome", exactUrl, sessionOptions)` call
  returned title **Content portal / Wisdome** and the three Korean topic links.
  A second normal Chrome tab opened the exact reference URL and its solar-permit
  dashboard title, description, palette and initial 0:00 / 0:23 video frame.
  Actual successful navigation establishes effective permission for both origins
  in this chat. Original permission-file bytes were neither changed nor reread;
  success here does not establish that the original chat's denies were removed.
- The official [browser-extension troubleshooting page](https://learn.chatgpt.com/docs/chrome-extension)
  was searched and opened again. Its fresh-chat step supports this procedure;
  the successful tool/page results, rather than documentation alone, prove recovery.

### Actual portal checks

The service purpose is the three-topic operator workspace described in the
[service report](../service-analysis.md). The root links to independent housing,
semiconductor and funding routes; shared responsive navigation wraps separate
validated local inventories. Navigation reads saved observations and does not
collect or publish.

| Chrome check | Actual result |
| --- | --- |
| Root / desktop | Correct page title, three topic cards, left topic rail and portal menu. Pale-gray canvas, white cards, black active controls and lime accents visually agree with the supplied reference direction and inspected initial reference frame. No pixel-identical or complete-video claim. |
| Housing / October | Native year/month controls submitted 2026/10 with schedule basis. Union 23, special 18, first 21, with overlap. Regional reception labels and a September-published notice with October first-priority reception remained visible. Unknown stage dates and partial collection were explicit. |
| Semiconductor / October | Original-publication basis displayed 2 saved local metadata records, separately labelled original and capture dates, and source/use limits. Source configuration count was not represented as collection coverage. |
| Funding / October | Posting/registration basis displayed 17 notices, with source registration dates separate from application windows and capture times. Detail opened immutable run `2026-10-08--run-fd18f61df3fc`; returning to the list preserved 2026/10 and live mode. |
| Independent menus | Housing dashboard/notices/archive/sources; semiconductor dashboard/news/drafts/sources; funding notices/guide/glossary/sources/history were visible and their internal destinations rendered successfully. Database drafts remained explicitly unavailable. |
| Period and search behavior | Year-only and all-period controls worked on news/funding; housing year-only worked and its initial all-period dashboard showed 175. Housing and funding no-match searches displayed honest empty results. Housing previous-month navigation preserved query/stage; funding period changes preserved query/mode. |
| Responsive menus | Official `viewport.set({width:390,height:844})` exposed quick topic switching and independent mobile menus. Pointer activation worked for all three; focused funding summary toggled closed/open with Return. Root, core topic screens, lists and inspected internal destinations had `scrollWidth == clientWidth` (375, or 390 for the short archive). |
| Desktop layout measurement | Default viewport was 1628 x 767 CSS pixels; document/client width 1613 with vertical scrollbar and no horizontal overflow on the three filtered topic screens and final root. Temporary viewport override was reset. |

### Portable evidence and capture limits

Captures, actual DOM snapshots and measurements are saved under
[`docs/acceptance/browser-recovery-2026-10-09/`](../acceptance/browser-recovery-2026-10-09/).
The [JSON ledger](../acceptance/browser-recovery-2026-10-09/browser-evidence.json)
contains URLs, selected controls, viewport geometry, rendered text and final tab
handoff. Key usable captures:

- [Root desktop](../acceptance/browser-recovery-2026-10-09/portal-desktop.jpg),
  [reference initial frame](../acceptance/browser-recovery-2026-10-09/reference-desktop.jpg).
- [Root mobile full page](../acceptance/browser-recovery-2026-10-09/portal-mobile-full.jpg),
  [housing October mobile](../acceptance/browser-recovery-2026-10-09/housing-october-mobile.jpg),
  [funding October mobile](../acceptance/browser-recovery-2026-10-09/funding-october-mobile.jpg),
  [semiconductor October mobile full page](../acceptance/browser-recovery-2026-10-09/semiconductor-october-mobile-full.jpg).

Some viewport screenshot calls timed out after 5 seconds or returned scaled
375 x 177 composites despite a 390 x 844 DOM viewport. Those retained images are
excluded from detailed visual acceptance. The documented `fullPage: true` API
produced readable root/news mobile captures and resolved that evidence gap.
One `getByLabel` locator missed the rendered year control; fresh DOM inspection
and the exposed combobox role selected it successfully. Neither tool issue
reproduced a product defect, so no service-code repair or test-suite rerun was needed.

### Limits, priorities and handoff

No material new portal defect was reproduced in this scope. Browser recovery and
the requested desktop/responsive menu/period/overflow checks are accepted.
Remaining limits: this is Chrome on Windows with a responsive viewport, not a
physical mobile device/touch/assistive-technology certification; not every filter
combination, external source link or the complete reference video was exercised.
Full collection coverage, distributed storage, authenticated console, article
generation and publishing acceptance retain their existing separate gates. No
new product decision is required by these results.

Final live deliverable: Chrome tab `1200383932` at `http://127.0.0.1:7667/`, marked
with `markDeliverable()`, viewport reset to default. Reference research tab was
closed after inspection. Preview PID 30240 remains running. No browser permissions,
extension installation, alternate browser/HTTP/raw-CDP access, external message,
commit or push occurred. Future browser work must still obey its current chat's
normal authorization decision.

Update log: October 9 fresh-chat recovery executed successfully; actual Chrome
desktop/responsive acceptance replaces the prior browser blocker for this chat.

### Final evidence verification

`PYTHONUTF8=1 .venv/Scripts/python.exe .local/reviews/browser-access/verify_recovery.py`
completed exit 0: 35 saved browser states, zero horizontal-overflow failures,
expected 23/18/21/17/2 counts, selected period/query/run/return assertions and
six usable JPEG captures verified. Seven affected canonical Markdown files passed
UTF-8, local-link and balanced-fence checks. The [verification result](../acceptance/browser-recovery-2026-10-09/verification.json)
and [current source-byte manifest](../acceptance/browser-recovery-2026-10-09/source-baseline.json)
are portable evidence. The latter covers 447 Python/HTML/CSS/JavaScript files in
src/tests with aggregate SHA-256
`2214241f51746f57cc6918dd67c54665f67e7634a0983450df1200fc63c9ff61`;
it is a current fingerprint, not a pre/post mutation comparison or semantic test.
The initial helper mistakenly expected the word "connection" in the unavailable
draft message; its assertion was corrected to the actual recorded Korean text
before the successful run. That was a verification assumption failure, not a
product repair. `git diff --check` passed with existing Windows newline warnings.

## User-requested recovery dispatch — 2026-10-09

The user then explicitly requested recovery after the proposed supported fresh-chat
procedure. The sanctioned browser tool successfully opened a dedicated Chrome
about:blank session (browser4, tab1200383929), restoring the closed browser's
connection. No denied-origin navigation or permission-file change occurred here.

A user-owned local project recovery task was created through the app's official
thread tool: `01a11c68-dc66-7b60-b39d-0c14a6716e8a`, title
`브라우저 복구 및 Chrome 검증`. It uses the same existing working tree and reports,
its own normal permission context, and the sanctioned Chrome API. It must respect
any new denial and must not edit permission files or use alternate access paths.
At dispatch, localhost/reference navigation and desktop/mobile acceptance were
pending. The recovery task subsequently completed successfully: both exact URLs
opened in sanctioned Chrome; topic menus, period selection, detail/return state
and390 x844 responsive overflow were verified. Its actual captures and complete
acceptance are documented above. The parent read back the updated report and
AGENTS, confirmed the capture files and verified14 Markdown files/157 local links.
The original chat's deny entries remain unchanged; subsequent browser operations
should continue in the accepted recovery conversation's normal permission context.
