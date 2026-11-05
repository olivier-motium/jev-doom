# Jev Doom

Watch [Jev](https://typesafe.ai/) make structured decisions in a real Freedoom game. A local dashboard shows the game, choices, probabilities, navigation, latency and cost. Change its standing orders while it plays.

Use **TypeSafe direct** or **Vercel AI Gateway**. This is an independent experiment, not an official TypeSafe, Vercel or Doom product.

## Quick start

Requires macOS or Linux, **Python 3.10–3.13**, **Node.js 22+**, and internet access for setup. Python 3.13 is recommended. Windows users can try WSL2; it has not been validated. Some systems need the native libraries listed in the [ViZDoom installation guide](https://vizdoom.farama.org/introduction/installation/).

```sh
git clone https://github.com/olivier-motium/jev-doom.git
cd jev-doom
./setup.sh
./run.sh
```

Open **http://127.0.0.1:8767**. Select **Local rule, no model** and start a run without an API key. Setup creates an isolated Python environment, installs the locked Node dependencies, and downloads **Freedoom 0.13.0** from its official release. No proprietary Doom assets are needed or included.

If your default Python is newer, use `PYTHON=python3.13 ./setup.sh`. To use a different port: `PORT=8768 ./run.sh`.

## Connect Jev

1. Edit the generated `.env` file. Set one of the keys below. Keep the unused line blank.
2. Restart `./run.sh`, then select the provider in the dashboard.
3. Click **Check provider**. This makes one billed API request. Once it passes, click **Start fresh run**.

| Dashboard provider | Server environment variable | Get access |
| --- | --- | --- |
| TypeSafe direct | `TYPESAFE_API_KEY` | [TypeSafe console](https://console.typesafe.ai/) · [API quick start](https://docs.typesafe.ai/introduction/quickstart) |
| Vercel Gateway | `AI_GATEWAY_API_KEY` | [AI Gateway setup](https://vercel.com/docs/ai-gateway) · [Jev model](https://vercel.com/ai-gateway/models/jev) |

Direct requests use `https://api.typesafe.ai/v1/systemone` with `jev-latest`. Gateway requests use AI SDK's experimental evaluation API with `typesafe-ai/jev`. Provider access, availability and billing depend on your account. The keys stay in the Python/Node server processes; the browser receives only readiness flags. Do not put private information in standing orders: game state and orders are sent to the selected provider.

The default run lasts **90 seconds**. The default controller is **Local rule, no model**: it needs no API key and makes no provider calls. Choose Jev for model decisions. Pause, stop, change orders, or switch maps between runs.

## Replay without a key

Each run records `episode.lmp`, its decision trace, and its result in `runs/<id>/`. To replay one locally:

```sh
./run.sh --replay runs/<id>
```

`./run.sh --demo` records a seeded local MAP01 run of up to 60 game seconds on first use, then replays it. Later uses replay the saved demo. Replay uses the recording and trace; it does not start Node or call a model. Keep `runs/` if you want to keep recordings.

## Spending and local data

The default **$15 cumulative charged-or-reserved cap** covers both providers, including provider checks, and persists across restarts in `runs/budget.json`. Each Jev request reserves $0.05. When a valid reported cost arrives, it replaces the reservation. Unknown costs keep the reservation; launch-price estimates never replace it. The cap can therefore stop a run before $15 has actually been billed. The time selection is a maximum, not a guarantee: a fresh budget allows at most 300 unknown-cost requests, including checks, and high call frequency can exhaust it sooner. A request could cost more than its reservation, so also configure provider account limits.

Change `JEV_BUDGET_USD` in `.env` and restart to change the cap. Run traces and replies stay in the ignored `runs/` directory. Keep the budget file if you want spending continuity. No requests are made until you click a billed action.

## What this measures

Jev receives **observable** state as structured engine observations: visible entity identities and positions, viewed floor cells and wall pieces, and remembered observations with ages. This is **not a pixels-only agent**. Evaluator kill, hit and damage counters are withheld from model state. Code builds legal choices and executes the chosen motors. The rendered map and evaluator statistics are for inspection.

The engine runs at 35 Hz. In real-time mode, a chosen option continues while another decision is pending. When the option ends, controls are released until a valid decision is available. Startup and explicit pause stop the simulation; during recovery, an already-started real-time run continues with released controls. `pause_assisted` timing is a separate API option. Game time and wall time differ. MAP01 and MAP02 are supported; later maps are unvalidated. A terminal episode is not automatically a completed level. This repository makes no level-completion or model-performance claim.

The local rule and a scripted delayed decider were compared with the earlier code on MAP01 and MAP02, seeds 41 and 42. Each current run lasted 1,040 game ticks. The baseline local-policy runs each made 209 requests; the current local runs made 92–108, a 48–56% reduction. Seen cells changed from 623 to 575, 552 to 640, 612 to 560, and 545 to 569, respectively. No current local run died.

With replies delayed by 20 ticks, the earlier runs made 51–53 requests; the current runs made 45–47. The current runs released controls for 413–652 of 1,040 ticks. These results missed the planned request and idle-time targets. One baseline MAP02 run died at tick 1,000; no current delayed run died. These are model-free tests and do not establish Jev performance.

## Architecture

`jevdoom/app.py` starts the local application or replay. The `jevdoom/core` package builds observations, paths, tactics and controls. Runtime adapters own the engine, recording, storage, dashboard and provider gateway. `gateway.mjs` connects to Jev providers. The dashboard polls state and displays an MJPEG stream and decision timeline. Provider replies are validated before use.

**Vercel support means AI Gateway access, not hosting this application on Vercel.** The native engine, long-lived process and local files require a local machine or a suitably managed server. This release binds only to loopback and has no multi-user authentication. Do not expose it directly to the internet.

## Troubleshooting

- **Python/Node version error:** install Python 3.13 and Node 22+, then make sure those versions are on your PATH. Remove only this project's `.venv` if recreating it with another Python.
- **No game frame:** inspect the terminal and dashboard error. Rerun `./setup.sh` for missing assets or dependencies. Linux may require ViZDoom's native system packages.
- **Provider check fails:** verify the selected key in `.env`, restart, and check account access or credit. The dashboard displays the provider error. A failed request may retain a cost reservation.
- **Budget prevents calls:** inspect `runs/budget.json` and your provider bill before deliberately increasing the cap. Restarting does not reset it.
- **Port busy:** use `PORT=8768 ./run.sh`. Only one server can use a checkout at a time, to protect its shared budget.

## License and attribution

Original harness code is released under [MIT](LICENSE). Third-party software and game data retain their own licenses; see [THIRD_PARTY.md](THIRD_PARTY.md). No engine binaries, game WADs, credentials or historical runs are committed.
