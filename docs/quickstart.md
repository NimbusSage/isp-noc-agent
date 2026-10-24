# Try the demo before connecting anything real

This quickstart takes you from a fresh clone to a full ticket pipeline run in a few minutes. No credentials, no real systems, everything stays on `127.0.0.1`. It's a good way to look at what the agent does before you connect anything live.

## Prerequisites and one-time setup

You need git, Python 3.9 or newer, Node.js (20 or newer is recommended; 18 works but is past end of life), and `curl`.

Install the server's dependencies once:

```bash
cd server && npm install
```

Then check that your machine is ready for the demo:

```bash
python3 scripts/doctor.py --tier 0
```

That's everything the demo needs.

## Run the demo

```bash
python3 scripts/demo.py
```

This starts the real webhook server against a mock ticket system with eight fictional tickets. A stub stands in for the coding agent. Everything stays on `127.0.0.1`, a real `.env` is never loaded, and the temporary files are removed at the end. The demo sends each sample ticket to the server the way n8n would, then compares what was decided against expected results.

Two other scenarios are worth running. This one shows the bounce gate refusing when the autonomous-bounce switch is off:

```bash
python3 scripts/demo.py --scenario kill-switch
```

This one shows it refusing when the circuit breaker is open:

```bash
python3 scripts/demo.py --scenario breaker-open
```

## What to do next

Tell your coding agent: `onboard me`

The onboarding skill walks through four tiers. You can skip any tier, and each one is checked by `python3 scripts/doctor.py`. Tier 0 is the demo you just ran (no credentials). Tier 1 covers devices: MikroTik and LibreNMS. Tier 2 covers Sonar and notifications, plus the optional ticket pipeline server. Tier 3 covers Google Sheets and Calendar.

If you don't have a coding agent configured yet, run this to see which ones are supported:

```bash
python3 scripts/harness_setup.py --list
```

To configure one:

```bash
python3 scripts/harness_setup.py --harness <name> --write
```

`docs/harnesses.md` covers the details for each supported harness.

When the onboarding skill asks for a credential, it uses `python3 scripts/env_set.py KEY`, which prompts for the value without showing it. Secrets don't go into the chat.

One more thing: autonomous ticket moves are off until you set `BOUNCE_AUTONOMOUS_ENABLED=true` yourself. The onboarding skill will tell you when you reach that point.
