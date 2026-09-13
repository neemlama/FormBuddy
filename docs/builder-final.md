# Agents for Humans: What is FormBuddy — A Good Neighbor for Boring Forms

**Tags:** Agents for Humans, #AgentsforHumans, Strands Agents SDK, Amazon Bedrock, AgentCore

If you live in Nepal, you know this pain.

A student fills the same details 10-15 times a year — ward residency letter, campus scholarship, exam registration, club RSVP. Same name, same citizenship number, same ward number. Different website, slightly different wording every time.

A local cooperative helping 30 families? That pain x30.

Browser autofill tries to help, but it blindly pastes and sometimes submits the wrong thing. If it guesses a required field, your application gets rejected.

That is why I built **FormBuddy**.

## What is FormBuddy in one sentence?

FormBuddy is an AI agent that reads any public-service form live, fills it only from what you actually gave it, and never submits anything until you approve the exact plan.

Think: autonomous until it matters, then it stops and asks you.

Built for the AWS Agents for Humans Hackathon, **Good Neighbor Agents** track, with the **Strands Agents SDK**.

## How it works — kept simple

1. **You point, it reads.** Give it a URL, or click on the tab you already have open. No pre-built form map.
2. **You talk, it matches.** Type "My name is Maya, email maya@example.com" or upload a photo of a citizenship paper or marksheet. It extracts the real values with Bedrock vision.
3. **It never invents.** If a required field is missing, it asks you. The code literally refuses to save an incomplete plan.
4. **You approve.** You see the exact plan: which URL, every field, every value, which submit button. Approve or reject.
5. **Then it acts.** Cloud mode fills and submits for you. Chrome extension mode fills your tab only — you click Submit yourself.
6. **Everything is logged.** Every match, proposal, approval, and submission goes to an audit trail.

4 minutes per form becomes ~45 seconds + one tap to approve.

## What I used on AWS

Simple stack, cost-conscious:

* **Strands Agents SDK** — the brain. One orchestrator, tools for inspect, propose, fill, parse docs, audit.
* **Amazon Bedrock (Claude Haiku 4.5)** — everywhere by default. Inspection, filling, orchestration. ~5x cheaper than Sonnet, Sonnet stays opt-in via env var.
* **AgentCore Browser** — managed cloud browser that reads and fills real forms. No Selenium babysitting.
* **AgentCore Runtime** — where the live demo runs for judging. Deployed on-demand, shut down after.
* **DynamoDB** — sessions + audit log in deployed env. Local JSON files for $0 local dev.

Two modes, one brain. Cloud mode uses AgentCore Browser. Extension mode reads your own tab HTML and never clicks Submit — zero Browser cost, maximum trust.

Tests: 48 pytest tests, zero AWS needed. Total build stayed well under the $50 credit.

## What I learned

1. **The decline path matters more than the approve path.** Everyone demos Approve. Judges poke Reject. Make Reject terminal and clean.
2. **Put guards in code, not in prompts.** "Don't invent values" in the prompt is a wish. `if required field empty: refuse to save plan` in Python is a guarantee.
3. **Explain refusals like an API.** Not "invalid input" but "citizenship_number is required, I have name+email, please provide it." The model self-corrects on first retry.
4. **Haiku is enough for forms.** Field matching is not creative writing. Save Sonnet money for hard reasoning.

## Try it

Repo is public, MIT licensed. Local demo needs only Python 3.12 + `uv`:

```
uv sync
uv run uvicorn api.main:app --reload
# open http://localhost:8000
```

Chrome extension: load `extension/` unpacked, open any form, Analyze This Page.

Live AgentCore Runtime link shared with judges in the Devpost submission and demo video.

---

FormBuddy does not try to be smart. It tries to be trustworthy.

It reads what is there, uses only what you gave, asks when it does not know, and waits for your yes.

That is what a good neighbor does.

#AgentsforHumans
