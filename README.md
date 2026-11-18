# CS7319 Group 5 Storytelling Project

Team: Farhad Azadjoutabari, Yiming Zeng, Vimala Nagireddygari

Authors create and publish campaigns; players use preset characters, submit actions and resume saved sessions. AI proposes changes and narration. Application rules validate state changes and control which facts players can see.

**Selected is Layered. Unselected is Event-Based.** Both implement the same campaign and gameplay workflows and run independently with Streamlit and SQLite.

## Repository layout

```text
README.md
README.docx
Selected/
  layered/              Layered gateway and workflow coordinator
  shared/               Reusable UI, domain services and adapters
  fixtures/             Example campaigns
  scripts/              Build helper
  requirements.txt
  run.py
  layered-demo.zip      Runnable Python distribution
Unselected/
  event_based/          Event gateway, bus, handlers and private state
  shared/               The same reusable implementation
  fixtures/             The same example campaigns
  scripts/              Build helper
  requirements.txt
  run.py
  event-based-demo.zip  Runnable Python distribution
Others/
  docs/                 Architecture diagrams, contracts and risk evidence
  tests/                Automated regression tests
  scripts/              Development and experiment tools
  run_checks.py         Cross-architecture verification
```

`Others` also contains the proposal, contribution notes and development configuration.

## Architecture and reuse

### Layered implementation

`LayeredGateway` passes commands to `LayeredApplication`, which coordinates domain services through synchronous calls. A gameplay turn follows admission, private planning, effect validation, player-view projection, narration, atomic commit and reporting. The gateway returns the final operation receipt.

The UI accesses services through the gateway. Protocols in `shared/ports.py` abstract storage, provider transport and credentials. Read queries go directly from the gateway to `QueryService`; polling reads `OperationStatusStore`.

### Event Based implementation

`EventGateway` publishes a command and returns a pending receipt. `InProcessBus` dispatches messages to registered methods in `ComponentHandlers`. Each stage publishes the next message. Gameplay advances through admission, planning, validation, narration and commit; result and reporting consumers subscribe separately to the committed-turn event.

`WorkflowStateStore` holds private intermediate state. Messages carry references or committed identifiers, and AI narration receives the authorized `PlayerView`. The in-process FIFO bus has one worker and executes subscribers sequentially. Read queries and status polling are synchronous.

### Reused components and connectors

The two `shared` directories contain identical models, commands, domain services, gameplay rules, privacy controls, Streamlit pages, SQLite persistence, provider adapters and credential storage. Each application includes its own copy to run independently. Shared changes must be applied to both; `Others/run_checks.py` checks their equality.

Both architectures use procedure calls to shared services, repository interfaces backed by SQLite transactions, and HTTP adapters for AI providers. Event-Based adds the queued publish-and-subscribe connector in `event_based/bus.py`. Coordination stays architecture-specific: Layered controls the sequence through direct calls; Event-Based advances through messages and subscribers. Neither application imports the other architecture.

### Design comparison

| Aspect | Layered in Selected | Event-Based in Unselected |
| --- | --- | --- |
| Coordinator | LayeredGateway → LayeredApplication | EventGateway → InProcessBus → ComponentHandlers |
| Gameplay turn flow | One ordered call chain: prepare, plan, validate, project, narrate, commit, report | Six staged topics from turn.requested to turn.committed, each handled by a separate handler |
| Request behavior | submit() returns after the whole turn completes | submit() enqueues and returns; the UI polls for the result |
| Reporting | Direct call after commit; failure returns the turn with reporting pending | Independent subscriber to turn.committed |
| Private stage state | Local variables within one call | WorkflowStateStore with opaque work references |
| Recovery | Reporting catch-up replays durable committed notices | CompletionRecovery republishes committed-notice IDs on startup |
| Extension | The coordinator names its services; workflow changes modify the call sequence. | Topic subscriptions add consumers without changing the publisher; consumers depend on message and workflow-state contracts. |
| Execution and delay | The caller waits for provider calls, commit and reporting. | One FIFO worker executes handlers and subscribers sequentially; slow handlers delay queued work. |

Both implementations use atomic SQLite commits, durable attempt records and committed notices. Startup marks interrupted running attempts failed. Event-Based messages that have not reached durable admission remain volatile. Read queries and status polling use synchronous gateway calls in both applications.

### Why Layered was selected

Our priority is a small, locally runnable author-to-player workflow. Each turn must validate a proposed change, prepare permitted player information and save the result before confirming success. Layered expresses those dependencies in one ordered coordinator, which makes tracing failures and writing workflow tests simpler. It uses fewer coordination mechanisms than the Event-Based implementation's topics, retained stage state, subscriptions and asynchronous receipts. Provider and database details remain behind shared interfaces in either design.

Event-Based is useful when multiple downstream features should react independently to a committed turn. Our MVP has result delivery and basic reporting, so that extension benefit does not outweigh the additional coordination work. The current single-worker queue also means a slow handler delays unrelated queued work.

