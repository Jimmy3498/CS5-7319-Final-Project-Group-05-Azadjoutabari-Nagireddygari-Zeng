# UI reference and user journey

The project has one shared Streamlit interface used by two separate applications:

- Layered: `python -m streamlit run layered/streamlit_app.py --server.port 8501`
- Event-Based: `python -m streamlit run event_based/streamlit_app.py --server.port 8502`

The pages live in `shared/ui/`. They communicate only through `ApplicationGateway`; UI code does not
open SQLite, call an AI provider or read the credential vault directly.

## Shared interaction model

Each application supplies a gateway to the shared `ScreenController`. A UI action creates a typed
command or query:

```text
Streamlit widget
  -> Command or Query
  -> ApplicationGateway
  -> OperationReceipt or authorized query DTO
  -> render status/result
```

Layered commands normally complete during `submit()`. Event-Based commands are queued, so the page
stores the operation ID and provides **Refresh status** until the receipt becomes terminal. Streamlit
reruns poll an existing operation; they never resubmit it automatically.

The browser session keeps a stable client-session ID. Server-side authentication resolves the account
from that ID. Account identity is never accepted from a user-editable widget.

## Pages and components

### Overview

- Shows the active architecture.
- Explains that the UI is shared while coordination differs.

### Account access

- A compact profile control stays at the top right of every workspace.
- When signed out, **Sign in** opens sign-in and registration forms without leaving the current page.
- When signed in, the control displays the username and opens a small account menu with **Sign out**.
- Account is not a separate navigation workspace.
- Logout clears account-scoped editor, session, card and operation state.

### Browse Campaigns

- Public catalog query; sign-in is not required to browse.
- Client-side title filter.
- Campaign summary and published version.
- Preset-character selection using the character ID/name pair supplied by the catalog DTO.
- **Start playthrough** creates an independent session and opens the play page.

### My Campaigns / Editor

- **Create campaign** form.
- Owned-draft selector and revision indicators.
- Local `EditorBuffer`; manual changes do not persist until **Save draft**.
- Setup editor for title, catalog summary, background and opening.
- Friendly forms for canon, characters, locations, item templates/instances, relationships,
  narrative rules, flags and milestones, with removal controls.
- Advanced typed JSON editor for the complete campaign document. Unknown or malformed fields reject.
- **Check readiness** displays structured path/code/message issues for the saved draft.
- **Publish version** creates an immutable catalog version after readiness validation.
- Save conflicts leave the local buffer intact.

#### AI coeditor

- Record-scope multiselect and bounded instruction field.
- **Request proposal** uses the active private API card.
- Proposal summary and field-level before/after diff.
- **Accept into buffer** changes only the unsaved local buffer.
- **Reject proposal** leaves the buffer unchanged.
- Saving and publishing remain separate explicit actions.

Editor proposals are stored in SQLite with owner, campaign, base revisions, candidate, diff and review
status. Provider credentials remain outside SQLite in the credential vault.

### My Sessions / Play

- Owned-session selector.
- Resume query returns only the player-authorized `PlayerView`.
- Accepted narration history and character-visible facts.
- Free-text turn form and confirmed narration after atomic persistence in both applications.
- Failed/rejected actions expose explicit linked retry; no automatic provider replay.
- Compatible newer campaign versions can be previewed and adopted explicitly.

### Activity & Milestones

- Campaign selector with playthrough and accepted/rejected/failed turn metrics.
- Idempotent milestone playthrough/account counts, denominators and projection freshness.

### API Cards

- Owner-scoped card list and active-card status.
- Create/update form for card name, protocol, endpoint, model and API key.
- Protocol choices: OpenAI-compatible, Anthropic Messages and Google Gemini generateContent.
- First-party presets populate the endpoint and current default model, leaving only the API key
  required for a basic card; every field remains editable for account-specific availability.
- **Activate card** changes the default for the next AI operation.
- **Delete card** supports replacement of an active card or explicitly disabling AI.
- API-key fields are password inputs. Keys are passed as `SecretInput` and stored in the configured OS
  credential vault, not SQLite.

## Reference author-to-player journey

1. Open either application and use **Sign in** at the top right.
2. Create an account if needed, then sign in from the same profile control.
3. Open **API Cards**, save a compatible provider card and activate it. This is required only for AI
   coediting and gameplay generation.
4. Open **My Campaigns / Editor** and create a campaign draft.
5. Edit setup and world content. Use the typed tabs or advanced structured editor.
6. Select records in **AI coeditor**, request a proposal, inspect the diff and accept or reject it.
7. Save the local buffer.
8. Check readiness. Resolve each reported structural issue.
9. Publish the campaign version.
10. Open **Browse Campaigns**, choose a preset character and start a playthrough.
11. Open **My Sessions / Play** to resume the session.
12. Submit actions and read confirmed narration. Retry explicitly if an attempt fails.
13. Preview a compatible campaign update when one is available, then opt in to adoption.
14. Use **Activity & Milestones** to inspect activity and milestone reach.

## UI state and safety rules

- `ui.*` session-state keys are account-scoped and cleared on logout.
- Each button submission receives a new request ID; reruns only poll its operation ID.
- Passwords and API keys are never displayed in receipts or stored in public DTOs.
- Catalog pages use public projections; play pages use `PlayerView`, never raw session snapshots.
- The shared UI never imports either architecture package.
- Post-MVP capabilities remain outside the UI instead of reporting false success.

## UI verification

Run:

```sh
python -m unittest tests.test_ui -v
python -m unittest discover -s tests -v
```

The UI tests render every page in both entrypoints, exercise register/login in both architectures, and
exercise campaign create/edit/save through the Layered entrypoint.