The risk experiments support this trade-off. In E1, a 20-event burst with a 0.5-second handler produced 9.011 seconds of p95 queue wait; E2 discarded ten queued events on controlled shutdown. Layered also exposes delay: L1's two six-second AI calls produced a 12.001-second median response, and L2 showed reporting delay extending the response time. These experiments isolate different mechanisms rather than rank end-to-end speed. We accept Layered's synchronous wait in exchange for simpler control flow. P1, R1 and R2 verified the tested privacy and recovery safeguards in both designs. Procedures and measurements are in `Others/docs/risk/REPORT.md`.

### Refinements from the proposal

The two candidates and the Layered selection are unchanged from the proposal's Architecture and Final Decision sections. The implementation refines the proposed design as follows:

**Storage and deployment.** SQLite replaces the proposed MongoDB adapter for a self-contained local demo with transactional updates and no database-server setup. The Streamlit UI calls the application gateway locally instead of using a separate application HTTPS interface. External AI calls still use HTTP adapters.

**Event processing.** The proposal left the messaging mechanism to development. The implementation uses an in-process, single-worker bus to demonstrate queued coordination without deploying a broker or managing parallel consumers.

**Authoring and gameplay scope.** Typed forms, structured JSON import and selected-record AI editing replace automatic extraction from uploaded prose. Sessions use authored preset characters. Timed or participation-count campaign closure is omitted to keep the demo focused on authoring and turn execution. Structured input supports validation and reviewable changes; preset characters keep both builds on the same testable workflow. Marketplace, payments, subscriptions and advanced administration were already deferred in the proposal's MVP plan.

**AI access and reporting.** Per-account API cards configure compatible chat-completion, Anthropic and Gemini HTTP endpoints, with keys in the OS credential vault. Basic milestone reports provide author feedback; the proposed AI-generated campaign-improvement recommendations are deferred to keep the demo focused.

## Install and launch

Download the project from https://github.com/Jimmy3498/CS7319-storytelling-project using Code → Download ZIP, then extract it.

The implementation uses Python 3.12 or newer, Streamlit 1.66.1, SQLite bundled with Python, and keyring 25.x–29.x. The current version was verified on macOS 26.5.1 with Python 3.14.3. Both architectures use the same dependency list.

Download Python from https://www.python.org/downloads/macos/ or https://www.python.org/downloads/windows/. Run the installer with pip included; on Windows, include the Python launcher (`py`). Reopen the terminal, verify the version, then run the commands below from the repository root. The dependency command installs Streamlit and keyring automatically.

### macOS Terminal

```sh
python3 --version
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r Selected/requirements.txt
```

### Windows PowerShell

```powershell
py -3 --version
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r Selected/requirements.txt
```

If PowerShell blocks activation, use `.\.venv\Scripts\python.exe` in place of `python` in the commands below and the dependency installation command above.

### Launch either architecture

With the environment active, run from the repository root:

```sh
# Layered — http://127.0.0.1:8501
python Selected/run.py

# Event-Based — http://127.0.0.1:8502
python Unselected/run.py
```

Use separate terminals to run both. Stop each server with Ctrl+C. Run only one server per application data directory.

## Build each demo

Python executes the source directly; no separate compiler installation is needed. The build commands package each application into its own runnable ZIP. With the environment active, run from the repository root:

```sh
cd Selected
python -m layered.build
cd ..

cd Unselected
python -m event_based.build
cd ..
```

Outputs are `Selected/dist/layered-demo.zip` and `Unselected/dist/event-based-demo.zip`. Each ZIP contains its architecture, shared runtime code, fixtures, dependencies and server configuration. These are Python distributions and require Python to run. For a repeat build, supply a new output directory, such as `python -m layered.build --output dist-review`.

The ZIPs directly inside Selected and Unselected are supplied builds. To run one, extract it, open a terminal inside the extracted application folder, and use an active Python environment:

```sh
python -m pip install -r requirements.txt

# Layered distribution
python -m streamlit run layered/streamlit_app.py

# Event-Based distribution
python -m streamlit run event_based/streamlit_app.py
```

Run the command matching the extracted distribution. Rebuild the ZIP after changing source.

## First use

1. **Register and sign in** using the account control at the top of the page. Each architecture has separate accounts and data.
2. **Create a draft** in My Campaigns / Editor. To import the example, open `fixtures/thornmere-video.json` inside the architecture folder. Copy the value of `draft.content` into Advanced structured campaign editor, then click Apply structured document.
3. **Save and publish:** Save draft → Check readiness → Publish version. Character edits and accepted AI proposals must be saved before publication.
4. **Configure AI** in API Cards: save and activate a card with your provider endpoint, model and key. For DeepSeek, use Custom compatible endpoint, protocol `chat-completions-compatible`, endpoint `https://api.deepseek.com/chat/completions`, and model `deepseek-chat`. Enter the key without a `Bearer` prefix. AI coediting and gameplay require a key and may incur charges; manual authoring and publication do not.
5. **Start a playthrough** from Browse Campaigns. Submit actions and resume sessions in My Sessions / Play. Use Refresh status for pending Event-Based operations. Activity & Milestones displays reports.

## Data and tests

Databases are stored at `Selected/.local/layered/application.sqlite3` and `Unselected/.local/event_based/application.sqlite3`. Saved campaigns and sessions survive restarts; unsaved edits do not. API keys are stored in the OS credential vault.

Run checks from the repository root with the environment active:

```sh
python -m pip install -r Others/requirements-dev.txt
python Others/run_checks.py
```
